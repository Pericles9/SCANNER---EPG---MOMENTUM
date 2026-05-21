---
tags:
  - type/reference
  - domain/backtest
  - domain/hawkes
  - project/scanner-epg-momentum
  - status/wip
created: 2026-05-04
last_reviewed: 2026-05-07
---

# Project Directory — scanner-epg-momentum

## Directory Tree

```
scanner-epg-momentum/
├── backtest/
│   ├── runner.py               — Main backtest runner (entry + exits)
│   ├── epg_replay.py           — EPG replay for research/debugging
│   ├── charts.py               — Per-event chart generation
│   ├── run_charts.py           — CLI wrapper for charts.py
│   └── signal_charts.py        — Signal panel charts
├── config/
│   ├── strategy.json           — All strategy params (EPG, Hawkes, EXIT_D, LULD)
│   ├── hawkes_params.json      — Phase A iter 7 calibrated Hawkes params
│   ├── epg_params.json         — EPG params with Phase R rationale
│   ├── q_bar_tiers.json        — Q-bar tiers for Lee-Ready classification
│   └── holdout_boundary.json   — Train/val/test split boundary (locked)
├── core/
│   ├── epg/
│   │   ├── anchor.py           — EventAnchor (dollar volume crossing detector)
│   │   └── gate.py             — ParticipationGate (λ_V decay, peak threshold)
│   ├── exits/
│   │   └── luld_proximity.py   — LuldProximityExit (Tier 2 band proximity, RTH only)
│   ├── filters/
│   │   └── setup_filter.py     — SetupFilter (range, volume, thinness, conviction)
│   ├── hawkes/
│   │   ├── engine.py           — HawkesEngine (univariate K=1, online replay)
│   │   ├── forgetting.py       — fit_hawkes_forgetting, fit_online, HawkesParams
│   │   └── ekf.py              — KalmanIntensityEstimator (used by engine)
│   └── ofi/
│       └── trade_ofi.py        — compute_trade_ofi (Lee-Ready side classification)
├── data/
│   ├── loaders/
│   │   ├── trades.py           — load_trades, list_events, compute_lambda_ref
│   │   ├── quotes.py           — load_quotes
│   │   └── prev_close.py       — get_prev_close
│   └── schemas/
│       └── mom_db.py           — CONFIG_DIR, NS_PER_SECOND, path constants
├── docs/
│   ├── Project_Directory.md    — This file
│   ├── Scanner-EPG-Momentum.md — Strategy spec
│   └── (phase result files added here as runs complete)
├── logs/                       — Run logs (gitignored)
├── results/                    — Backtest outputs (gitignored)
├── tests/
│   ├── test_epg.py             — EventAnchor + ParticipationGate tests (22 tests)
│   ├── test_hawkes_ll.py       — Hawkes log-likelihood + engine tests (19 tests)
│   └── test_luld_proximity.py  — LuldProximityExit tests (9 tests)
├── tools/
│   └── exit_d_tuning/
│       ├── replay.py           — Build per-event replay caches (Hawkes+EPG state)
│       └── simulate.py         — Sweep EXIT_D params without re-running Hawkes
├── CLAUDE.md                   — Session directives
└── MEMORY.md                   — Claude's working memory
```

---

## Module Interfaces

### `core/epg/anchor.py` — EventAnchor

```python
class EventAnchor:
    def __init__(self, k: float, lambda_ref: float)
    def set_lambda_ref(self, lambda_ref: float) -> None
    def on_trade(self, t_sec: float, dollar_vol: float) -> float | None
        # Returns t_event (seconds) on first crossing, None otherwise
    def reset(self) -> None
    @property
    def threshold(self) -> float    # k * lambda_ref
    @property
    def t_event(self) -> float | None
```

**Inputs:** Trade timestamps (seconds), dollar volume per trade
**Outputs:** `t_event` on first crossing of threshold (k × λ_ref)

---

### `core/epg/gate.py` — ParticipationGate, GateState

```python
class GateState(enum.Enum):
    INACTIVE = "INACTIVE"
    WARMUP = "WARMUP"
    PASS = "PASS"
    FAIL = "FAIL"

class ParticipationGate:
    def __init__(self, tau: float, p: float, warmup_sec: float = 300.0)
    def activate(self, t_event: float) -> None
    def update(self, t_sec: float, dollar_vol: float) -> GateState
    def reset(self) -> None
    @property
    def state(self) -> GateState
    @property
    def lambda_v(self) -> float
    @property
    def running_peak(self) -> float
```

**Inputs:** Trade timestamps (seconds), dollar volume per trade; t_event from EventAnchor
**Outputs:** GateState at each tick; λ_V decays with half-life τ

---

### `core/exits/luld_proximity.py` — LuldProximityExit, ProximityState

