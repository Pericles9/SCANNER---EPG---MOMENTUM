# Live System Build Log

Running record of decisions made during the build, deviations from the architecture spec, and bugs encountered and fixed. This becomes the Phase H audit trail.

**Format:** Each entry is a date-stamped section. Within a section, entries are one of:
- `[DECISION]` — a judgment call made during implementation, with rationale
- `[DEVIATION]` — something that differs from the architecture spec, and why
- `[BUG]` — something that broke and how it was fixed
- `[FINDING]` — a discovery about the codebase that affects the build

---

## 2026-06-21 — LULD module V3+C2 complete; runner_rapid look-ahead fix

### [FINDING] luld_proximity.py completed: V3 pin+duration clock + C2 independent thresholds

`LuldProximityExit` now has two independent proximity params (`proximity_threshold_upper`,
`proximity_threshold_lower`) and a `lower_enabled: bool = False` flag. All existing callers
pass only `proximity_threshold` (backward-compatible: both thresholds default to it). Lower
band: uses NBBO ask; fires immediately (no pin clock). Upper band retains V3 pin+duration
clock (`luld_exit_duration_sec`). `ProximityResult` now carries `lower_band` and
`ask_proximity_pct` fields. The module is NOT used in EPG-Rapid (exit = EPG PASS→FAIL
only, C2 DROPPED as EPG-Rapid phase). Retained for the classic EPG runner only.

### [BUG] luld_halt_detection.py: 30s VWAP replaced with 5-min sticky mean

The halt labeler used 30-second VWAP as its reference price. `LuldProximityExit` uses a
5-minute arithmetic mean with a 1% sticky filter. This mismatch caused band divergence up
to 35.80% in V3b, making confusion-matrix scoring meaningless. Fixed: labeler now uses
`rolling("300s").mean()` + the same 1% sticky filter as the proximity exit module. Also
adds `HaltWindow.limit_state_start` field (the onset of the limit-state proxy, for the
V3c T3 anchor fix). Changes committed for completeness; labeler output feeds V3c audit
only — not used in EPG-Rapid runner.

### [BUG] runner_rapid.py: look-ahead bias in entry_eligible check

`entry_eligible(sf, n_hold)` was checking `sf.q_tilde[-15:]` — the last 15 bars of the
session, not the last 15 bars AT the entry tick. This used post-entry bars, invalidating
the entry check. Fix: bar-aware check via `np.searchsorted(bar_starts_sf, timestamp,
side='right') - 1` to find the current bar index at each tick, then slice
`sf.q_tilde[:bar_idx+1]`. Inline in the state machine; `entry_eligible` helper import
replaced with direct `Q_THRESHOLD` constant.

---

## 2026-06-20 — LULD Exit Dropped from EPG-Rapid

### [DECISION] LULD exit removed from EPG-Rapid exit stack permanently

After reverse-engineering the Nasdaq halt system and completing the LULD-V3c audit,
the LULD exit mechanism as designed is infeasible for EPG-Rapid.

**Root cause (V3c T5a FN diagnostic):** Halt labeler detects limit-state entry via
trade-price ≥ upper band; the exit module fires on NBBO-bid within proximity_threshold
of upper band. During limit-up, the bid evaporates — the exit is blind to ~70% of
trade-based halts. Widening lead window 15s → 300s: recall 0.25 → 0.31 only.
Structural ceiling, not a threshold problem.

**Impact on EPG-Rapid:**
- C2 (LuldProximityExit independent thresholds) — DROPPED
- R4 (LULD Halt-Avoidance Tuning) — DROPPED
- EPG-Rapid exit stack: EPG PASS→FAIL only (EPG_CLOSE). EXIT_D and LULD both off.
- New phase sequence: C3 → C4 → R0 → R1 → R2 → R3 → R5

**Classic EPG runner:** unaffected. `LuldProximityExit` retained as-is in
`backtest/core/exits/luld_proximity.py`. Phase LULD-REBUILD T6 winner selection
question remains open for the classic runner (separate track).

---

## 2026-06-19 — Pre-flight Wipe of Prior EPG-Rapid Work

### [DECISION] Pre-flight: full wipe of prior EPG-Rapid work

The original EPG-Rapid implementation ran under a bundled-phase structure (single R0
covering setup_filter, LULD, halt-clock, and ROC changes together). That structure has
been replaced with isolated component-validation phases (C1–C4) followed by integration
(R0–R5), per the updated `EPG_Rapid_Test_Phases.md`.

