# Live System Build Log

Running record of decisions made during the build, deviations from the architecture spec, and bugs encountered and fixed. This becomes the Phase H audit trail.

**Format:** Each entry is a date-stamped section. Within a section, entries are one of:
- `[DECISION]` — a judgment call made during implementation, with rationale
- `[DEVIATION]` — something that differs from the architecture spec, and why
- `[BUG]` — something that broke and how it was fixed
- `[FINDING]` — a discovery about the codebase that affects the build

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

## 2026-06-15 — Feed Reliability + LULD WS Integration

### Feed reliability — $0 mark

**GATE 0 root cause (confirmed by static analysis; live instrumentation added for corroboration).**

An open position up >30% intraday displayed as `$0` on Telegram `/positions`. Root cause
is hypothesis **(a): empty / zero-state `_sf_prices` buffer falling back to a hard `0.0`,
compounded by missing `cur_price > 0` guards in the readouts.** It is **independent of LULD
halts** — LULD will not fix it.

**The mark source.** Every readout sources the displayed mark from
`signal_state.last_price`:
- `LiveSignalState.last_price` → `self._sf_prices[-1] if self._sf_prices else 0.0`
  (`live/signals/live_state.py:600-602`).
- `VwapSignalState.last_price` → `self._last_price` (initialised `0.0`,
  `live/signals/scanner_vwap.py:48,398`).

`_sf_prices` is the **setup-filter bar buffer**, not a dedicated last-trade field. It is
seeded from `ctx.tick_prices` at construction and appended only on non-deduped, non-frozen
trade ticks inside `update_trade`. It returns a hard `0.0` whenever the buffer is empty.

**When the buffer is empty for an open position:**
- A **degraded / zero-state session** (`cold_start_n` below `degraded_min_trades`, or a
  context-fetch timeout — `context_fetch.py:464-465` returns `cold_start_n=0`) seeds an empty
  or near-empty buffer.
- A **position recovered at startup** (`existing_position` path in
  `universe.py:_context_fetch_and_start`) calls `record_entry()` immediately; until the first
  live trade prints, `last_price == 0.0`.
- During a **halt at/around the open**, no trades print, so the buffer never primes and the
  `0.0` persists for the whole halt.

**Why it printed a fake catastrophic loss.** The buffer `0.0` then flowed unguarded into the
unrealised-P&L math:
- `/risk` (`handlers.py:269-271`) — `unreal_total += (cur - avg_cost) * qty` with **no
  `cur > 0` guard**.
- hourly P&L (`worker.py:524-526`) — same, **no guard**.
- `/status` (`handlers.py:136-139`) and `/summary` (`handlers.py:399-406`) — same, no guard.
- `/positions` (`handlers.py:226-251`) guards the *total* (`if cur_price > 0`) but still
  renders `format_position_block(current_price=0.0)` → `Current: $0.00` and
  `Unrealised: (0 − avg_cost) × qty` = the full notional as a loss in the per-ticker block.

**Causes ruled out:**
- **(b) ctx evicted while position open** — `handle_snapshot_dropoffs` and the SF/VWAP
  disqualify paths all guard on `not has_position` / state `FLAT`, so an open position's ctx
  stays in `universe`. Only the 20:00 ET `session_close` sweep removes it, and that flattens
  first.
- **(c) signal_state recreated on WS reconnect** — `_ws_connect_and_run` only re-subscribes;
  the `TickerContext` and its `signal_state` persist in `self._universe`. No re-priming gap.

**Instrumentation added (Phase 0, diagnostic-only, no logic change):** `live/bot/diag.py`
`dump_zero_mark()` — a one-shot (per where/ticker) WARNING dump fired from `/positions`,
`/risk`, and the hourly-P&L loop whenever an open position resolves to a `0`/`None` mark. It
reports `last_price`, `len(_sf_prices)`, `last_bid/ask`, `_in_position`, universe membership,
`state_ready`, `degraded_mode`/`cold_start_n` (from `sessions`), WS/heartbeat ages, and
`is_tradable_now()`. The fix is robust to all of (a)/(b)/(c).


### Phase 1 — Price-source hardening (the $0 fix)

#### [BUG] $0 mark fixed by a dedicated last-trade field + a never-zero mark() ladder

