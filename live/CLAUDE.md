# CLAUDE.md — Live System Build Instructions

> This file is the authoritative instruction set for building the EPG live paper trading system.
> You are implementing a fully designed, research-validated system. You are not making design decisions.
> When this file conflicts with any other document, this file wins.

---

## Repo Structure

```text
repo/
├── backtest/                  ← existing research + backtest code — READ ONLY
├── live/                      ← you are building this
│   └── signals/setup_filter.py  ← standalone setup filter — import, do not rebuild
├── CLAUDE.md                  ← top-level project brief (read for strategy context)
├── live_system_architecture.md   ← full architecture spec (read before starting)
├── Schema.md                  ← backtest data schema reference
└── Tradeable Setup Filter.md  ← setup filter math + design
```

**Read before writing any code:**

1. `../live_system_architecture.md` — full architecture, PostgreSQL schemas, all locked decisions
2. `../CLAUDE.md` — strategy overview, Phase G v2 gate, locked decisions list
3. `../Tradeable Setup Filter.md` — setup filter spec
4. `strategy.json` — config values (in this directory)

---

## What Exists in backtest/ — Do Not Rebuild

Before writing any signal code, locate these in `backtest/` and confirm their exact import paths:

| Component | File | Import Statement |
| --- | --- | --- |
| `_hawkes_replay_with_refit` | `backtest/runner.py` | `from backtest.runner import _hawkes_replay_with_refit` |
| `EventAnchor` | `backtest/core/epg/anchor.py` | `from backtest.core.epg.anchor import EventAnchor` |
| `ParticipationGate` | `backtest/core/epg/gate.py` | `from backtest.core.epg.gate import ParticipationGate, GateState` |
| `LuldProximityExit` | `backtest/core/exits/luld_proximity.py` | `from backtest.core.exits.luld_proximity import LuldProximityExit, ProximityState` |
| `session_bucket()` | `backtest/runner.py` | `from backtest.runner import session_bucket` |
| `_et_offset_ns()` | `backtest/data/loaders/trades.py` | `from backtest.data.loaders.trades import _et_offset_ns` |
| `_session_ns_bounds()` | `backtest/data/loaders/trades.py` | `from backtest.data.loaders.trades import _session_ns_bounds` |
| `SetupFilterResult` + `run_setup_filter` | `backtest/setup_filter.py` | `from backtest.setup_filter import SetupFilterResult, run_setup_filter` |

**Important:** The canonical `setup_filter.py` lives at `backtest/setup_filter.py`. The original spec listed `SetupFilter` as the import target; no such class exists. The public API is `SetupFilterResult` (dataclass) and `run_setup_filter` (function). See `## Import Notes` for details.

**Do not reimplement any of these.** If you cannot locate a component, stop and report the path issue rather than writing a replacement.

---

## Target File Structure

Build exactly this layout. Do not add files not listed here without flagging it first.

```
live/
├── CLAUDE.md                  ← this file
├── strategy.json              ← config (already written — read it)
├── docker-compose.yml         ← Step 1
├── Dockerfile                 ← Step 1
├── requirements.txt           ← Step 1
├── init_db.sql                ← Step 1 — all PostgreSQL table definitions
├── main.py                    ← entry point — launches all 3 processes
├── config.py                  ← loads and validates strategy.json
│
├── scanner/
│   ├── __init__.py
│   ├── monitor.py             ← Step 3 — Process 1, Polygon REST poller
│   └── context.py            ← Step 3 — scanner_quartile + context field computation
│
├── feed/
│   ├── __init__.py
│   ├── universe.py            ← Step 5 — Universe manager
│   ├── context.py            ← Step 5 — TickerContext dataclass
│   └── signal_loop.py         ← Step 5 — per-ticker asyncio.Task
│
├── signals/
│   ├── __init__.py
│   ├── live_state.py          ← Step 5 — LiveSignalState wrapping backtest components
│   └── context_fetch.py       ← Step 4 — historical context fetch + Hawkes warmup
│
├── orders/
│   ├── __init__.py
│   ├── worker.py              ← Step 6 — Process 3, single order_queue consumer
│   ├── risk.py                ← Step 7 — RiskState
│   └── ibkr.py               ← Step 6 — IBKR execution wrapper (ib_insync)
│
├── db/
│   ├── __init__.py
│   ├── pool.py                ← Step 1 — asyncpg connection pool setup
│   ├── writer.py              ← Step 6 — async batch writer (1s flush, COPY protocol)
│   └── models.py             ← Step 1 — Python-side table column definitions matching init_db.sql
│
├── alerts/
│   ├── __init__.py
│   └── telegram.py            ← Step 8 — Telegram bot + kill switch
│
└── export/
    ├── __init__.py
    └── session.py             ← Step 9 — end-of-session parquet export
```

