# CLAUDE.md — Live System Build Instructions

> This file is the authoritative instruction set for the EPG live paper trading system.
> The system is built and running. Read this before modifying anything.
> When this file conflicts with any other document, this file wins.

---

## Repo Structure

```text
repo/
├── backtest/                  ← research + backtest code — READ ONLY
├── live/                      ← the running system
├── live_system_architecture.md   ← full architecture spec
├── Tradeable Setup Filter.md  ← setup filter math + design
└── Schema.md                  ← backtest data schema reference
```

**Read before touching signal code:**

1. `../live_system_architecture.md` — architecture, PostgreSQL schemas, locked decisions
2. `strategy.json` — all config values (in this directory)
3. `../Tradeable Setup Filter.md` — setup filter math

---

## What Exists in backtest/ — Do Not Rebuild

| Component | File | Import Statement |
| --- | --- | --- |
| `_hawkes_replay_with_refit` | `backtest/runner.py` | `from backtest.runner import _hawkes_replay_with_refit` |
| `EventAnchor` | `backtest/core/epg/anchor.py` | `from backtest.core.epg.anchor import EventAnchor` |
| `SlopeGate` (live gate) | `backtest/core/epg/gate_variants.py` | `from core.epg.gate_variants import SlopeGate` |
| `GateState` | `backtest/core/epg/gate.py` | `from core.epg.gate import GateState` |
| `ParticipationGate` | `backtest/core/epg/gate.py` | retained in backtest — NOT the live gate |
| `LuldProximityExit` | `backtest/core/exits/luld_proximity.py` | `from core.exits.luld_proximity import LuldProximityExit, ProximityState` |
| `session_bucket()` | `backtest/runner.py` | `from backtest.runner import session_bucket` |
| `SetupFilterResult` + `run_setup_filter` | `backtest/setup_filter.py` | `from backtest.setup_filter import SetupFilterResult, run_setup_filter` |

**The live EPG gate is `SlopeGate` (F_ss), not `ParticipationGate`.** `ParticipationGate` exists in backtest and is importable, but the live system does not use it. Do not import it into live code.

**`SetupFilter` class does not exist.** The public API is `SetupFilterResult` (dataclass) and `run_setup_filter` (function). `LiveSignalState` calls `run_setup_filter()` directly each minute and reads `q_tilde[-1]` for the entry gate.

**`backtest/runner.py` imports trigger Numba JIT.** Cold import takes 10–30 seconds. The Dockerfile warms this at container start.

---

## File Structure (as built)

```
live/
├── CLAUDE.md                  ← this file
├── strategy.json              ← config
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── init_db.sql                ← all PostgreSQL table definitions
├── main.py                    ← entry point
├── config.py                  ← loads + validates strategy.json
│
├── scanner/
│   ├── __init__.py
│   ├── monitor.py             ← stale duplicate, unused — live scanner is scanner_monitor.py
│   └── context.py             ← scanner_quartile + context field computation
│
├── feed/
│   ├── __init__.py
│   ├── universe.py            ← Universe manager
│   ├── context.py             ← TickerContext dataclass
│   ├── signal_loop.py         ← per-ticker asyncio.Task
│   └── market_status.py       ← Polygon market status + holiday cache
│
├── signals/
│   ├── __init__.py
│   ├── live_state.py          ← LiveSignalState — SlopeGate, Hawkes, SF gate
│   ├── scanner_vwap.py        ← VwapSignalState — VWAP bar-close strategy (scanner_vwap mode)
│   └── context_fetch.py       ← cold-start context fetch + Hawkes + SlopeGate replay
│
├── scanner_monitor.py         ← Process 1 (the live scanner — not scanner/monitor.py)
│
├── ticker_classifier.py       ← CS-on-XNYS/XNAS filter
│
├── orders/
│   ├── __init__.py
│   ├── worker.py              ← Process 3, single order_queue consumer
│   ├── risk.py                ← RiskState
│   └── ibkr.py               ← IBKR execution wrapper (ib_insync)
│
├── db/
│   ├── __init__.py
│   ├── pool.py                ← asyncpg connection pool
│   ├── writer.py              ← async batch writer (1s flush, COPY protocol)
│   └── models.py              ← Python-side column definitions
│
├── recovery/
│   ├── __init__.py
│   └── crash_recovery.py      ← startup position reconciliation + flatten
│
├── bot/
│   ├── __init__.py
│   ├── bot.py
│   ├── handlers.py
│   ├── formatters.py
│   ├── probes.py
│   ├── ratelimit.py
│   └── auth.py
│
├── alerts/
│   ├── __init__.py
│   └── telegram.py            ← Telegram bot + kill switch
│
└── export/
    ├── __init__.py
    └── session.py             ← end-of-session parquet export
```