**What was removed:**

- Branch: `epg-rapid` (local and remote) — 4 commits
- Files deleted with branch: `backtest/runner_rapid.py`, `backtest/core/filters/rapid_entry.py`,
  modified `backtest/core/exits/luld_proximity.py` (n_spread_multiple params added),
  modified `backtest/setup_filter.py` and `backtest/core/filters/setup_filter.py`
  (rho_fast param added), `backtest/results/.gitignore`, `backtest/results/phase_r0/.gitkeep`
- Untracked artifacts removed: `backtest/chart_rapid.py`, all contents of
  `backtest/results/phase_r0/` (84 per-event HTML charts, r0_index.html, 3 parquet files,
  5 JSON files across parity_classic/, parity_rapid/, t4_baseline_classic_entry/ dirs)

**Rationale:** The bundled approach could not isolate which component caused a parity
or behavior failure. Starting fresh under the component-isolated structure avoids
carrying forward that risk.

**Impact:** No code from the prior run is reused. C1 begins from a clean `main`.

---

## 2026-06-08 — Flatten Escalation Feedback Loop Fix

### [BUG] Single stuck exit froze the order worker and triggered a continuous flatten cycle

**Root cause:** Two compounding issues created a feedback loop this morning.

1. **`_execute_flatten_all` was serial.** Each `ibkr.submit()` awaited in sequence — with 4 open positions and a 5s cancel timeout each, the order worker was blocked for up to 20s per flatten call, starving all queue entries including Telegram command responses.

2. **Exit timeout escalated to FlattenAllRequest.** When a single exit order timed out, `order_worker` called `_execute_flatten_all` for *every* open position. One illiquid ticker should never close positions in unrelated tickers.

3. **`pending_close_monitor` fired `FlattenAllRequest` on retry.** The retry path used `FlattenAllRequest(reason=f"pending_close_retry_{ticker}")` — still a full account flatten — which re-entered `_execute_flatten_all` serially, blocking the worker again, timing out again, and repeating the cycle.

**Fix:**

- Introduced `FlattenTickerRequest(ticker, reason)` in `live/orders/risk.py` — a per-ticker sentinel that closes exactly one position.
- Removed the exit-timeout → `FlattenAllRequest` escalation from `order_worker`. Timed-out exits are added to `pending_close` for retry only.
- Added `_execute_flatten_ticker` in `worker.py`: mirrors `_execute_flatten_all` for a single ticker with the same market-closed deferral and `pending_close_failures` counter.
- Made `_execute_flatten_all` concurrent via `asyncio.gather` — all positions now submit in parallel, bounding wall time at ~1× `unfilled_cancel_sec` regardless of position count.
- `pending_close_monitor` now enqueues `FlattenTickerRequest` instead of `FlattenAllRequest` for stuck tickers.
- `heartbeat_monitor` in `signal_loop.py` now enqueues `FlattenTickerRequest(ticker=ticker)` — the dead man's switch now affects only the stale ticker's position, not the entire account.

**Invariant:** `FlattenAllRequest` is reserved for: `/kill` command, `kill.flag` watcher, daily loss auto-kill, and WS disconnect (60s). All other flatten paths use `FlattenTickerRequest`.

**Files changed:** `live/orders/risk.py`, `live/orders/worker.py`, `live/feed/signal_loop.py`.
**Tests added:** `live/tests/live/test_flatten_escalation.py` (5 gate tests).
**CLAUDE.md:** Dead man's switch row superseded; new behaviour documented.

---

## 2026-05-20 — Pre-Build Housekeeping

### [FINDING] `live_system_architecture.md` does not exist

The architecture spec (`live/CLAUDE.md`) repeatedly references `../live_system_architecture.md` as the authoritative source for PostgreSQL table schemas (Section 9) and the full architecture. This file is not present in the repo. The DB table names are listed in `live/CLAUDE.md` but column definitions are not provided there. **Blocker for Step 1 (`init_db.sql`)** — cannot write column definitions without this file. Will need to be provided before Step 1 begins.

---

### [FINDING] `SetupFilter` class does not exist

The original spec listed `from setup_filter import SetupFilter, SetupFilterResult` as the live import target. The actual public API in both `setup_filter.py` (root) and `backtest/core/filters/setup_filter.py` exports:

- `SetupFilterResult` — a dataclass
- `run_setup_filter(...)` — a function

No `SetupFilter` class exists. `LiveSignalState` must call `run_setup_filter()` directly on each context fetch and hold the result. This does not change the behavior spec — just the call site shape.

---

### [FINDING] Root `setup_filter.py` had a broken import after repo restructure

The standalone `setup_filter.py` placed at the repo root imported `from data.schemas.mom_db import NS_PER_SECOND`. After the restructure that moved `data/` into `backtest/`, this import path no longer resolves from the repo root.

**Fix:** Inlined `NS_PER_SECOND = 1_000_000_000` directly in `setup_filter.py`. This makes the file truly standalone with no package dependency — correct for its role as the live system's copy. The `backtest/core/filters/setup_filter.py` copy was left unchanged; it works within the backtest package context via `sys.path` manipulation in the backtest runner.

---

### [DECISION] Use root `setup_filter.py` for live, not the backtest copy

Two copies of the setup filter exist: `setup_filter.py` at repo root (standalone) and `backtest/core/filters/setup_filter.py` (backtest package). The live system uses the root copy.

**Rationale:** The root copy has no `backtest` package dependency, making it suitable for import without needing `backtest/` on `sys.path`. The backtest copy is coupled to the backtest data schema path resolution. The root copy is semantically the "production" version of the same code.

**Import:** `from setup_filter import SetupFilterResult, run_setup_filter` — requires repo root on `sys.path`, which Docker handles via `WORKDIR`.

---

### [DECISION] Repo restructured before any live code written

All existing source code was moved from root-level packages into `backtest/` subdirectory. The `live/` directory was created. `CLAUDE(live system).md` and `strategy.json` were moved into `live/`.

**Rationale:** The architecture requires a clean `live/` vs `backtest/` separation. Starting with source code at the root would have caused import ambiguity and made the Docker working directory setup error-prone.

**Impact:** Six files required `sys.path` depth fixes. `data/schemas/mom_db.py` required a `parents[3]` → `parents[4]` fix to preserve the external `DATA_ROOT` pointing to `d:\Trading Research\data`. All 87 tests pass after restructure.

---

### [FINDING] `backtest/runner.py` imports trigger Numba JIT compilation on cold start

Importing `_hawkes_replay_with_refit` from `backtest.runner` triggers Numba JIT compilation of `core.hawkes.engine`. Measured at 10–30 seconds on a cold Python process.

**Impact on live startup:** The live system's context fetch imports `_hawkes_replay_with_refit`. First startup of the Docker container will be slow. Pre-compilation via a warmup import at container start (before market open) is the mitigation — document this in the Dockerfile or `main.py` startup sequence when those are written.

---

### [DECISION] `setup_filter.py` placed at `live/signals/setup_filter.py`

The original spec placed `setup_filter.py` at the repo root. Moved to `live/signals/setup_filter.py` — its primary consumer is `signals/live_state.py`, so the signals subpackage is the correct home. Import within the live package: `from live.signals.setup_filter import SetupFilterResult, run_setup_filter`, or as a relative import `from .setup_filter import ...` from within `live/signals/`.

---

### [FINDING] `strategy.json` `refit_interval_trades` field added (not in architecture spec)

The architecture spec did not include `refit_interval_trades` in the hawkes config block. Added it (`50`) sourced from `backtest/config/hawkes_params.json` (`refit_interval_events: 50`) and the `REFIT_INTERVAL = 50` constant in `backtest/runner.py`.

**Rationale:** The context fetch step replays `_hawkes_replay_with_refit`, which uses this value. Without it in config, the live system would need to hardcode it or re-read the backtest config file. Keeping it in `strategy.json` makes it visible and lockable alongside the other Hawkes params.

---

## 2026-05-20 — Full Build (Steps 1–9 + main.py)

### Step 1 — Infrastructure

#### [FINDING] `live_system_architecture.md` absent — schemas inferred

`live/CLAUDE.md` references `../live_system_architecture.md` Section 9 as the authoritative PostgreSQL table schema source. This file does not exist anywhere in the repo. All ten table schemas in `init_db.sql` were inferred from:

- Table names listed in `live/CLAUDE.md`
- Column names from the backtest filtered/ parquet catalog (`Schema.md`)
- `backtest/data/schemas/mom_db.py` for field types
- Structural conventions from backtest queries visible in the codebase