---

## Build Order — Follow This Exactly

Do not start a step before the previous one is complete and tested.

### Step 1 — Infrastructure

Files: `docker-compose.yml`, `Dockerfile`, `requirements.txt`, `init_db.sql`, `db/pool.py`, `db/models.py`

- `docker-compose.yml`: two services — `trading` and `db`. `trading` depends on `db`. All config via env vars.
- `Dockerfile`: Python 3.11+. Install requirements. No hardcoded paths.
- `requirements.txt`: `ib_insync`, `asyncpg`, `polygon-api-client`, `python-telegram-bot`, `numba`, `numpy`, `pandas`, `pyarrow`, `aiohttp`, `aiofiles`
- `init_db.sql`: Create all tables. **Use the exact schemas from `../live_system_architecture.md` Section 9.** Do not alter column names, types, or constraints. Tables: `strategies`, `scanner_snapshots`, `ticks`, `quotes`, `positions`, `orders`, `trades`, `sessions`, `hawkes_refits`, `signal_events`.
- `db/pool.py`: asyncpg connection pool. Single pool, shared across all consumers. `DB_URL` from env.
- `db/models.py`: Python-side column lists matching each table — used by the batch writer's COPY calls.

**Verify:** `docker-compose up` brings up both containers. DB init script runs clean. Python can connect to DB.

---

### Step 2 — Config

Files: `config.py`

- Load and validate `strategy.json`.
- Expose a single `Config` dataclass. All other modules import from here — no module reads `strategy.json` directly.
- Fail loudly on missing required fields. No silent defaults for trading parameters.
- Check `strategy.json` for any fields marked `"REQUIRED_FROM_BACKTEST"` — if any remain unfilled, raise at startup with a clear message identifying which fields need backtest values.

---

### Step 3 — Scanner Monitor (Process 1)

Files: `scanner/monitor.py`, `scanner/context.py`

**`scanner/context.py` — scanner_quartile algorithm:**

```python
def compute_scanner_context(qualifying: list[dict]) -> list[dict]:
    """
    qualifying: list of dicts with 'ticker' and 'pct_change' keys.
    Returns each dict augmented with: scanner_rank, scanner_n, scanner_heat,
    scanner_quartile, and the raw snapshot for storage.

    scanner_quartile algorithm (Phase G v2 — momentum-weighted):
      1. Sort descending by pct_change.
      2. total = sum(pct_change for all qualifying names)
      3. Walk accumulating running sum:
         - running < total/4  → Q1
         - running < total/2  → Q2
         - running < 3*total/4 → Q3
         - else               → Q4
      Q1 = dominant movers. Q4 = secondary. Gate: only Q3 and Q4 enter.
    """
```

**`scanner/monitor.py`:**
- Poll `GET /v2/snapshot/locale/us/markets/stocks/gainers` every `config.scanner.poll_interval_s` seconds.
- Filter: `todaysChangePerc >= config.scanner.gap_threshold` (0.30).
- Capture the **full qualifying snapshot** — not just the triggered ticker. All context fields require the full picture.
- Call `compute_scanner_context()` on the full list.
- **Gate: skip any ticker where `scanner_quartile` is 1 or 2. Do not pass to universe manager.**
- Write full snapshot JSON to `scanner_snapshots` table (one row per poll that has ≥1 qualifying name).
- For each ticker passing the gate: push `(ticker, scanner_context)` to the universe manager via a shared `asyncio.Queue`.
- `closed_today` set: if ticker was closed this session, do not re-add. Check before pushing.
- Log every gate rejection at DEBUG level with the quartile value.

