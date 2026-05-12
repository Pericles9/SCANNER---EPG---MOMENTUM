---
tags:
  - type/reference
  - domain/data
  - domain/gpu
  - project/scanner-epg-momentum
  - status/complete
created: 2026-04-04
---
<!-- fullWidth: false tocVisible: false tableWrap: true -->
# HPC Data Processing Plan

### Alpha Hypothesis — Scanner × Hawkes × OFI Price Impact

---

## Hardware Baseline

| Resource | Spec                                  | Key Constraint                                |
| -------- | ------------------------------------- | --------------------------------------------- |
| CPU      | Ryzen 5 3600 — 6c/12t, 4.2GHz boost   | 12 threads max; no AVX-512                    |
| GPU      | GTX 1070 — 1920 CUDA cores, 8GB GDDR5 | Compute 6.1 (Pascal); no FP16 Tensor cores    |
| RAM      | 32GB DDR4                             | Comfortably fits mom_db working set in memory |
| Storage  | Assume NVMe SSD                       | Parquet I/O is the likely first bottleneck    |

**Architecture philosophy:** The CPU handles all per-event streaming logic (Hawkes, EKF, OFI, regime machine). The GPU handles bulk matrix work during offline calibration (MLE, regressions, perm_frac). DuckDB runs the heavy historical scans. Never pay Python interpreter overhead in the hot path.

---

## Section 1 — Data Layer (All Phases)

### 1.1 Storage Format and Access

All historical data lives in mom_db as Parquet files. This is already the right choice. A few things to enforce:

**Partition by ticker + date.** DuckDB can prune partitions before reading anything. A scan for a single symbol over 60 trading days should touch zero files outside that symbol's folder.

```
mom_db/
  filtered/
    {TICKER}_{DATE}_{MOM}/
      trades.parquet
      quotes.parquet

```

**Column pruning.** Never `SELECT *` in DuckDB. Pull only the columns each phase needs. For the setup filter that's `timestamp, open, high, low, close, volume, vwap`. For Hawkes it's `timestamp, price, size, side`. Each unnecessary column is wasted I/O.

**Predicate pushdown.** Push time filters into the query, not post-fetch in Python:

```sql
-- Good: DuckDB reads only matching row groups
SELECT timestamp, price, size, side
FROM read_parquet('filtered/**/*.parquet')
WHERE ticker = 'NVDA'
  AND session_date = '2025-11-15'
  AND timestamp BETWEEN '04:00' AND '20:00';

-- Bad: reads everything, filters in Python
df = pd.read_parquet(...)
df = df[df['ticker'] == 'NVDA']

```

**Row group size.** When writing Parquet, target \~100k rows per row group. Smaller row groups make predicate pushdown more effective on timestamp ranges.

### 1.2 DuckDB Configuration

Pin this at startup for all historical work:

```python
import duckdb

con = duckdb.connect()
con.execute("SET threads = 10")                    # leave 2 threads for OS
con.execute("SET memory_limit = '24GB'")           # leave headroom for Python
con.execute("SET temp_directory = '/tmp/duckdb'")  # spill path if needed
con.execute("PRAGMA enable_progress_bar")

```

DuckDB will vectorize internally across all 10 threads. You don't need to write any parallel code for the scan layer — let it do it.

### 1.3 In-Memory Working Set

32GB RAM means you can comfortably hold an entire symbol's trade + quote history in memory during calibration. The pattern to use:

```python
# Load once per symbol into numpy arrays — not DataFrames
result = con.execute(query).fetchnumpy()
timestamps = result['timestamp']   # np.int64 (unix ns)
prices     = result['price']       # np.float64
sizes      = result['size']        # np.float32
sides      = result['side']        # np.int8  (-1, 0, 1)

```

Numpy arrays are cache-friendly and pass directly to Numba without copy. DataFrames add overhead with no benefit in the inner loop.

---

## Section 2 — Setup Filter (Phase F0, Ongoing Calibration)

### 2.1 Computation Profile

The setup filter runs four EMA-based signals per 1-minute bar. It's O(N_bars × N_symbols). For a catalog of 500 events × \~200 bars each = 100k rows. This is trivially fast even in pure Python — but batch-vectorizing it correctly still matters because you'll run it thousands of times during threshold search (Phase F0.4).