---

## Telegram Bot Commands

| Command | Description |
|---|---|
| `/summary` | Account equity, realised/unrealised/combined P&L, today's win rate and notional, open positions |
| `/status` | Session overview: bucket, position, daily P&L, feed ages |
| `/universe` | All tracked tickers and their states |
| `/scanner` | Scanner snapshot + universe |
| `/positions` | Open positions with unrealised P&L |
| `/trades` | Today's completed trades (full detail) |
| `/risk` | Risk state snapshot |
| `/services` | Full service health probe |
| `/reconcile` | Sync positions against IBKR (clears manual closes) |
| `/kill` | Kill switch — flatten all positions |
| `/help` | Command list |

**Bot architecture note:** The `Application` (polling) reuses the same `Bot` instance as outbound alerts (`send_silent`). Both share `connection_pool_size=8`. Using `.token()` instead of `.bot()` when building the Application would create a second Bot with `pool_size=1`, where the long-poll `getUpdates` (holds the socket open ~30s) and every `reply_text()` compete for the single connection, causing slow command responses.

---

## Process 1 — Scanner Monitor (`scanner_monitor.py`)

Polls `GET /v2/snapshot/locale/us/markets/stocks/gainers` every `poll_interval_s` seconds.

**Admission:** `todaysChangePerc >= gap_threshold (0.30)` AND ticker is common stock on XNYS/XNAS (`TickerClassifier`). **No quartile gate.** All Q1–Q4 names that clear the gap are admitted at all hours. Entry selection is the setup filter's job.

`scanner_quartile`, `scanner_rank`, `scanner_heat`, `scanner_n` are still **computed and stored** in `scanner_snapshots` and `sessions` as analysis fields — they do not gate anything.

**`closed_today` set:** real session closes (EPG_CLOSE, EOD, session_close, crash recovery) lock the ticker out for the day. `scanner_dropoff` removals do NOT add to `closed_today` — a ticker that bounces back into the snapshot can re-enter.

Reconciliation runs both directions each poll: push every qualifying ticker to universe (idempotent), call `universe.handle_snapshot_dropoffs(qualifying_set)` to remove absent tickers with no open position.

---

## Process 2 — Historical Context Fetch (`signals/context_fetch.py`)

Fired per ticker at scanner admission. Two concurrent Polygon REST calls:
```
GET /v3/trades/{ticker}?timestamp.gte={4am_et_ns}&limit=25000   (paginated)
GET /v2/aggs/ticker/{ticker}/range/1/minute/{date}/{date}
```

**What it does:**
1. Replay all historical trades through `_hawkes_replay_with_refit` (backtest code, unchanged).
2. Set `lambda_v_ref` from the cold-start reference: `mu_buy + mu_sell` (fitted or global). **NOT equilibrium formula.** The offline `compute_lambda_ref_per_event` (parquet-catalog lookup) and `compute_global_fallback_ref` (training process-pool) are not usable in the live path.
3. Construct `SlopeGate(tau_sec=180, L_sec=30, k_open=0.5, k_close=0.0, mode="ss", lambda_v_ref=..., warmup_seconds=300)`.
4. Replay `EventAnchor` → `SlopeGate` through all historical ticks, populating the gate's 30s lookback buffer.
5. Run `run_setup_filter()` on the full historical tick buffer so SF is warm at go-live.
6. Pass the **gate instance** (not params) into `LiveSignalState` via `ContextFetchResult`. The `_in_pass` state and lookback buffer must survive the live/historical boundary.
7. At handoff: drop any live tick with `sip_timestamp <= last_historical_ts`. Dedup is non-negotiable.