**Verify:** Run against Polygon. Confirm context fields compute correctly. Confirm Q1/Q2 tickers are dropped. Confirm snapshot writes to DB.

---

### Step 4 — Historical Context Fetch

Files: `signals/context_fetch.py`

This step warms up the Hawkes model and EPG gate before live ticks arrive. It must complete in 2-4 seconds.

```
Two concurrent Polygon REST calls:
  GET /v3/trades/{ticker}?timestamp.gte={4am_et_ns}&limit=50000
  GET /v2/aggs/ticker/{ticker}/range/1/minute/{date}/{date}
```

**Exact behaviour:**

1. Fire both REST calls concurrently with `asyncio.gather`.
2. Replay all historical trades into `_hawkes_replay_with_refit` — reuse backtest code exactly.
3. Set `lambda_ref` from cold-start: `mu_buy + mu_sell` — **NOT the equilibrium formula**.
4. Replay `EventAnchor` + `ParticipationGate` through all historical ticks in timestamp order.
5. The resulting gate instance (not its params) is passed into `LiveSignalState`. `prev_state` must survive the historical→live boundary intact.
6. At handoff: drop any live tick with `sip_timestamp <= last_historical_ts`. Dedup is non-negotiable.

**Fallback tiers:**

| Historical trade count | Action |
|---|---|
| ≥ 1000 | Full replay — normal path |
| 100–999 | Use global `lambda_ref` fallback. Log DEGRADED. EPG gate starts from zero state. |
| < 100 | Proceed with caution. Log DEGRADED with count. |
| Timeout (> 5s) | Global `lambda_ref` fallback. EPG from zero. Log DEGRADED. |

**Write to `sessions` table** on context fetch completion: `context_fetch_ms`, `cold_start_n`, `degraded_mode`, `lambda_ref_global`, `lambda_ref_fitted`, `mu_buy_fitted`, `mu_sell_fitted`.

**Verify:** Time the fetch against a hot pre-market name. Target < 4 seconds total. Confirm dedup logic drops ticks at the boundary.

---

### Step 5 — Feed + Signal Loop (Process 2)

Files: `feed/context.py`, `feed/universe.py`, `feed/signal_loop.py`, `signals/live_state.py`

**`feed/context.py` — TickerContext dataclass:**
```python
@dataclass
class TickerContext:
    ticker: str
    queue: asyncio.Queue          # bounded — drop on full, log WARNING
    signal_state: LiveSignalState
    task: asyncio.Task
    state_ready: asyncio.Event    # set after context fetch completes
    scanner_context: dict         # scanner fields at trigger time
    closed_today: bool = False
```

**`signals/live_state.py` — LiveSignalState:**
- Wraps the backtest `EventAnchor`, `ParticipationGate`, and `SetupFilter` instances.
- Exposes `update_trade(tick)` and `update_quote(quote)` methods.
- Internally calls Hawkes update → EPG gate → EXIT_D → LULD check → setup filter update.
- Returns `SignalResult` on each tick: includes `order_signal` (None or an OrderRequest), plus current state snapshot for batch writing.
- **Never touches the DB or broker directly.** Returns data; caller decides what to do with it.

**Lee-Ready classification:**
- Use last known quote (bid/ask). No buffering.
- Trade at ask or above → BUY. Trade at bid or below → SELL. Between → tick test (sign of last price change).
- Accept occasional stale classification. This is the locked decision.

**`feed/signal_loop.py` — per-ticker task:**
```python
async def signal_loop(ctx: TickerContext, order_queue: asyncio.Queue):
    await ctx.state_ready.wait()   # block until context fetch done
    async for raw_msg in ctx.queue:
        if is_trade(raw_msg):
            result = ctx.signal_state.update_trade(raw_msg)
        else:
            result = ctx.signal_state.update_quote(raw_msg)

        # Accumulate for batch writer
        hot_ticks.append(...)
        hot_signal_events.append(...)

        if result.order_signal:
            order_queue.put_nowait(result.order_signal)

        heartbeat.update(ctx.ticker)   # dead man's switch
```