### 2.2 Vectorized Numpy Implementation

The exponential forgetting update is a first-order IIR filter. Numpy doesn't have a native cumulative EMA, but `scipy.signal.lfilter` implements it in C:

```python
from scipy.signal import lfilter

def ema_filter(x: np.ndarray, rho: float) -> np.ndarray:
    # S_t = rho * S_{t-1} + (1 - rho) * x_t
    # Equivalent IIR: b = [1 - rho], a = [1, -rho]
    b = np.array([1 - rho])
    a = np.array([1.0, -rho])
    return lfilter(b, a, x)

```

This runs the full EMA pass in a single C call — no Python loop over bars.

**Batch all four signals together:**

```python
def compute_setup_filter(bars: dict, rho_slow=0.985, rho_fast=0.90) -> np.ndarray:
    r_t   = (bars['high'] - bars['low']) / bars['close']   # bar range
    v_t   = bars['volume']
    dv_t  = bars['dollar_volume']
    tau_t = np.where(dv_t > 0,
                     (bars['high'] - bars['low']) * bars['midprice'] / dv_t,
                     np.nan)

    mu_r   = ema_filter(r_t,   rho_slow)
    mu_v   = ema_filter(v_t,   rho_slow)
    mu_tau = ema_filter(tau_t, rho_slow)  # handle NaN with forward fill first

    range_score = np.minimum(r_t   / (mu_r   * 0.60), 1.0)
    vol_score   = np.minimum(v_t   / (mu_v   * 0.30), 1.0)
    thin_score  = np.maximum(1.0 - tau_t / (mu_tau * 2.50), 0.0)

    # geometric mean (add body conviction when implemented)
    composite = (range_score * vol_score * thin_score) ** (1/3)
    return ema_filter(composite, rho_fast)

```

### 2.3 Parallelizing Across Symbols (Phase F0.5 Full Catalog Scan)

Each symbol is independent. Use `multiprocessing.Pool` with the full 12 logical CPUs:

```python
from multiprocessing import Pool

def process_symbol(symbol_path: str) -> dict:
    bars = load_bars(symbol_path)   # DuckDB query
    q    = compute_setup_filter(bars)
    return {'symbol': symbol_path, 'q_mean': q.mean(), 'q_min_15m': rolling_min(q, 15)}

with Pool(processes=10) as pool:   # 10 workers, leave 2 for DuckDB
    results = pool.map(process_symbol, all_symbol_paths)

```

At 10 workers this runs the full 500-event catalog in seconds, not minutes.

---

## Section 3 — Hawkes Engine (Hot Path)

The Hawkes per-event update is the tightest loop in the entire system. In live trading this runs on every trade print. In historical calibration it runs on millions of events. This is where Python overhead kills you.

### 3.1 What Needs JIT Compilation

The inner loop touches:

- `R[k]` update: `R[k] = 1 + exp(-beta_k_eff * dt) * R[k_prev]`
- `lambda_buy`, `lambda_sell` summation across K kernels
- EKF predict/update (2-state)
- Adaptive β computation
- Ė EMA update
- Regime state transitions

All of this should be a single Numba-compiled function. **No Python interpreter calls in the event loop.**

### 3.2 Numba JIT Setup (K=1, Free Beta)

```python
import numba as nb
import numpy as np

@nb.njit(cache=True, fastmath=True)
def hawkes_update(
    t: float,
    t_prev: float,
    side: int,                      # +1 buy, -1 sell
    R_buy: float,                   # scalar (K=1)
    R_sell: float,                  # scalar (K=1)
    alpha_self_buy: float,          # scalar
    alpha_cross_buy: float,         # scalar
    alpha_self_sell: float,         # scalar
    alpha_cross_sell: float,        # scalar
    mu_buy: float,
    mu_sell: float,
    beta_mle: float,                # scalar — fitted by MLE
    lambda_hat: float,              # Snyder estimate
    lambda_ref: float,
) -> tuple:

    dt = t - t_prev
    beta_eff = beta_mle * (lambda_hat / max(lambda_ref, 1e-10))

    # Decay existing R
    decay = np.exp(-beta_eff * dt)
    R_buy  *= decay
    R_sell *= decay

    # Compute left-limit intensities BEFORE adding event
    lam_buy  = mu_buy  + alpha_self_buy * R_buy + alpha_cross_buy * R_sell
    lam_sell = mu_sell + alpha_self_sell * R_sell + alpha_cross_sell * R_buy

    # Add new event contribution AFTER intensity computation
    if side == 1:
        R_buy += 1.0
    else:
        R_sell += 1.0

    return R_buy, R_sell, lam_buy, lam_sell, beta_eff

```