**`gate_activated` flag:** `SlopeGate` has no public `t_event` property. Activation is tracked by a wiring boolean (`gate_activated` on `ContextFetchResult`, `_gate_activated` on `LiveSignalState`). Never reach into gate internals.

**Fallback tiers:**

| Historical trade count | Action |
|---|---|
| ≥ 1000 | Full replay — normal path. `lambda_v_ref` from fitted params. |
| 100–999 | Global `lambda_ref` fallback. Log DEGRADED. Gate starts from zero state. |
| < 100 | Proceed with caution. Log DEGRADED with count. |
| Timeout (> 10s) | Global `lambda_ref`. Gate from zero. Log DEGRADED. |

---

## Process 2 — Signal Loop and LiveSignalState

### Signal Stack (per tick)

```
Hawkes update (lambda_buy, lambda_sell)
    → EventAnchor (fires T_event once)
    → SlopeGate.update(dollar_vol, t_sec)
    → Setup filter recompute (1-min boundary only)
    → Entry / exit evaluation
```

### Entry Rule

**Both conditions required** (first entry AND re-entry):
1. **SlopeGate rising edge** — previous state was not PASS, current state is PASS.
2. **Setup-filter admission** — `q_tilde[-1] >= q_threshold` on the current bar.

During the warmup phase (first `warmup_bars=65` bars), the provisional threshold `warmup_provisional_threshold=0.75` applies instead of `q_threshold=0.65`.

The 15-minute persistence requirement (`sf.passes`) is **not used as the live entry gate**. `run_setup_filter` is still called every minute and `sf.q_tilde[-1]` is read directly.

### Exit Rule

**Sole strategy exit:** SlopeGate PASS→FAIL or PASS→INACTIVE (`EPG_CLOSE`).

**EXIT_D** — code is present in `_check_exit_d()` but is **disabled** via `CFG.exit_d.enabled = False`. The evaluation block is gated on that flag and never runs.

**LULD proximity** — code is present in `_check_exits()` but is **disabled** via `CFG.luld.enabled = False`. Same pattern.

**Do not remove EXIT_D or LULD code.** Both must remain importable and their evaluation blocks must remain gated on their enable flags. Re-enabling is one config line.

### Setup-Filter De-qualification (universe removal)

When `q_tilde[-1] < q_threshold` for `removal_bars=15` **consecutive bars** (not the same 15-bar persistence as `sf.passes`), `_sf_disqualified` is set and `disqualify=True` is returned in `SignalResult`. The signal loop calls the disqualify callback → `universe.remove_ticker("sf_disqualified")`. This is hysteresis to prevent universe thrash.

---

## Process 3 — Order Worker (`orders/worker.py`)

Single asyncio consumer. The only coroutine that writes to `RiskState` or touches the broker.

**Order types:**
- Pre-market / post-market: marketable limit. Buy = ask + $0.01. Sell = bid - $0.05. `tif="EXT"`, `outside_rth=True`.
- RTH: limit order (not market — IBKR paper trading quirk). `tif="DAY"`.
- Cancel unfilled after 5 seconds. Do not chase. Do not resubmit.

**Fills use explicit transactions — non-negotiable:**
```python
async with conn.transaction():
    await conn.execute("INSERT INTO orders ...", *values)
    await conn.execute("UPDATE positions ...", *values)
```

---

## Startup — Crash Recovery (`recovery/crash_recovery.py`)