**Change.** Added an authoritative mark source to both `LiveSignalState` and
`VwapSignalState`, decoupled from the setup-filter bar buffer:
- `self._last_trade_price` / `self._last_trade_ts_ns` — set on every valid (`price > 0`)
  live trade in `update_trade()`; `LiveSignalState` also seeds `_last_trade_price` from the
  last historical SF tick. `VwapSignalState` keeps its internal `_last_price` (VWAP/bar-close
  math) untouched and tracks the display mark separately.
- `mark(now_ns, stale_s=5.0) -> (price, source, age_s)`, `source ∈
  {LIVE, MID, STALE, HALTED, NONE}`. Priority: fresh last-trade → `LIVE`; else two-sided
  quote mid → `MID`; else stale last-trade → `STALE` (or `HALTED` when `_halted`, set in
  Phase 2); else `(None, 'NONE', 0.0)`. **Never returns 0.0 as a real price.**
- `last_price` property retained for back-compat but re-backed by the same ladder
  (last-trade → mid → buffer tail), returning `None` instead of a silent `0.0`.

**Readouts.** New `format_mark()` in `live/bot/formatters.py` renders the tuple with a source
tag and renders a missing/zero mark as `—`, never `$0.00`. `format_position_block` now takes
`Optional[current_price]` + `mark_str` and suppresses the (fake) unrealised line when the mark
is absent. All readouts route through `mark()` + `format_mark()`: `/positions`, `/status`,
`/risk`, `/summary`, and the hourly P&L loop. The unguarded unrealised-P&L math in `/risk`,
`/summary`, and hourly P&L now skips a `0`/`None` mark — eliminating the fake catastrophic-loss
print.

**Tests.** `tests/live/test_mark_source.py` (25 tests): the `mark()` ladder on both signal
states (LIVE/MID/STALE/HALTED/NONE, never 0.0), `update_trade` wiring the last-trade fields,
and `format_mark` / `format_position_block` rendering `—` (never `$0.00`) for a missing mark.
Full `tests/live` suite: **179 passed, 5 skipped**.


### Phase 2 — Massive LULD WS integration (detection / display / safety only)

#### [DECISION] LULD WS added for halt awareness; the LULD exit signal stays disabled

**Subscriptions.** `universe.py` now subscribes `LULD.{ticker}` alongside `T.`/`Q.` in the
initial subscribe, the post-reconnect re-subscribe, and the unsubscribe-on-remove. The WS
message loop was refactored to accept `ev ∈ {T, Q, LULD}`, normalise timestamps (LULD `t` is
ms, same as T/Q — `_normalize_ws_timestamps` already converts the `t` field), and update the
global `ws_last_msg_t` heartbeat for any of the three.

**Routing.** LULD events carry the ticker in field **`T`** (not `sym`), so the existing
`_dispatch` (keyed on `sym`) would silently drop every LULD message. Added `_dispatch_luld`
which resolves the ticker from `T`, looks up the ctx, and calls
`signal_state.update_luld(msg)` synchronously. Late / unknown tickers drop cleanly. Telegram
HALT / RESUME alerts (deduped — fired only on the state-transition edge, RESUME includes the
halt duration) are sent from the dispatch layer, keeping the signal_state free of broker/DB/
telegram coupling.

**Signal state.** `update_luld(msg)` (on both `LiveSignalState` and `VwapSignalState`) stores
`_luld_bands`, `_luld_indicators`, and a monotonic `_luld_last_seen` (used by Phase 3). It sets
`_halted=True` on indicator **17** (and calls the existing `freeze()` on `LiveSignalState`),
clears it on **18** (calls `resume(duration)`), and **returns only a `('HALT'|'RESUME', dur)`
marker or `None` — never an order signal.** Indicators 17/18 are NASDAQ-only (`z==3`); other
tapes carry only bands (Phase 3 infers halt for those). `is_halted()` exposes the flag;
`mark()` reports source `HALTED` for a stale trade while halted.

**Display.** `/positions` shows a `⛔ HALTED (LULD band $l–$h)` badge and `/status` appends a
`[HALTED]` tag when `is_halted()`.

**Tests.** `tests/live/test_luld_ws.py` (14 tests): ms→ns normalisation, `T`-field routing
(+ unknown/missing-`T` clean drops), band/indicator storage, 17→halt / 18→resume with
duration, duplicate-17 no re-trigger, non-Nasdaq (`z!=3`) band breach does NOT halt, and no
order signal produced. Full `tests/live`: **193 passed, 5 skipped**.


### Phase 3 — Halt vs feed-death disambiguation (stop halts triggering flatten)