**Key Numba flags:**

- `cache=True` — compiles once, reuses on next run. First call will be slow (\~1s JIT). All subsequent calls are near-native.
- `fastmath=True` — allows reordering of floating point ops. Safe here; gives \~15–30% speedup on the exp/dot operations.
- `@nb.njit` not `@nb.jit` — forces nopython mode. If Numba can't compile something it errors immediately instead of silently falling back to Python.

### 3.3 EKF in Numba

The EKF state is 1D (log-λ̂). Predict and update are trivial arithmetic — no matrix inversion needed. Write it as a separate `@nb.njit` function and call it from the main event loop:

```python
@nb.njit(cache=True)
def ekf_update(
    x: float,    # log(lambda_hat)
    P: float,    # scalar variance
    lam_obs: float,
    Q: float,    # process noise
    R_noise: float,
) -> tuple:
    # Predict
    x_pred = x
    P_pred = P + Q

    # Jacobian H = d(lambda)/d(log_lambda) = exp(x) = lambda_hat
    lam_hat = np.exp(x_pred)
    H = lam_hat

    # Innovation
    S = H * P_pred * H + R_noise
    K = P_pred * H / S
    innov = lam_obs - lam_hat

    # Update
    x_new = x_pred + K * innov
    P_new = (1.0 - K * H) * P_pred

    return x_new, P_new

```

### 3.4 Replay Loop Design

For historical calibration the replay loop should never allocate in the inner loop. Pre-allocate all output arrays before the loop starts:

```python
N = len(timestamps)
lam_buy_out  = np.empty(N, dtype=np.float64)
lam_sell_out = np.empty(N, dtype=np.float64)
E_out        = np.empty(N, dtype=np.float64)
Edot_out     = np.empty(N, dtype=np.float64)

# Then pass these arrays into a Numba function that fills them in place
hawkes_replay(timestamps, sides, params, lam_buy_out, lam_sell_out, E_out, Edot_out)

```

This avoids repeated memory allocation and keeps cache pressure low.

### 3.5 Expected Throughput

On a Ryzen 5 3600 with Numba + `fastmath=True`, a K=1 bivariate Hawkes update (including Snyder filter and Ė EMA) should run at roughly 10–40M events/second in the compiled loop. K=1 is faster than K=7 due to scalar operations replacing array dot products. A single symbol with 500k trades replays in \~25–50ms. A 500-event catalog with \~200k trades each replays in under 30 seconds.

### 3.6 Warm Path — Online Refitting

The warm path runs MLE parameter refitting in the background while the hot path continues processing events. This is the online adaptation mechanism described in the spec.

**Event buffer accumulation:** The hot path appends `(timestamp, side)` pairs to a rolling buffer. When the buffer reaches `refit_interval_events` (default: 50) new events since the last refit, the warm path is triggered.

**Refit execution:** Use `concurrent.futures.ProcessPoolExecutor` with a single worker. Submit the `fit_online()` call as a future. The hot path checks for completion non-blockingly at each event — it must never wait for the refit to finish.

```python
from concurrent.futures import ProcessPoolExecutor

executor = ProcessPoolExecutor(max_workers=1)
refit_future = None

# In the event loop:
if n_new_events >= refit_interval and refit_future is None:
    refit_future = executor.submit(fit_online, event_buffer, rho, prev_params)

if refit_future is not None and refit_future.done():
    new_params = refit_future.result()
    engine.swap_params(**new_params)
    refit_future = None
```

**Atomic parameter swap:** `HawkesEngine.swap_params()` uses a `threading.Lock` to ensure the hot path never reads a partially-updated state. All 7 parameters (4 alpha, 2 mu, 1 beta) are updated together inside the lock. The lock is held only for the assignment — no computation happens inside it.

**Warm start:** `fit_online()` uses the previous parameter solution as one of the optimizer starting points. This dramatically improves convergence speed in the online setting — median refit target is < 500ms with 5 restarts on Ryzen 5 3600.