Runs before any trading begins. A crash == dead man's switch scenario: cancel all open orders, flatten all open positions, reconcile DB to flat. No smart resume — EPG windows are 30–120s and always expire before a restart completes.

- RTH: market order.
- Pre/post-market: marketable EXT limit ladder (bid ∓ $0.01 → $0.05 → $0.10).
- Outside hours: DEFERRED (position tracked, no order sent).
- DB step: zeroes positions, cancels PENDING orders (`cancel_reason=CRASH_RECOVERY`), writes `CRASH_RECOVERY_CLOSE` audit rows to `signal_events`.
- Idempotent: DB is zeroed even for stuck/deferred cases because IBKR is re-queried at each startup.

---

## Critical Concurrency Rules

| Rule | Detail |
|---|---|
| Signal loops never touch broker or DB | `order_queue.put_nowait()` and hot buffers only |
| `order_worker` is the only `RiskState` writer | Signal loops may read, never write |
| `order_queue` is the single serialisation point | All order submissions go through it |
| Universe lifecycle order on removal | Pop dict → cancel task → unsubscribe WS. Never reversed. |
| Fills use explicit transactions | `async with conn.transaction()` — no bare INSERT for fills |
| Batch writer uses COPY, not INSERT | For ticks, quotes, signal_events |

---

## Locked Decisions