**Fields requiring human review before first live use:** `scanner_snapshots` JSON shape, `sessions` column set (especially `multi_day_runner` storage format), `hawkes_refits` output shape.

---

#### [DEVIATION] Dockerfile PYTHONPATH set to `/app:/app/backtest`

Original spec implied `PYTHONPATH=/app` only. Updated to `/app:/app/backtest` because `backtest/` modules use internal relative-style imports (`from core.hawkes.engine import ...` not `from backtest.core.hawkes.engine import ...`). Without `/app/backtest` on the path these imports fail at runtime.

**Additionally:** `backtest/runner.py` has `sys.path.insert(0, str(Path(__file__).resolve().parent))` at module scope. This runs on import and adds `/app/backtest` as a redundant safety net. Both mechanisms are in place (belt and suspenders).

---

#### [DECISION] Import style: `from core.X import Y` throughout live modules

Live modules that call into backtest components use `from core.X import Y` (not `from backtest.core.X import Y`). This avoids registering the same module under two different `sys.modules` keys, which would break `isinstance` checks and cause subtle behaviour divergence. `from backtest.runner import _hawkes_replay_with_refit` seeds the `sys.path` insert, making subsequent `from core.X import Y` imports work identically to the backtest environment.

---

#### [DECISION] `live/__init__.py` added (not in spec file list)

The spec's target file structure does not list `live/__init__.py`. Added as empty file so `from live.X import Y` imports work correctly when the container runs with `python -m live.main`. Without it the package is not importable.

---

### Step 2 — Config

#### [FINDING] `rho=0.99` and `rho_E=0.98` are hardcoded, not in `strategy.json`

`_hawkes_replay_with_refit` takes `_RHO` (forgetting factor) and `_RHO_E` (EKF EMA) as parameters. These are set to `0.99` and `0.98` respectively in `context_fetch.py`. They match the backtest runner constants but are not exposed in `strategy.json`. If these need tuning they currently require a code edit.

**Resolution options:** Add to `strategy.json` under `hawkes.rho` and `hawkes.rho_e`, or accept as internal constants. Flagged for human decision.

---

#### [FINDING] `_TAIL_REPLAY_SEC=60.0` and `_DEAD_MAN_TIMEOUT_S=30.0` are hardcoded

Tail-replay window for R-state warmup (60s) and dead man's switch timeout (30s) are hardcoded in `context_fetch.py` and `signal_loop.py` respectively. Both values are derived from the architecture spec text but not in `strategy.json`.

---

### Step 3 — Scanner Monitor

#### [FINDING] `min_quartile` semantic conflict between CLAUDE.md and strategy.json

`live/CLAUDE.md` says: "Q3 and Q4 only. Q1 and Q2 are not traded." `strategy.json` has `min_quartile: 2` with comment "Only Q2, Q3 and Q4 enter." These are contradictory.

**Resolution:** Implemented gate as `if quartile <= CFG.scanner.min_quartile: skip`. With `min_quartile=2`, this skips Q1 (=1) and Q2 (=2), passing Q3 and Q4. Matches the locked decision text in CLAUDE.md. The comment in `strategy.json` appears to be a leftover from a previous iteration. **Human should confirm `min_quartile` value before live use.**

---

#### [DECISION] Scanner `gap_threshold` conversion

Polygon's `todaysChangePerc` is in percentage units (e.g. 30.0 for 30%). `strategy.json` stores `gap_threshold: 0.30` in decimal. Monitor converts: `pct_change >= CFG.scanner.gap_threshold * 100`. This is the correct interpretation; flagged for review since the multiplication is a non-obvious unit conversion.

---

### Step 4 — Historical Context Fetch

#### [FINDING] Lee-Ready in context_fetch uses tick direction only (no historical quotes)

The Polygon trades REST endpoint does not return bid/ask. True Lee-Ready (requiring last-quote comparison) is not possible for historical ticks in context fetch. Used tick direction (sign of price change from previous tick) as fallback. This affects the Hawkes side assignment during the replay window only — live ticks use full Lee-Ready from WebSocket quotes.

---

#### [DECISION] Context fetch fires two REST calls concurrently via `asyncio.gather` with 5s timeout

Both the trades endpoint and 1-minute aggregates endpoint are fetched in parallel. If either times out, fallback to degraded mode (global `lambda_ref`, EPG from zero state) as specified.

---

#### [DECISION] `export_session()` call wired into ticker removal path