#### [BUG] Dead-man's switch force-flattened on a halt; now halt-aware

**Problem.** A halt produces the same per-ticker T/Q gap as feed death, so the
dead-man's switch (`heartbeat_monitor`) would `FlattenTicker` a halted position —
contradicting the locked rule "do not force-exit on halt alone".

**Fix.** `heartbeat_monitor` now threads the global `ws_last_msg_t` box and `telegram` in
(from `UniverseManager._heartbeat_loop`). The per-pass decision was extracted into the
testable `_dead_mans_switch_pass`. For each stale ticker with an open position:
- **Real halt** (`signal_state.is_halted()`, indicator 17) **or halt-suspected** (T/Q stale
  but `luld_last_seen` fresh — covers NYSE/AMEX where 17/18 don't exist) → **HOLD**, alert once,
  start a grace timer. Escalate a single `FlattenTicker(reason="halt_hold_cap_exceeded")` only
  if the hold exceeds `CFG.luld.max_halt_hold_s` (new config, default 1800s).
- **No halt evidence** → flatten X only when the global WS is healthy (true symbol feed
  death); if the global WS is also stale, **defer to the WS-disconnect path** (it owns that).
  Bookkeeping (`halt_since`/`alerted`/`escalated`) is cleared when a ticker recovers.

**`ws_healthy` default.** When no WS box is supplied (e.g. legacy call sites / tests),
`ws_healthy` defaults to True so true feed-death still flattens — preserving prior behavior;
the gate only *suppresses* a flatten when the global WS is positively known to be down.

**Flatten-escalation coordination.** `fix_flatten_escalation.md` has **already landed** — the
dead-man path was already issuing `FlattenTickerRequest` (per-ticker), not an account-wide
`FlattenAllRequest`. No request-type change needed; only the halt-aware skip was added.

**Tests.** `tests/live/test_dead_mans_switch.py` (7 tests): halted → no flatten; halt-suspected
(LULD fresh) → no flatten; true feed death (WS healthy) → flatten (`dead_mans_switch`); feed
death with WS down → defer; no position → skip; hard-cap escalates exactly once; recovery clears
bookkeeping. Full `tests/live`: **200 passed, 5 skipped**.


## 2026-06-15 — Pricing from Massive, never IBKR (fix stuck crash-recovery exit)

### [BUG] Crash recovery priced exits from IBKR market data → 10089 stall wedged startup

**Locked principle (now in CLAUDE.md):** IBKR is **portfolio / positions / account /
order-execution only**. All market-data pricing comes from **Massive (Polygon)**.

**Root cause.** `recovery/crash_recovery.py::_get_quote` called `ib.reqMktData`, and the
flatten paths (`worker.py::_execute_flatten_all`/`_execute_flatten_ticker`) called
`IBKRClient.snapshot_quote` (`reqTickersAsync`) — both IBKR **market data**. On the paper
account post-market there is no live data subscription: IBKR returns **Error 10089** and
`(0,0)`. Crash recovery then priced limits off `avg_cost`, never filled, marked positions
STUCK, and `main.py` `sys.exit(1)`-halted startup → Docker restart loop, `/tmp/epg_alive`
never written → container **unhealthy**. (This is what surfaced after the LULD rebuild.)

**Fix.**
- **`live/feed/massive.py::fetch_mark(ticker, api_key)`** — single-ticker Polygon snapshot
  → `(bid, ask, last_trade)`; NBBO-preferred, last-trade fallback; never raises. Reuses
  `POLYGON_API_KEY`. Pure `_parse_snapshot` for testability.
- **crash_recovery** — `_get_quote` (IBKR `reqMktData`) removed; replaced by
  `_get_quote_massive` (bounded retry budget: `_MASSIVE_PRICE_RETRIES`, so recovery can
  never wedge the heartbeat). `run_crash_recovery` takes `polygon_api_key` (threaded from
  `main.py`). If Massive has **no** price after the budget, recovery does **not** blind-fire
  an `avg_cost` order or mark STUCK — it routes to **DEFERRED** (manual review, non-halting)
  so recovery finishes and the main loop starts.
- **worker flatten paths** — `snapshot_quote` → `fetch_mark`; NBBO bid (last-trade fallback,
  `avg_cost*0.5` emergency last resort). `expected_price` still set for slippage.
- **`IBKRClient.snapshot_quote` removed** — replaced by a guard comment forbidding any
  IBKR quote/price method, so the 10089 path cannot be reintroduced. Execution calls
  (`positions`, `get_open_positions`, `get_account_equity`, `openOrders`/`openTrades`,
  `has_open_order_for`, `cancel_all_orders`, `submit`) are untouched.

**Audit (Task 3).** Grep for `reqMktData` / `reqTickers*` / `snapshot_quote` across `live/`:
the only remaining references are the guard comment in `ibkr.py`. No pricing/signal path
uses IBKR market data.

**Tests.** `tests/live/test_massive.py` (5), `tests/live/test_crash_recovery_pricing.py` (4:
Massive-priced submit with IBKR market data never called; Massive-empty → DEFER, no blind
order, no STUCK, alert; 10089-no-longer-blocks; module-has-no-IBKR-quote guard). Existing
`test_crash_recovery.py` extended-hours tests + `test_flatten_escalation.py` repointed to
mock `fetch_mark` and assert IBKR market data is never called. Full `tests/live`: **209
passed, 5 skipped**.


### [BUG] scanner_vwap orphaned just-opened positions (no price, unmanaged)

**Symptom.** `/positions` showed `Current: —` for 3 of 4 open positions (and a 75-min
`STALE` mark for the 4th). The three had `EPG gate: ?` / `Scanner: Q?` → no `TickerContext`
in the universe at all, so nothing to price them from — and, worse, no signal_state means
**no VWAP/hard-stop exit is ever generated** for them (unmanaged until session close).

**Root cause (race).** `VwapSignalState.update_trade` had an external-close reconciliation:
`if _state=="LONG" and not risk_state.has_position(ticker): → CLOSED + session_done`. But
`signal_loop` calls `record_entry()` **optimistically** when the ENTRY order is *queued*
(`_state="LONG"`), before the BUY fills. In the fill-in-flight window the strategy is `LONG`
while `risk_state` has no position yet → the next tick misread this as an external close →
`session_done` → `_vwap_session_done_callback` removed the ctx and added the ticker to
`closed_today`. Then the entry filled: position open in `risk_state`, ctx gone, locked out.
Logs confirmed: `external close detected` fired ~120 ms *before* the BUY `placeOrder`.

**Fix.** Added a `_position_confirmed` latch (set once `has_position` is observed True while
`LONG`). The external-close branch now requires `_position_confirmed` — so the entry-fill-in-
flight window can no longer be mistaken for an external close. Genuine external closes (kill
switch / EOD / manual flatten after the fill) still fire correctly. Exit checks
(VWAP_CROSS/HARD_STOP) were deliberately NOT gated on the latch (HARD_STOP already requires an
avg_cost from risk_state; VWAP_CROSS is bar/price-driven).

**Display robustness.** Added `live/feed/massive.py::resolve_mark(signal_state, ticker,
now_ns)` — prefers the live WS-fed `signal_state.mark()`, falls back to a Massive REST
`fetch_mark` when the WS mark is stale/absent OR there is no signal_state (orphaned position).
Wired into `/positions`, `/status`, `/risk`, `/summary`, and hourly P&L; `format_mark` renders
the fallback as `$X.XX (REST)`. An open position now always shows a real mark.

**Current orphans.** The already-orphaned positions are cleaned up on the next restart:
crash recovery (now Massive-priced) flattens all open IBKR positions. The race fix prevents
new orphans; a periodic "adopt untracked open position into the universe" reconciler remains a
recommended follow-up (so a mid-session orphan would be re-managed without a restart).

**Tests.** `TestEntryFillRace` (2: in-flight not closed; external-close only after confirmed),
`resolve_mark` (4), `format_mark` REST tag (1). Full `tests/live`: **216 passed, 5 skipped**.

## 2026-06-16 — Exit-timeout retry loop fix (YYGH)

### [BUG] A non-filling exit spun in an infinite retry + Telegram-spam loop

**Symptom.** YYGH spammed `EXIT TIMEOUT: YYGH — added to pending_close for retry` every ~6 s.

**Root cause (two compounding issues).**
1. **Two retry authorities in parallel.** On each 5 s `unfilled_cancel_sec` timeout the
   `order_worker` main path added the ticker to `pending_close` AND called
   `request.on_fill_failed()` (= `clear_exit_pending`), **re-arming the strategy**. The next
   tick below VWAP re-emitted VWAP_CROSS → another exit → another timeout → another alert.
   Meanwhile `pending_close_monitor` *also* retried. No dedup between them, no alert throttle.
2. **Unfillable order.** YYGH is a real 5,952-share position (~$0.13). The exit is a marketable
   SELL **limit** at $0.08 the IBKR paper simulator won't fill for a thin sub-$1 name.
   `IBKRClient.submit()` is all-limit; crash recovery (which fills) uses **market** orders.

**Fix A — single retry authority + throttle (`order_worker`).**
- Skip an exit whose ticker is already in `pending_close` (and still held): `pending_close_monitor`
  is the sole retry authority.
- Send the EXIT TIMEOUT Telegram only when the ticker *first* enters `pending_close`.
- Stop re-arming the strategy on timeout (removed the `on_fill_failed()` call) — once committed to
  exit, `pending_close` owns it; `scanner_vwap`'s `_position_confirmed`-gated external-close
  reconciliation marks it CLOSED once it actually flattens.

**Fix B — actually flatten / never loop forever.**
- `OrderRequest.order_type` ("LMT"|"MKT"); `ibkr.submit` builds a `MarketOrder` for "MKT".
- `_execute_flatten_ticker` (+ shared `_build_flatten_request`): after
  `_MARKET_ESCALATION_FAILS` (2) limit timeouts AND during RTH, escalate to a MARKET order
  (mirrors crash recovery, fills illiquid names). Extended hours stay marketable-limit (market
  orders are rejected outside RTH).
- Hard cap `_MANUAL_REVIEW_FAILS` (6): park the ticker (→ `manual_review_required`, out of
  `pending_close`), one manual-review alert, stop auto-retrying.
- `pending_close_monitor`: auto-reconcile against `ibkr.get_open_positions()` (drop a phantom
  that IBKR reports flat); skip parked tickers; throttle the "STUCK POSITION" alert to once;
  throttle the per-retry "FLATTEN triggered" / market-closed alerts.

**Tests.** `tests/live/test_exit_retry.py` (7): dedup-skip, alert-once + no-rearm, MKT submit,
`_build_flatten_request` MKT/LMT, RTH market escalation, extended-hours stays limit, hard-cap
park, monitor phantom-reconcile. `test_flatten_escalation.py` monitor test updated to report the
position held at IBKR. Full `tests/live`: **224 passed, 5 skipped**.

## 2026-06-16 — Telegram command continuity across restarts

### [BUG] Trades/summary blank + realised P&L reset to $0 after a mid-session restart

**Root cause.**
1. `/trades` and `/summary` queried the `trades` table with a hardcoded `"epg_v1"`
   (`handlers.py:91,465`). In `scanner_vwap` mode the day's trades are stored under
   `"scanner_vwap"`, so both showed nothing/wrong stats — independent of restart.
2. `risk_state` is rebuilt fresh each boot (`daily_pnl=0`, empty `_trade_history`,
   `theoretical_equity` reset, `_loss_limit_hit=False`) with no DB reconstruction, so `/status`,
   `/risk`, `/summary` showed `$0` realised P&L after a mid-day restart while the `trades` /
   `sessions` tables still held the real figures.

**Fix.**
- `handlers.py`: module-level `from live.config import CFG`; `/trades` + `/summary` now query
  `CFG.strategy_id` (strategy-agnostic; matches `cli.py`).
- New `live/recovery/state_recovery.py::reconstruct_daily_state()` — best-effort (never raises)
  rebuild from the DB for the current `session_date` / `strategy_id`: `daily_pnl` =
  `sum(trades.pnl_dollar)`; `_trade_history` = last `kelly_lookback` `pnl_pct` (oldest→newest,
  for Kelly); `_loss_limit_hit` re-armed when `daily_pnl <= max_daily_loss`; `theoretical_equity`
  carried from the latest non-null `sessions.theoretical_equity_end` (else the account-equity
  seed). Called once in `main.py` after crash recovery + the equity seed, before the task group
  starts — live fills then accumulate on top (no double counting; crash recovery writes
  `signal_events`, not `trades`).
- Positions intentionally NOT reconstructed: crash recovery flattens on restart, so
  `open_positions` correctly reflects the flat post-recovery state; the scanner rebuilds the
  universe.

**Tests.** `tests/live/test_state_recovery.py` (6): daily_pnl + Kelly-history (oldest→newest)
reconstruction, loss-limit re-arm, null-theo keeps the equity seed, empty-DB flat start, DB-error
safety, and a regression guard that `handlers.py` has no hardcoded `"epg_v1"`. Full `tests/live`:
**230 passed, 5 skipped**.