| Decision | Value |
|---|---|
| Scanner admission | `todaysChangePerc >= 0.30` AND CS on XNYS/XNAS. All quartiles admitted. No quartile gate. |
| Entry gate | SlopeGate rising edge AND `q_tilde[-1] >= 0.65` (1-bar; 0.75 for first 65 bars). Both required. |
| EPG gate variant | `ParticipationGate` (half_life=300s, peak_threshold_p=0.65, warmup=300s). Gate class is config-driven via `epg_gate.variant` — switch to `"slope_gate_fss"` to activate SlopeGate F_ss (heuristic/unvalidated). |
| Sole strategy exit | SlopeGate PASS→FAIL (`EPG_CLOSE`). EXIT_D and LULD disabled via config flags; code retained. |
| lambda_v_ref (live) | `mu_buy + mu_sell` at cold start (fitted or global) — same as `lambda_ref`. NOT equilibrium formula. NOT the offline `compute_lambda_ref_per_event`. |
| Setup filter re-entry gate | `q_tilde[-1] >= 0.65` on the current bar, checked in `live_state._sf_admit()`. `sf.passes` (15-bar persistence) is NOT the live gate. |
| Setup filter de-qualification | 15 consecutive bars `q_tilde[-1] < 0.65` → remove from universe. Config: `removal_bars=15`. |
| Crash recovery | Flatten all on startup if any open positions. No smart resume. |
| `scanner_heat` / `scanner_quartile` | Computed and stored as analysis fields only. Neither gates anything. |
| Lee-Ready | Last known quote, no buffering. Accept occasional stale classification. |
| Position sizing (paper v1) | Flat $1,000 RTH / $500 pre-market. |
| Unfilled limit cancel | 5 seconds — do not chase. |
| Failed-exit retry authority | `pending_close_monitor` is the SINGLE retry authority for a failed exit. The order-worker main path must not re-submit or re-arm a ticker already in `pending_close`. EXIT TIMEOUT alert is sent once (on first entry to `pending_close`). |
| Restart continuity (Telegram readouts) | On startup (after crash recovery + equity seed) `reconstruct_daily_state` rebuilds realised `daily_pnl`, Kelly `_trade_history`, `_loss_limit_hit`, and `theoretical_equity` from the DB for the current `session_date` so `/status`/`/risk`/`/summary` are continuous across restarts. Positions are NOT reconstructed (crash recovery flattens on restart; scanner rebuilds the universe). All trade/summary DB queries use `CFG.strategy_id` — never a hardcoded id. |
| Unfillable-exit escalation | Escalate to a MARKET order during RTH only (after `_MARKET_ESCALATION_FAILS=2`); extended hours stay marketable-limit (market orders rejected outside RTH). Hard cap `_MANUAL_REVIEW_FAILS=6` → park (`manual_review_required`) + one alert; never loop. `pending_close_monitor` auto-reconciles against IBKR positions to drop phantoms. |
| Timestamps | All nanoseconds UTC throughout. No timezone assumptions in data layer. |
| IBKR data | [SUPERSEDED 2026-06-15] Execution quotes only. Not the primary feed. |
| IBKR = execution only; pricing from Massive | [2026-06-15] IBKR is used for **portfolio / positions / account / order execution ONLY**. **All** market-data pricing — context fetch, signal, **crash-recovery exits**, and the **flatten paths** (kill / dead-man / pending-close) — comes from **Massive (Polygon) REST/WS** (`live/feed/massive.py::fetch_mark`). `IBKRClient` exposes **no** quote/price method (`snapshot_quote` / `reqMktData` / `reqTickersAsync` removed). Rationale: the paper account has no live market-data subscription, so IBKR `reqMktData` returns Error 10089 / `(0,0)` — this stalled crash recovery and wedged startup (`/tmp/epg_alive` never written → unhealthy restart loop). IBKR needs no data subscription to *execute* a marketable limit, so pricing from Massive and submitting to IBKR is sufficient. |
| PDT Rule | Paper trading only until rule change. Do not remove paper trading constraints. |
| `exit_reason` codes (scanner_vwap) | `vwap_close`, `vwap_cross`, `hard_stop` — additive VARCHARs; no schema migration required. |
| `signal_events.event_type` (scanner_vwap) | `VWAP_ARMED`, `VWAP_ENTRY`, `VWAP_EXIT`, `HARD_STOP` — additive; EPG fields NULL. |
| `scanner_vwap` strategy registration | `id = 'scanner_vwap'` in `strategies` table — inserted idempotently in `init_db.sql`. |
| `CFG.strategy_id` derivation | Auto-derived in `load_config()` from `active_strategy`: `"epg"` → `"epg_v1"`, `"scanner_vwap"` → `"scanner_vwap"`. |
| LULD WS (Massive) | Subscribed as `LULD.{ticker}` alongside `T.`/`Q.` (initial subscribe, post-reconnect re-subscribe, and unsubscribe). **Detection / display / safety only.** `signal_state.update_luld(msg)` (routed by field `T`, not `sym`) sets `_halted`/bands and returns a HALT/RESUME marker for alerting — it never emits an order signal. The LULD *exit* signal stays disabled (`CFG.luld.enabled=False`, quote-derived `LuldProximityExit`); the order/exit path is byte-for-byte unchanged. Indicators 17 (halt) / 18 (resume) are NASDAQ-only (`z==3`); other tapes carry only bands. |

---

## Phase H — Do Not Implement Without Explicit Approval

These were identified in research but are not active:
- Multi-day runner gate
- TOD midday exclusion (11:30–13:30 ET)
- Rank × Heat combined filter
- Quartile-based entry selection (Q3/Q4 preference)

`multi_day_runner` is **collected and stored** in `sessions`. It is not a gate.

---

## Known Failure Modes