**Dead man's switch:** If no heartbeat update for > 30 seconds while a position is open → `order_queue.put_nowait(FlattenAllRequest())` immediately.

**LULD halts:** On halt detection, freeze signal state (stop processing ticks). Resume on first post-halt tick. Do not force-exit on halt alone.

**Pre-market soft halt:** No quote update for > 30 seconds → pause processing. Do not force-exit.

**Universe manager — ticker lifecycle:**
1. Ticker arrives from scanner queue.
2. Check `closed_today` — skip if true.
3. Fire `context_fetch` as an asyncio task. Set `state_ready` event on completion.
4. Create `TickerContext`. Start signal loop task.
5. Subscribe `T.{ticker}` and `Q.{ticker}` on Polygon WebSocket.

On ticker removal (session close):
1. Pop from universe dict.
2. Cancel signal loop task.
3. Unsubscribe from WebSocket.
**That order. Not reversed.**

**`feed/universe.py` — feed dispatch:**
```python
async def feed_dispatch(raw_message: dict):
    ticker = raw_message.get("sym")
    if ticker not in universe:
        return   # late message after unsubscribe — safe to drop
    ctx = universe[ticker]
    try:
        ctx.queue.put_nowait(raw_message)
    except asyncio.QueueFull:
        log.warning(f"{ticker}: queue full, dropping tick")
```

**Verify:** Feed dispatch drops late ticks cleanly. Heartbeat fires correctly. Dead man's switch triggers on simulated stall.

---

### Step 6 — Order Worker (Process 3)

Files: `orders/worker.py`, `orders/ibkr.py`, `db/writer.py`

**`orders/worker.py` — single asyncio consumer:**
```python
async def order_worker(order_queue: asyncio.Queue):
    async for request in order_queue:
        if not risk_state.allows(request):
            log.warning(f"Risk check blocked: {request}")
            continue
        fill = await ibkr.submit(request)
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                await write_order(conn, fill)
                await update_position(conn, fill)
        await telegram.notify_fill(fill)
```

**Order types by session:**
- Pre-market: marketable limit. Buy = ask + $0.01. Sell = bid - $0.01. `tif="EXT"`, `outside_rth=True`.
- RTH: market order. `tif="DAY"`.
- Cancel unfilled limit orders after 5 seconds. Do not chase. Do not resubmit.

**`db/writer.py` — async batch writer:**
- Wakes every 1 second.
- Swaps hot/cold buffers atomically.
- Uses `asyncpg` `copy_records_to_table` for ticks, quotes, and signal_events.
- Fills (orders, positions, trades) are written in the order worker via explicit transactions — never batched.

**Fills use explicit transactions — non-negotiable:**
```python
async with conn.transaction():
    await conn.execute("INSERT INTO orders ...", *values)
    await conn.execute("UPDATE positions ...", *values)
    # Raises on failure → auto rollback
```

**Verify:** Batch writer flushes correctly. Fill transaction rolls back on simulated DB error. Order cancellation fires after 5s.

---

### Step 7 — Risk Management

Files: `orders/risk.py`

```python
@dataclass
class RiskState:
    daily_pnl: float = 0.0
    open_positions: dict = field(default_factory=dict)
    max_daily_loss: float = -500.0   # from config
    max_concurrent: int = 1          # from config

    def allows(self, request: OrderRequest) -> bool:
        if self.daily_pnl <= self.max_daily_loss:
            return False
        if request.is_entry and len(self.open_positions) >= self.max_concurrent:
            return False
        return True
```

**`order_worker` is the only writer to `RiskState`.** Signal loops read it but never write it.

**IBKR position reconciliation on startup:**
1. Query IBKR for all open positions.
2. Sum per-ticker across all strategies from DB.
3. Compare. If any mismatch: log CRITICAL, halt, do not trade until manually reconciled.
4. This check runs before any order processing begins.