```python
class ProximityState(enum.Enum):
    INACTIVE = "INACTIVE"
    ACTIVE = "ACTIVE"
    EXIT_HALT = "EXIT_HALT"

class LuldProximityExit:
    def __init__(self, ref_window_sec: float = 300.0,
                 proximity_pct_threshold: float = 2.0,
                 warmup_sec: float = 60.0)
    def update(self, t_sec: float, price: float, rth_active: bool) -> ProximityState
    def reset(self) -> None
```

**RTH only:** Returns INACTIVE for pre/post-market. Fires EXIT_HALT when price within
`proximity_pct_threshold`% of Tier 2 LULD band.

---

### `core/filters/setup_filter.py` — SetupFilter, run_setup_filter

```python
@dataclass
class SetupFilterResult:
    passes: bool
    q_tilde: float          # composite filter score [0, 1]
    range_signal: float
    volume_signal: float
    thinness_signal: float
    conviction_signal: float

def run_setup_filter(
    ticker: str,
    date: str,
    session_start_ns: int,
    session_end_ns: int,
    mom_pct: float,
) -> SetupFilterResult
```

---

### `core/hawkes/forgetting.py` — fit_hawkes_forgetting, fit_online, HawkesParams

```python
@dataclass
class HawkesParams:
    mu_buy: float
    mu_sell: float
    alpha_buy_self: float
    alpha_sell_self: float
    beta: float

def fit_hawkes_forgetting(
    t_sec: np.ndarray, sides: np.ndarray, rho: float, lambda_ref: float,
    T: float, init_params: np.ndarray, n_restarts: int = 5,
    beta_fixed: float = 0.1,
) -> HawkesParams

def fit_online(
    t_sec: np.ndarray, sides: np.ndarray, rho: float, lambda_ref: float,
    prev_params: HawkesParams, T: float, n_restarts: int = 1,
    beta_fixed: float = 0.1,
) -> HawkesParams
```

---

### `backtest/runner.py` — Main Runner

**CLI:**
```
python -m backtest.runner
    --split {train,val,trainval}    default: val
    --max-events N                  limit for testing
    --random-sample N               stratified random sample
    --seed N                        random seed
    --workers N                     parallel workers
    --config PATH                   strategy.json path
    --results-dir PATH              output directory
    --ticker TICKER                 single event debug
    --date DATE                     single event debug
    --exit-d-theta FLOAT            override theta
    --exit-d-tau-min FLOAT          override tau_min
    --gap-threshold FLOAT           override gap gate
```

**Outputs** (to `--results-dir`):
- `per_trade.parquet` — one row per trade: entry/exit timestamps, prices, PnL, exit reason
- `per_event.parquet` — one row per event: PF, n_trades, session breakdown
- `summary.json` — aggregate metrics: overall PF, win rate, mean PnL

---

### `tools/exit_d_tuning/replay.py` — Replay Cache Builder

Runs Hawkes+EPG replay for each event and saves per-event state arrays.
Used by `simulate.py` to sweep EXIT_D params without re-running Hawkes.

```
python -m tools.exit_d_tuning.replay --split val --results-dir results/sweep_caches/
```

---

### `tools/exit_d_tuning/simulate.py` — EXIT_D Parameter Sweep

Loads replay caches and simulates EXIT_D for multiple (theta, tau_min) combinations.

```
python -m tools.exit_d_tuning.simulate --caches-dir results/sweep_caches/ --output results/sweep_results.json
```

---

## Module Dependency Graph

```
backtest/runner.py
    ├── core/hawkes/engine.py        ← core/hawkes/ekf.py
    ├── core/hawkes/forgetting.py    ← core/hawkes/ekf.py
    ├── core/epg/anchor.py
    ├── core/epg/gate.py
    ├── core/ofi/trade_ofi.py
    ├── core/filters/setup_filter.py
    ├── core/exits/luld_proximity.py
    ├── data/loaders/trades.py       ← data/schemas/mom_db.py
    ├── data/loaders/quotes.py
    └── data/loaders/prev_close.py
```

---

## Config Schema — strategy.json

```json
{
  "epg": {
    "k_multiplier": 5,
    "half_life_seconds": 300,
    "peak_threshold_p": 0.65,
    "warmup_seconds": 300
  },
  "hawkes": {
    "K": 1,
    "beta": 0.1,
    "rho": 0.99,
    "refit_interval_events": 50,
    "cold_start_size": 1000
  },
  "exit_d": {
    "theta": 0.65,
    "tau_min_sec": 4.0
  },
  "luld": {
    "ref_window_sec": 300.0,
    "proximity_pct_threshold": 2.0,
    "warmup_sec": 60.0,
    "rth_only": true
  },
  "gap_gate": {
    "threshold": 0.30,
    "backtest_only": true
  }
}
```