**Constraint:** The hot path must NEVER block waiting for the warm path. If a refit takes longer than expected, the hot path continues with the previous parameter set. Stale params are always better than blocking the event stream.

---

## Section 4 — OFI Computation

### 4.1 Trade OFI — Lee-Ready Classification

Lee-Ready runs per trade. It has branching logic (compare to midpoint, check tick direction), which makes it awkward to vectorize naively. Two approaches:

**Option A (preferred for calibration): Vectorized numpy with `np.where`**

```python
def lee_ready_classify(
    prices: np.ndarray,
    mids: np.ndarray,
    threshold_frac: float = 0.10    # set empirically per Phase 0 spec
) -> np.ndarray:
    above = prices > mids * (1 + threshold_frac)
    below = prices < mids * (1 - threshold_frac)
    tick_up = np.diff(prices, prepend=prices[0]) > 0

    side = np.where(above, 1,
           np.where(below, -1,
           np.where(tick_up, 1, -1)))    # tick test fallback
    return side.astype(np.int8)

```

This is fully vectorized — one pass over the array with no Python loop.

**Option B (live path): Numba JIT for the stateful tick test**

The tick test requires memory of the last non-zero tick direction. That's stateful and doesn't vectorize cleanly. For live use, wrap it in Numba:

```python
@nb.njit(cache=True)
def lee_ready_live(price: float, mid: float, last_side: int, threshold: float) -> int:
    spread_frac = (price - mid) / mid
    if spread_frac > threshold:
        return 1
    elif spread_frac < -threshold:
        return -1
    else:
        return last_side   # tick test: carry last direction

```

### 4.2 OFI Accumulation

The 10-second OFI window uses exponential forgetting (`ρ` set per Phase C). This is a single scalar update per trade — trivial arithmetic. Put it inside the Numba event loop alongside the Hawkes update. Zero additional overhead.

```python
@nb.njit(cache=True)
def ofi_update(ofi_prev: float, signed_vol: float, rho: float) -> float:
    return rho * ofi_prev + (1.0 - rho) * signed_vol

```

### 4.3 Quote OFI — Microprice + Quote Imbalance

Quote events arrive separately from trades. On a pre-market micro-cap you might see 5–50 quote updates per second. This is slower than the trade stream — no special treatment needed. Run quote OFI in a lightweight Numba function alongside the trade loop, triggered on each quote event.

```python
@nb.njit(cache=True)
def microprice(bid: float, ask: float, bid_sz: float, ask_sz: float) -> float:
    total = bid_sz + ask_sz
    return (bid * ask_sz + ask * bid_sz) / total  # weighted toward pressure side

@nb.njit(cache=True)
def quote_imbalance(bid_sz: float, ask_sz: float) -> float:
    return (bid_sz - ask_sz) / (bid_sz + ask_sz)

```

---

## Section 5 — Calibration (GPU-Accelerated Phases)

This is where the GTX 1070 earns its keep. Calibration involves repeated regression and MLE fitting over the full catalog. These are batch matrix operations — exactly what the GPU is good at.

### 5.1 GTX 1070 Capabilities

- 8GB GDDR5 — fits the entire calibration dataset in GPU memory (comfortably for a 500-event catalog at tick resolution)
- CUDA 6.1 — supports CuPy, PyTorch (CPU fallback available), and custom CUDA kernels
- No FP16 Tensor cores — use FP32 throughout; FP64 is slow on Pascal (1/32 rate)

Use FP32 for all GPU work. The accuracy difference vs FP64 is negligible for regression and OFI fitting.

### 5.2 perm_frac OLS Regression (Phase C)

The OLS slope `Cov(Δmid_60s, Δmid_5s) / Var(Δmid_5s)` needs to be computed per spread tier across hundreds of events. Use CuPy to batch this:

```python
import cupy as cp

def compute_perm_frac_gpu(delta_mid_5s: np.ndarray, delta_mid_60s: np.ndarray) -> float:
    x = cp.asarray(delta_mid_5s, dtype=cp.float32)
    y = cp.asarray(delta_mid_60s, dtype=cp.float32)

    x_c = x - x.mean()
    y_c = y - y.mean()

    slope = cp.dot(x_c, y_c) / cp.dot(x_c, x_c)
    r2    = (cp.dot(x_c, y_c) ** 2) / (cp.dot(x_c, x_c) * cp.dot(y_c, y_c))

    return float(slope), float(r2)

```