The spec says export happens "on session close (end of day or ticker removal)" but does not specify the call site. Placed the call in `universe.py`'s ticker removal path, after the signal loop task is cancelled. This ensures export runs once per ticker regardless of how the session ends. If universe manager itself is shutting down (kill sequence), the export may be skipped — kill sequence prioritizes position flatness over data export.

---

### Step 5 — Feed + Signal Loop

#### [DECISION] `LiveSignalState` pre-populates tick buffer from `ContextFetchResult`

Context fetch returns raw tick arrays (`tick_timestamps_ns`, `tick_prices`, `tick_sizes`). `LiveSignalState.__init__` copies these into its internal tick buffer so that setup filter re-runs at bar boundaries include historical ticks, not just live ticks accumulated since WebSocket subscription. This ensures the setup filter has a meaningful buffer at handoff instead of starting cold.

---

#### [DECISION] Exit OrderRequest uses `qty=0` sentinel

At the time `signal_loop` emits an exit signal, it does not know how many shares are open (risk state is owned by order_worker). Sentinel `qty=0` is used; `order_worker` resolves the actual quantity from `risk_state.open_positions[ticker]["qty"]`. This is the only place qty resolution happens.

---

#### [DECISION] `record_entry()` called immediately after `order_queue.put_nowait()`, before fill confirms

Entry is recorded optimistically when the signal fires, not when the fill arrives. This keeps signal state consistent: once `record_entry()` is called, EXIT_D timer starts and `i_entry` is frozen. If IBKR rejects the order, there will be a brief phantom position in signal state. Order worker calls `record_fill()` only after fill confirm, so risk_state remains accurate. The signal state / risk_state divergence lasts only for the IBKR round-trip (typically < 1s for market orders). Known edge case.

---

### Step 6 — Order Worker

#### [DECISION] IBKR `qualifyContractsAsync` called on every order submit

`ib_insync` requires contract qualification before order placement. Called once per submit rather than caching. Pre-caching would require knowing tickers in advance; since the scanner delivers new names dynamically, per-submit qualification is simpler and correct. Latency impact: ~50–100ms per order. Acceptable for paper trading; revisit for live.

---

#### [DECISION] `FlattenAllRequest` and `OrderRequest` defined in `orders/risk.py`

These are shared types consumed by signal_loop, order_worker, and telegram. Placed in `risk.py` since they are tightly coupled to risk logic. Fewer files than a separate `orders/types.py`.

---

### Step 7 — Risk Management

#### [DECISION] `_loss_limit_hit` flag reused in kill sequence

`execute_kill_sequence()` sets `risk_state._loss_limit_hit = True` to block new entries during the kill. This reuses the daily loss limit flag rather than adding a dedicated kill flag field to RiskState. Pragmatic: once the kill sequence runs, the process exits (`sys.exit(0)`) so flag semantics don't matter.

---

### Step 8 — Alerting

#### [DECISION] `send_silent()` used for all non-critical Telegram messages

All operational alerts (fills, hourly PnL) use `send_silent()` which catches and logs Telegram delivery failures rather than raising. Kill switch confirmation also uses `send_silent()` — if Telegram is down, the kill sequence still proceeds to `sys.exit(0)`.

---

### Step 9 — Session Export

#### [DECISION] Export always runs, even with zero trades

Spec says "If no trades were taken, still export ticks and quotes." Implemented. If the ticks query returns zero rows, `trades.parquet` is not written and a warning is logged. Quotes export is independent.

---

### main.py

#### [DECISION] Startup sequence: DB → IBKR → RiskState → reconcile → Telegram → tasks

Ordering rationale: DB pool must exist before IBKR (reconciliation needs both). Telegram starts last so it doesn't alert before the system is fully initialized. Tasks launch as a group after all initialization is complete.

---

#### [FINDING] `multi_day_runner` always False — Phase H not implemented

`scanner_snapshots` and `sessions` tables have `multi_day_runner` columns. Written as `False` always. Phase H gate is explicitly not implemented per spec. Stored as a future extension point only.

---

## 2026-05-20 — Build Report Fix Pass

### [BUG] All 10 table schemas incorrect — inferred schemas diverged from spec

`live_system_architecture.md` was absent during the original build. All schemas were inferred and contained extensive errors: wrong column names, TIMESTAMPTZ instead of BIGINT nanoseconds, extra columns, missing columns, wrong FK structure. **Root cause:** source of truth was unavailable at build time.