| Failure | Required Response |
|---|---|
| Context fetch timeout (> 10s) | Global `lambda_ref` fallback. SlopeGate from zero state. Log DEGRADED. Continue. |
| LULD halt during position | [SUPERSEDED 2026-06-15 — quote-proximity only] Freeze signal state. Resume on first post-halt tick. Do not exit on halt alone. |
| LULD halt during position | [2026-06-15 halt-aware] Real Massive LULD WS sets `is_halted()` (indicator 17) and freezes `LiveSignalState`; cleared on 18. The dead-man's switch does NOT flatten a halted (or halt-suspected: T/Q stale but LULD still arriving — covers NYSE/AMEX) ticker. Still no order-path exit on halt alone. Escalates a single `FlattenTicker` only if the hold exceeds `CFG.luld.max_halt_hold_s` (1800s). `/positions` shows a `⛔ HALTED` badge + bands; halt/resume Telegram alerts are deduped (resume includes duration). |
| Pre-market — no quote > 30s | Soft halt. Pause processing. Do not force-exit. |
| Queue full | `put_nowait` drops tick. Log WARNING. Bounded by design. |
| Open positions on startup | Crash recovery runs. Flattens all before trading begins. |
| scanner_vwap orphaned a just-opened position | [FIXED 2026-06-15] The external-close reconciliation (`_state==LONG and not has_position`) raced the optimistic `record_entry` (LONG set when the order is *queued*, before the fill): the fill-in-flight window was misread as an external close → `session_done` removed the ctx + locked the ticker out, leaving the filled position unmanaged with no price. Fixed with a `_position_confirmed` latch — external-close only fires once the fill is observed in `risk_state`. Readouts also gained a Massive REST fallback (`resolve_mark`) so an open position always shows a real mark even if orphaned/stale. |
| Open position displays `$0` mark | [FIXED 2026-06-15] `signal_state.mark(now_ns)` returns `(price, source, age)` from a dedicated last-trade field (then quote mid, then buffer) — never a silent `0.0`. Readouts render `—` via `format_mark()`; `/risk`, `/status`, `/summary`, hourly P&L skip unrealised P&L when the mark is `0`/`None`. Root cause was the empty-`_sf_prices`→`0.0` fallback + missing guards (see build_log.md). |
| Dead man's switch | [SUPERSEDED — see build_log.md 2026-06-08] No heartbeat > 30s during open position → flatten all immediately. |
| Dead man's switch | [SUPERSEDED 2026-06-15 — halt-unaware] No heartbeat >30s for ticker X with open position → FlattenTickerRequest for X only. FlattenAllRequest reserved for kill switch, daily loss auto-kill, and WS disconnect (60s) only. |
| Dead man's switch | [2026-06-15 halt-aware] Stale ticker X with open position: if `is_halted()` or halt-suspected (LULD still arriving) → HOLD (do not flatten); escalate one `FlattenTicker` only past `CFG.luld.max_halt_hold_s`. Else flatten X only when the global WS is healthy (true symbol feed death); if the global WS is also stale, defer to the WS-disconnect path. `FlattenTickerRequest` still per-ticker; `FlattenAllRequest` reserved for kill / daily-loss auto-kill / WS-disconnect (60s). |
| Unfilled limit order | Cancel after 5s. Do not chase. Do not resubmit (single attempt). |
| Exit won't fill / exit-timeout loop | [2026-06-16] `pending_close_monitor` is the SINGLE retry authority. The order-worker main path skips an exit already in `pending_close` and does NOT re-arm the strategy on timeout; the EXIT TIMEOUT alert fires once. Unfillable exits (illiquid sub-$1 names) escalate to a MARKET order during RTH after `_MARKET_ESCALATION_FAILS` (extended hours stay marketable-limit); after `_MANUAL_REVIEW_FAILS` the ticker is parked (`manual_review_required`) with one alert — never an infinite retry/alert loop. The monitor auto-reconciles against `ibkr.get_open_positions()` to drop a phantom IBKR reports flat. |
| `dv=0` in setup filter | `τ_t = μ_τ(t-1)` — handled in `setup_filter.py`. Do not add logic. |

---

## Key Numbers

- Mean qualifying scanner names per poll: **12.5**
- Typical hot pre-market tick volume (4am → 9:45am): **10,000–30,000 trades**
- Context fetch target time: **< 4 seconds**
- EventAnchor fires **~30s before scanner trigger** — model is already mid-warmup at live handoff
- SlopeGate lookback buffer: **30s**. Must be populated during replay, not built up from live ticks.
- Phase G join rate: **94.6%**
- Strategy ID: `"epg_v1"` (EPG mode) or `"scanner_vwap"` (scanner_vwap mode) — auto-derived at startup from `strategy.active_strategy`