For the stratified-by-tier computation, batch all tiers as a matrix multiply:

```python
# X: (N_events, N_features), Y: (N_events,)
# Normal equations: beta = (X^T X)^{-1} X^T Y
X_gpu = cp.asarray(feature_matrix, dtype=cp.float32)
Y_gpu = cp.asarray(targets, dtype=cp.float32)
beta  = cp.linalg.lstsq(X_gpu, Y_gpu, rcond=None)[0]

```

`cp.linalg.lstsq` uses cuBLAS under the hood on the 1070. For a 500×10 system this is near-instant. For a 50k×10 bootstrap this matters.

### 5.3 Hawkes MLE Fitting (Phase A)

Hawkes MLE requires evaluating the log-likelihood across parameter space. This involves:

1. Replaying the event sequence under candidate parameters → CPU (Numba)
2. Computing the log-likelihood sum → CPU (Numba, trivial)
3. Running the optimizer (L-BFGS-B or basin-hopping) → CPU

The GPU is not well-suited for sequential event replay. Keep MLE on the CPU with Numba. However, if you're doing a **grid search over initial conditions** (which you should, given \~28 α parameters), parallelize across starting points on the CPU:

```python
from concurrent.futures import ProcessPoolExecutor

def mle_from_start(init_params):
    return scipy.optimize.minimize(neg_log_likelihood_numba, init_params,
                                   method='L-BFGS-B', bounds=bounds)

with ProcessPoolExecutor(max_workers=10) as ex:
    futures = [ex.submit(mle_from_start, p0) for p0 in initial_conditions]
    results = [f.result() for f in futures]

```

10 parallel MLE runs on 10 threads. Pick the best final loss.

### 5.4 Kernel Count Validation (K=3/4/5/7 test)

This is 4 MLE fits × N_symbols. Embarrassingly parallel. Use the same `ProcessPoolExecutor` pattern with `(K, symbol)` as the work unit. At 10 workers and a 500-symbol catalog, this runs 4× faster than serial.

### 5.5 Bootstrap / Sensitivity Analysis

The Phase E sensitivity analysis (20+ parameters) requires many re-fits. This is the most compute-intensive calibration step.

**Strategy:** Run the outer bootstrap loop in a `ProcessPoolExecutor`. Each worker gets a parameter perturbation and runs a full calibration sequence. The GTX 1070 can accelerate the regression steps inside each worker if you use CuPy with `CUDA_VISIBLE_DEVICES` set — but be careful: multiple workers sharing the GPU will contend on memory. Limit GPU-using workers to 2–3 simultaneous if the dataset is large.

---

## Section 6 — Regime State Machine

The four-state machine (BASELINE → BUILDING → SUPERCRITICAL → COLLAPSE) runs per-event and is stateful. It's a simple switch on scalar values of `E(t)`, `n_base`, and the lockout counter.

This is already trivial compute. The only HPC concern is **correctness under Numba**:

```python
@nb.njit(cache=True)
def regime_update(
    state: int,          # 0=BASELINE, 1=BUILDING, 2=SUPERCRITICAL, 3=COLLAPSE
    E: float,
    n_base: float,
    n_thresh: float,
    lockout_remaining: int,
    E_min: float,
    E_collapse: float,
) -> tuple:             # (new_state, lockout_remaining)

    if state == 2:      # SUPERCRITICAL
        if n_base < n_thresh:
            return 3, 50    # COLLAPSE, set lockout
    elif state == 3:    # COLLAPSE / lockout
        if lockout_remaining > 0:
            return 3, lockout_remaining - 1
        elif E < E_collapse:
            return 0, 0     # back to BASELINE
    elif state == 1:    # BUILDING
        if n_base >= n_thresh:
            return 2, 0     # SUPERCRITICAL
    elif state == 0:    # BASELINE
        if E > E_min:
            return 1, 0     # BUILDING

    return state, max(0, lockout_remaining - 1)

```

Keep this as a pure function with scalar inputs. It compiles to a handful of machine instructions.

---

## Section 7 — Live Streaming Path

In live trading the architecture is different from calibration. You have one symbol at a time, events arriving at 0–500 TPS.

### 7.1 Thread Architecture