**Fix:** `init_db.sql` fully rewritten from spec Section 9. `db/models.py` column lists updated. All DB write paths updated across `scanner/monitor.py`, `orders/worker.py`, `signals/context_fetch.py`, `feed/signal_loop.py`, `export/session.py`, `main.py`.

---

### [DEVIATION] `signal_events` changed from per-tick to per-transition

Original build appended every tick/quote to `signal_events` (wrong). Spec intends named state transitions only: `T_EVENT_FIRE`, `EPG_PASS_OPEN`, `EPG_PASS_CLOSE`, `RISING_EDGE`, `EXIT_D_TIMER_START`, `EXIT_D_FIRE`, `LULD_PROXIMITY_FIRE`.

**Fix:** Added `signal_events: list` and `hawkes_refit_record: Optional[tuple]` to `SignalResult`. `live_state.update_trade()` detects transitions and accumulates them. `signal_loop` only writes events when `result.signal_events` is non-empty. `hawkes_refits` records now emitted via `hot_hawkes_refits` list (same pattern as `hot_ticks`).

---

### [DEVIATION] `positions` table exit path changed from UPDATE to DELETE

Original build used `UPDATE positions SET status='closed'` on exit. Spec has no `status` column — composite PK (strategy_id, ticker, session_date), one row per open position, deleted on close. `trades` table preserves round-trip history.

**Fix:** `orders/worker.py._update_position` now SELECTs entry data, DELETEs position row, INSERTs trades record using spec column names (`pnl_dollar` not `pnl_dollars`, `entry_ns`/`exit_ns` BIGINT not TIMESTAMPTZ, `qty` not `shares`).

---

### [BUG] worker.py line 55: `return` instead of `continue` on no-fill

On order timeout, `order_worker` called `return` instead of `continue`, terminating the entire worker loop rather than continuing to process subsequent orders.

**Fix:** Changed to `continue`.

---

### [DEVIATION] Scanner quartile gate changed from `min_quartile` to `trade_quartiles`

Original: `if quartile <= CFG.scanner.min_quartile`. This was ambiguous about which quartiles pass. New gate: `if quartile not in CFG.scanner.trade_quartiles` where `trade_quartiles: [2, 3]`. Q2 and Q3 pass; Q1 (dominant movers) and Q4 (weakest tail) are skipped. `strategy.json` and `config.py` updated.

---

### [DECISION] Constants moved to `strategy.json`: `rho`, `rho_e`, `tail_replay_sec`, `dead_man_timeout_s`

Four previously hardcoded constants are now config-driven. No behavioral change — values unchanged from original build.

---

## 2026-05-20 — Pre-Paper-Trading Feature Pass (7 features)

### Feature 1 — Partial fill handling

#### [DECISION] Partial fill resolved via 1s post-cancel wait, not a separate callback

`ibkr.py`'s `submit()` method polls `trade.orderStatus` during the 5s timeout window. On timeout, it cancels the order and waits 1 additional second for the cancel confirm before reading `trade.orderStatus.filled`. If `filled > 0`, a partial Fill is returned with `status='partial_cancelled'` and `remaining_qty = qty - filled_qty`. If `filled == 0`, returns None (no fill).

`Fill` dataclass fields added: `filled_qty`, `remaining_qty`, `status` (`'filled'` | `'partial_cancelled'`). `qty` retains the originally requested shares.

**DB impact:** `orders` table gains `filled_qty INTEGER NOT NULL DEFAULT 0`, `remaining_qty INTEGER NOT NULL DEFAULT 0`.

**worker.py impact:** Entry path uses `fill.filled_qty` (not `fill.qty`) when recording position size.

---

### Feature 2 — Slippage tracking

#### [DECISION] `expected_price` set at signal time, slippage computed after fill

`OrderRequest` gains `expected_price: float = 0.0`. Signal loop sets it at the moment of entry/exit signal:

- Pre-market entry: `expected_price = limit_price` (the ask + $0.01 used as limit)
- RTH entry: `expected_price = last trade price`
- Exit: `expected_price = last trade price`

`ibkr.py` computes `slippage_bps = (fill_price - expected_price) / expected_price * 10000` for buys; reversed sign for sells. Stored on `Fill` and written to `orders` table.

**DB impact:** `orders` table gains `expected_price DOUBLE PRECISION`, `slippage_bps DOUBLE PRECISION`.