---

## Environment Variables

```bash
DB_URL=postgresql://epg:password@db:5432/epg_live
POLYGON_API_KEY=
IBKR_HOST=host.docker.internal   # Docker Desktop — TWS on host
IBKR_PORT=4002                   # IB Gateway paper trading port
IBKR_CLIENT_ID=1
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DATA_ROOT=/data
```

---

---

## `scanner_vwap` Strategy (v1 — added June 2026)

**Strategy selection is now config-driven.** Set `strategy.active_strategy` in `strategy.json` to either `"epg"` or `"scanner_vwap"`. Restart required. EPG is NOT removed; flipping back to `"epg"` produces zero behavioral diff.

**Implementation:** `live/signals/scanner_vwap.py` — `VwapSignalState` class. Duck-typed to the same protocol as `LiveSignalState`; `signal_loop.py` is strategy-agnostic.

**v1 parameters (HEURISTIC / UNVALIDATED — not backtested):**

| Parameter | Value | Notes |
|---|---|---|
| `vwap_anchor` | `per_bucket` | VWAP resets at each session bucket boundary (pre-market → RTH). |
| Entry | Bar-close strictly above VWAP | Unchanged in all modes. Requires `_armed=True`. |
| Exit (`vwap_exit_mode=tick`) | **First trade below running VWAP** | `VWAP_CROSS` signal. **Current live behavior.** HEURISTIC/UNVALIDATED. |
| ~~Exit (`vwap_exit_mode=bar_close`)~~ | ~~Bar-close strictly below VWAP~~ | ~~`VWAP_CLOSE` signal.~~ **SUPERSEDED** by tick mode as live default. Code retained — flip `vwap_exit_mode` to restore. |
| `hard_stop_pct` | 0.12 | Intra-bar crash backstop. Not the working stop. |
| `one_shot_per_session` | true | After any exit: `session_done_callback` → `closed_today.add(ticker)` for the day. |
| `setup_filter_gate` | true | SF gates arming. Same admission (1-bar, `q_tilde >= 0.65`) and removal (15 consecutive bars) as EPG. |
| `skip_hawkes` | false | Context fetch runs Hawkes replay as-is. VwapSignalState ignores the Hawkes output. |

**Exit reason codes:**
- `VWAP_CROSS` — tick-level exit (price < running VWAP); `vwap_exit_mode="tick"`.
- `VWAP_CLOSE` — bar-close exit (bar close < VWAP); `vwap_exit_mode="bar_close"`. Both modes supported; `bar_close` is the pre-June-2026 behavior.
- `HARD_STOP` — intra-bar crash backstop; either mode.

**Open config forks (pending live validation):**
- `vwap_exit_mode`: `"tick"` vs `"bar_close"` — tick is tighter and faster; bar_close may reduce noise. Pending live comparison.
- `setup_filter_gate`: true/false — is SF arming necessary for VWAP entry quality?
- `vwap_anchor`: `"per_bucket"` vs `"rth_only"` — does including pre-market ticks in VWAP help or hurt?

**One-shot session lockout mechanism:**
- `VwapSignalState.record_exit()` → `_state="CLOSED"`
- Next tick: `update_trade()` returns `SignalResult(disqualify=True, session_done=True)`
- `signal_loop` routes `session_done=True` → `session_done_callback()`
- `session_done_callback` in `universe.py`: `_closed_today.add(ticker)` then `remove_ticker(ticker, "vwap_exit")`
- Scanner's next `_add_ticker()` finds ticker in `_closed_today` → blocked for the day

**Do not implement without explicit approval:**
- Multiple entries per session
- Time-of-day filters on VWAP entry
- Dynamic stop levels (ATR-based, etc.)

---

*Last updated: June 2026 — scanner_vwap v1 added.*