```
[Data Feed Thread]  →  [Event Queue]  →  [Signal Thread]  →  [Order Logic]
    (I/O bound)         (lock-free)        (CPU bound)          (I/O bound)

```

- **Data Feed Thread:** Receives raw WebSocket messages, parses JSON/binary, pushes typed structs onto the queue. Python `asyncio` or a C extension like `hummingbot`'s connector.
- **Event Queue:** Use `collections.deque` with a lock, or better, `multiprocessing.Queue` if the signal thread is a separate process. Keep it shallow — if the queue depth exceeds 100 events, something is wrong.
- **Signal Thread:** Runs the Numba-compiled Hawkes + EKF + OFI + regime loop. **This thread must never do I/O.** Pre-load all parameters at startup.

### 7.2 Latency Budget

On a Ryzen 5 3600 the Numba-compiled per-event update (Hawkes + EKF + OFI + regime) should run in **<10 µs**. The bottleneck in live trading will be network I/O and JSON parsing, not signal computation. So don't over-optimize the signal thread — optimize the feed parser.

If you're on a REST-poll data source (not WebSocket), set the poll interval to match your signal's time resolution. The Hawkes kernel is clock-time — polling at 100ms is fine for pre-market extended-hours at typical TPS.

### 7.3 LULD Halt Handling

Halts must freeze the signal state. The cleanest way is a boolean flag checked at the top of the event loop:

```python
@nb.njit(cache=True)
def should_process_event(is_halted: bool, halt_start: float,
                          halt_end: float, t: float) -> bool:
    if is_halted:
        return False
    if halt_start <= t <= halt_end:
        return False
    return True

```

When `False`, skip the Hawkes and EKF update entirely but do not decay R — the pause prevents clock-time decay from collapsing R during the halt window.

---

## Section 8 — Memory Layout and Cache Optimization

### 8.1 Struct-of-Arrays, Not Array-of-Structs

Don't store events as a list of dicts or a structured record array with mixed types. Store each field as its own flat array:

```python
# Bad: array-of-structs (poor cache locality for single-field access)
events = np.array([(t1, p1, s1, side1), ...], dtype=[('ts', 'f8'), ('price', 'f8'), ...])

# Good: struct-of-arrays (contiguous access per field in Numba loops)
timestamps = np.array([t1, t2, ...], dtype=np.float64)
prices     = np.array([p1, p2, ...], dtype=np.float64)
sizes      = np.array([s1, s2, ...], dtype=np.float32)
sides      = np.array([s1, s2, ...], dtype=np.int8)

```

When the Numba replay loop reads `timestamps[i]` for all i, the entire timestamp array fits in L2 cache. Mixed-type records cause cache line waste.

### 8.2 Data Types

| Field               | Type    | Why                                              |
| ------------------- | ------- | ------------------------------------------------ |
| timestamp (unix ns) | int64   | Exact, no float rounding                         |
| price               | float64 | Mid-prices need precision                        |
| size                | float32 | 4 bytes; 32-bit is enough for share counts       |
| side                | int8    | 1 byte; only -1/0/1                              |
| R_k arrays          | float64 | Accumulation over many events; precision matters |
| EKF state (x, P)    | float64 | Filter stability                                 |
| OFI accumulator     | float64 | Running sum                                      |
| Composite Q score   | float32 | Output only; 4 bytes is fine                     |

Using `int8` for side and `float32` for size cuts memory use on long catalogs and improves cache efficiency.

### 8.3 Pre-computation

During parameter loading (before any replay), pre-compute everything that doesn't change per event:

- `beta_base * (1 / lambda_ref)` → just multiply in the hot path
- `mu_buy + mu_sell` denominator for E(t)
- `alpha_self`, `alpha_cross` as contiguous float64 arrays, not Python lists
- All threshold scalars as Python-level constants (Numba inlines them)

---

## Section 9 — Phase-by-Phase Processing Summary