---

### Feature 3 — Re-entry after EXIT_D

#### [BUG] `signal_loop` never called `record_exit()` — `_in_position` permanently True after first exit

`LiveSignalState._in_position` is set True by `record_entry()`. Nothing called `record_exit()` after an exit signal was queued, leaving signal state permanently in-position after the first trade. This blocked all subsequent entries for the ticker session.

**Fix:** `signal_loop.py` calls `ctx.signal_state.record_exit()` immediately after putting an exit order on `order_queue` — the same optimistic pattern already used for `record_entry()`. If IBKR rejects the exit (very unlikely for a market sell), signal state diverges briefly; order worker remains authoritative via `risk_state.open_positions`.

---

### Feature 4 — Auto-kill on daily loss limit

#### [DECISION] Auto-kill disabled by default; guarded by `_auto_kill_fired` flag

`strategy.json` `risk.auto_kill_on_daily_loss: false`. When enabled, `order_worker` issues a single `FlattenAllRequest(reason="auto_kill_daily_loss")` the first time an order is blocked by the daily loss limit. The `_auto_kill_fired: bool` flag on `RiskState` prevents re-issuing the flatten request on every subsequently blocked order.

When `auto_kill_on_daily_loss` is False (default), the worker logs and alerts via Telegram but does not flatten — session continues without new entries until manual intervention.

---

### Feature 5 — Status CLI and Telegram `/status` command

#### [DECISION] `get_system_status()` is a shared function, not a method

`live/cli.py` exports `async def get_system_status(risk_state, pool) -> str`. Called by both:

- Telegram `/status` CommandHandler (registered via `telegram.register_status_callback()` in `main.py`)
- Direct CLI: `python -m live.cli` (standalone mode — creates its own pool and a stub `RiskState`)

Status output includes: daily PnL, loss limit state, account equity, theoretical equity, open positions, trades count + realized PnL, and last 10 session rows with degraded mode flag.

---

### Feature 6 — Environment variable template

#### [DECISION] Template at `live/.env.template`; `.env` already in `.gitignore`

`live/.env.template` documents all 8 required environment variables with descriptions and safe defaults. `.gitignore` already excluded `.env` (line 21 — pre-existing). No changes needed to `.gitignore`.

Variables: `DB_URL`, `POLYGON_API_KEY`, `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATA_ROOT`.

---

### Feature 7 — Kelly position sizing framework

#### [DECISION] Kelly uses in-memory `_trade_history` on RiskState, not a DB query

`RiskState._trade_history` is a list of PnL percentages appended by `record_fill()` on each SELL. `compute_kelly_notional()` uses the last `kelly_lookback_trades` entries (default 50). Falls back to flat `rth_notional` when `len(history) < kelly_min_sample` (default 20).

Kelly formula: `f* = win_rate/avg_loss - (1-win_rate)/avg_win`. Fractional: `f* * kelly_fraction * account_equity`. Clamped to `[0.25x, 5x]` of flat RTH notional.

#### [DECISION] `position_sizing.mode: "flat"` is default — Kelly is opt-in

`compute_position_size()` dispatches on `CFG.position_sizing.mode`. `"flat"` uses pre-configured notional by session bucket. `"kelly"` uses Kelly regardless of bucket. Default is `"flat"` to match paper v1 spec. Switch to `"kelly"` requires an explicit config change.

#### [DECISION] Theoretical equity initialized to account equity at startup

`main.py` seeds `risk_state.theoretical_equity = risk_state.account_equity` immediately after querying IBKR `NetLiquidation`. From that point, `theoretical_equity` compounds on each closed trade via `record_fill()`: `theoretical_equity *= (1 + pnl_pct)`. This tracks what the account would be worth if every trade were sized by Kelly.

**DB impact:** `sessions` table gains `account_equity_start DOUBLE PRECISION`, `theoretical_equity_start DOUBLE PRECISION`, `theoretical_equity_end DOUBLE PRECISION`. Start values written on context fetch; end value written by `export_session()` on session close.

#### [DECISION] Account equity refreshed every 5 minutes via `equity_refresher` task

`main.py` spawns `equity_refresher()` as a named asyncio task. Queries `ibkr.get_account_equity()` (IBKR `NetLiquidation` account value) every 300 seconds. Updates `risk_state.account_equity` — used for Kelly sizing in subsequent orders. Errors are logged but do not halt the system.

---