**Daily loss limit:** Once `daily_pnl <= max_daily_loss`, set a flag. Order worker rejects all new entries for the rest of the session. Log WARNING. Telegram alert.

**Verify:** Reconciliation mismatch halts correctly. Daily loss limit blocks new entries but allows exits.

---

### Step 8 — Alerting + Kill Switch

Files: `alerts/telegram.py`

**Telegram alerts — send for:**
- Entry filled (ticker, side, price, qty, session_bucket, scanner_quartile)
- Exit filled (ticker, exit reason, PnL %, PnL $)
- Daily PnL update (every hour during session, and on session close)
- Risk limit hit
- Degraded mode activated
- CRITICAL events (mismatch, dead man's switch, unhandled exception)

**Kill switch — two triggers:**
1. File watcher: if `live/kill.flag` exists at process startup or appears during runtime → execute kill sequence.
2. Telegram command: `/kill` → execute kill sequence.

**Kill sequence:**
```
1. Set kill flag in RiskState (blocks all new entries)
2. Cancel all open orders via IBKR
3. Flatten all open positions via market order
4. Wait for confirms (max 10s)
5. Log CRITICAL: KILL SWITCH ACTIVATED
6. Telegram: "KILL SWITCH: all positions flat, system stopping"
7. sys.exit(0)
```

**Verify:** Kill flag file triggers sequence. Telegram `/kill` triggers sequence. Sequence completes cleanly with no open positions.

---

### Step 9 — Session Export

Files: `export/session.py`

On session close (end of day or ticker removal):
- Query `ticks` and `quotes` tables for the session.
- Export to:
  ```
  data/filtered/{TICKER}_{DATE}_{MOM_PCT}/trades.parquet
  data/filtered/{TICKER}_{DATE}_{MOM_PCT}/quotes.parquet
  ```
- `MOM_PCT` = `intraday_pct_at_entry` from the session's first trade, formatted to 2dp (e.g. `0.42`).
- Column schema must match `filtered/` catalog exactly — backtest pipeline reads these files.
- Refer to `../Schema.md` for confirmed column names: `sip_timestamp`, `price`, `size`, `exchange`, `conditions` for trades; `sip_timestamp`, `bid_price`, `ask_price`, `bid_size`, `ask_size` for quotes.
- If no trades were taken, still export ticks and quotes — the data is useful for analysis.

**Verify:** Exported parquet files are readable by the backtest DuckDB ingest pipeline.

---

## Critical Concurrency Rules

These are non-negotiable. Violating them introduces race conditions that are hard to debug in live trading.

| Rule | Detail |
|---|---|
| Signal loops never touch broker or DB | They only call `order_queue.put_nowait()` and append to hot buffers |
| `order_worker` is the only `RiskState` writer | Signal loops may read, never write |
| `order_queue` is the single serialisation point | All order submissions go through it — no direct IBKR calls from signal loops |
| Universe lifecycle order on removal | Pop dict → cancel task → unsubscribe WS. Never reversed. |
| Fills use explicit transactions | `async with conn.transaction()` — no bare INSERT for fill records |
| Batch writer uses COPY, not INSERT | For ticks, quotes, signal_events |

---

## Locked Decisions — Do Not Revisit

These are decided. If you think one is wrong, flag it — do not silently change it.

| Decision | Value |
|---|---|
| Scanner gate | `scanner_quartile` Q3 and Q4 only. Q1 and Q2 are not traded. |
| `scanner_heat` | Collected and stored. Not the entry gate. |
| Setup filter threshold | Q̃ ≥ 0.65. Final. |
| Warmup gate | Q̃ ≥ 0.75 for first 65 bars |
| Lee-Ready | Last known quote, no buffering |
| Position sizing (paper v1) | Flat $1,000 RTH / $500 pre-market |
| EXIT_D pre-market | Disabled pre-market (`pre_market_override: false` in config) |
| Unfilled limit cancel | 5 seconds — do not chase |
| Timestamps | All nanoseconds UTC throughout. No timezone assumptions in data layer. |
| IBKR data | Execution quotes only. Not the primary feed. |
| Context fetch lambda_ref | `mu_buy + mu_sell` at cold start — NOT equilibrium formula |

---

## Phase H — Do Not Implement

These signals were identified in research but require explicit approval before any code:
- Multi-day runner gate
- TOD midday exclusion (11:30–13:30 ET)
- Rank × Heat combined filter

`multi_day_runner` is **collected and stored** in `sessions` and `scanner_snapshots`. It is not an entry gate.

---

## Known Failure Modes — Handle These

| Failure | Required Response |
|---|---|
| Context fetch timeout (> 5s) | Global `lambda_ref` fallback. EPG from zero state. Log DEGRADED. Continue. |
| LULD halt during position | Freeze signal state. Resume on first post-halt tick. Do not exit on halt alone. |
| Pre-market — no quote > 30s | Soft halt. Pause processing. Do not force-exit. |
| Queue full | `put_nowait` drops tick. Log WARNING. Bounded by design. |
| IBKR position mismatch on startup | Log CRITICAL. Halt. Do not trade until manually reconciled. |
| Dead man's switch | No heartbeat > 30s during open position → flatten all immediately. |
| Unfilled limit order | Cancel after 5s. Do not chase. Do not resubmit. |
| `dv=0` in setup filter | `τ_t = μ_τ(t-1)` — already handled in `setup_filter.py`. Do not add logic. |

---

## Key Numbers (For Reference)

- Mean qualifying scanner names per poll: **12.5**
- Typical hot pre-market tick volume (4am → 9:45am): **10,000–30,000 trades**
- Context fetch target time: **< 4 seconds** (1-2s REST + < 200ms Numba replay)
- EventAnchor fires **~30s before scanner trigger** — model is already mid-warmup at live handoff
- Phase G join rate: **94.6%**
- Strategy ID for this system: `"epg_v1"` — used in all strategy-tagged DB tables

---

## Environment Variables Required

```bash
# Database
DB_URL=postgresql://epg:password@db:5432/epg_live

# Polygon
POLYGON_API_KEY=

# IBKR
IBKR_HOST=127.0.0.1
IBKR_PORT=4002        # IB Gateway paper trading port
IBKR_CLIENT_ID=1

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Data export (mirrors backtest DATA_ROOT)
DATA_ROOT=/data
```

---

## PDT Rule

**Paper trading only until the PDT rule change.** The system is built for paper trading. Do not add live trading logic or remove paper trading constraints.

---

*Last updated: May 2026. Build against this file. Do not modify it during the build.*

---

## Import Notes

Added during pre-build housekeeping (2026-05-20). Updated when `setup_filter.py` and `Tradeable Setup Filter.md` were placed at repo root (2026-05-20).

**SetupFilter class does not exist.** The original spec listed `from setup_filter import SetupFilter, SetupFilterResult`. The public API exports only:
- `SetupFilterResult` — dataclass with pass/fail decision and signal trajectories
- `run_setup_filter(timestamps, prices, sizes, session_start_ns, session_end_ns, ...)` — the main entry point
- No `SetupFilter` class. `LiveSignalState` should call `run_setup_filter()` directly and hold the result.

**Canonical `setup_filter.py` lives at `backtest/setup_filter.py`.** Import from anywhere in the project as:

```python
from backtest.setup_filter import SetupFilterResult, run_setup_filter
```

`NS_PER_SECOND` is inlined as `1_000_000_000` in this file — no dependency on `data.schemas.mom_db`.

**sys.path requirements.** With repo root as working directory (Docker default, pytest default):

- `from backtest.setup_filter import ...` — works directly (repo root on path, `backtest/` is a package)
- No additional `sys.path` manipulation needed.

**backtest/runner.py imports trigger Numba JIT compilation.** `_hawkes_replay_with_refit` calls into `core.hawkes.engine` which uses Numba. Cold import takes 10–30 seconds the first time. Plan accordingly for live startup time.