| Phase                          | Primary Work                            | Best Tool                                | Parallelism                 |
| ------------------------------ | --------------------------------------- | ---------------------------------------- | --------------------------- |
| Phase 0 — Universe scan        | Count triple-AND pass rate              | DuckDB                                   | Automatic (10 threads)      |
| Phase 0.5 — Train/test split   | Partition event catalog                 | DuckDB / Pandas                          | Single-threaded; trivial    |
| Phase F0 — Filter validation   | Build Q(t) trajectories for 200+ events | Numpy (vectorized) + ProcessPoolExecutor | 10 workers                  |
| Phase F0.5 — Full catalog scan | Apply filter to all training events     | Numpy + ProcessPoolExecutor              | 10 workers                  |
| Phase A — Hawkes MLE           | Online refit validation: K=1 free-beta MLE per event session | Numba (replay) + ProcessPoolExecutor     | 10 workers for event-level parallelism; single worker for warm-path refit |
| Phase B — OFI calibration      | Fit OFI_norm, γ per tier                | Numba (replay) + CuPy (regression)       | CPU for replay, GPU for OLS |
| Phase C — Impact regression    | perm_frac, β\_impact per tier           | CuPy lstsq                               | GPU                         |
| Phase D — Backtest             | Replay full catalog end-to-end          | Numba event loop                         | 10 workers across symbols   |
| Phase E — Sensitivity analysis | Parameter perturbation fits             | Numba + ProcessPoolExecutor              | 10 workers                  |
| Live — Signal computation      | Per-event Hawkes/EKF/OFI/regime         | Numba (single thread)                    | None — inherently serial    |

---

## Section 10 — Setup and Dependency Stack

```
Python 3.11+
│
├── duckdb >= 0.9           # columnar SQL engine; multi-threaded scan
├── numba >= 0.59           # JIT compilation; LLVM backend
├── numpy >= 1.26           # array primitives; feeds into Numba without copy
├── scipy                   # lfilter for EMA; L-BFGS-B for MLE
├── cupy-cuda11x            # GPU arrays; matches your CUDA 11.x driver
└── pyarrow                 # Parquet I/O; used by DuckDB internally

```

**Verify GPU compute capability before installing CuPy:**

```bash
nvidia-smi  # should show GTX 1070, CUDA 11.x driver
python -c "import cupy; print(cupy.cuda.Device(0).compute_capability)"
# Should print: 61

```

If CuPy install is problematic, the GPU regressions can fall back to `numpy.linalg.lstsq` with \~5–10× slowdown — acceptable for Phase C which runs once, not in a hot loop.

---

## Section 11 — Profiling and Bottleneck Identification

Don't guess what's slow. Profile first.

**For the Numba replay loop:**

```python
import time

t0 = time.perf_counter()
hawkes_replay(timestamps, sides, params, out_arrays)
t1 = time.perf_counter()

n_events = len(timestamps)
print(f"{n_events / (t1 - t0) / 1e6:.2f}M events/sec")

```

Target: >5M events/sec. If below 1M, check:

1. Is `@nb.njit` in effect? (print function type: should say `CPUDispatcher`)
2. Is `cache=True` working? (second call should be same speed as first)
3. Are Python objects leaking into the Numba function? (check for `object mode` warnings)

**For DuckDB scans:**

```python
con.execute("PRAGMA enable_profiling")
con.execute(your_query)
con.execute("PRAGMA disable_profiling")

```

Look for `TABLE_SCAN` rows with high elapsed time — those indicate missing predicate pushdown.

**For GPU calibration:**

```python
import cupy as cp
cp.cuda.Stream.null.synchronize()   # ensure prior ops are done
t0 = time.perf_counter()
result = cp.linalg.lstsq(X_gpu, Y_gpu)
cp.cuda.Stream.null.synchronize()
t1 = time.perf_counter()

```

Always synchronize before timing GPU ops — they're async by default.

---

## Quick Reference — Rules That Prevent Common Mistakes

1. **Never call Python from inside a Numba hot loop.** No `print()`, no `dict` lookups, no `list.append()`. Pre-allocate output arrays.
2. **Never `SELECT *` in DuckDB.** Always name the columns you need.
3. **Always use `int64` for unix nanosecond timestamps.** Float64 loses precision past microseconds.
4. **Never allocate inside the event replay loop.** Allocate once before, write in-place.
5. **Set `CUDA_VISIBLE_DEVICES=0` explicitly** if you run multiple processes that each try to use the GPU.
6. **Pin DuckDB thread count to 10**, not 12 — leave 2 logical CPUs for the OS and Python GC.
7. **First Numba call is slow.** Always trigger a warm-up call with dummy data at startup before timing or going live.