# EPG Live Trading System — Architecture Reference

> Compiled from design sessions. Reference document for implementation.
> Strategy: EPG (Event Participation Gate) built on the Hawkes OFI foundation.
> Repo: EPG GitHub (current) | Scanner Hawkes OFI (legacy reference only)

---

## Table of Contents

1. [Decisions Locked In](#1-decisions-locked-in)
2. [High-Level Architecture](#2-high-level-architecture)
3. [Process Architecture](#3-process-architecture)
4. [Universe Management and Concurrency](#4-universe-management-and-concurrency)
5. [Historical Context Fetch](#5-historical-context-fetch)
6. [Order Management](#6-order-management)
7. [Risk Management](#7-risk-management)
8. [Pre-Market Specifics](#8-pre-market-specifics)
9. [Database Integration](#9-database-integration)
10. [Data Source](#10-data-source)
11. [Alerting and Kill Switch](#11-alerting-and-kill-switch)
12. [Paper Trading Criteria](#12-paper-trading-criteria)
13. [Cloud Migration Path](#13-cloud-migration-path)
14. [Open Items](#14-open-items)
15. [Phase G & G v2 Research Findings](#15-phase-g--g-v2-research-findings)

---

## 1. Decisions Locked In

| Decision | Choice | Notes |
|---|---|---|
| Broker | IBKR | Already configured. Use `ib_insync`. |
| PDT Rule | Blocked until rule change | Do not go live until resolved |
| Lee-Ready (live) | Simplest approach | Last known quote, no buffering. Accept occasional stale classification. |
| Scanner source | Polygon poller | `/v2/snapshot` every 15-30s, filter `todaysChangePerc >= 0.30` |
| Position sizing v1 | Flat $1,000 per trade | No sizing logic during initial paper trading phase |
| Position sizing v2 | 0.1 Kelly | After paper phase validates signal |
| Position sizing v3 | 0.25 Kelly | Once sufficient live trade sample exists |
| Alerting | Telegram bot | Simple, mobile-native, free |
| Kill switch | File-watch + Telegram command | See Section 11 |
| Paper trading exit criteria | Industry standard (see Section 12) | |
| Scanner heat filter | All quartiles (Q1–Q4) | scanner_quartile is computed and stored as analysis field only. No entry gate on quartile — all tickers with ≥30% gap enter. |
| EXIT_D pre-market regression | Treat as survivorship bias artifact, ignore | PF drop 1.73→0.90 is not a reason to disable |
| Data source | Polygon (maxed plan) | Everything: scanner, context fetch, live feed |
| IBKR market data subs | Pay for NYSE + NASDAQ | For order execution quotes only, not primary feed |
| Database | PostgreSQL (Docker) | Single DB for all strategies. Native concurrent writes. Cloud = change one env var. See Section 9. |

---

## 2. High-Level Architecture

The system is **local-first, Docker-containerised from day one** so cloud migration requires no code changes — only infrastructure.

```
┌─────────────────────────────────────────────────────────────┐
│                        PROCESS 1                            │
│                     Scanner Monitor                         │
│   Polls Polygon snapshot every 15-30s                       │
│   Filters ≥30% gap → pushes ticker to universe manager      │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                        PROCESS 2                            │
│              Feed + Signal (asyncio)                        │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  Universe Manager                                   │    │
│  │  Dict[ticker → TickerContext]                       │    │
│  │  Manages add/remove lifecycle                       │    │
│  └────────────────────┬────────────────────────────────┘    │
│                       │ per-ticker asyncio.Queue             │
│  ┌────────────────────▼────────────────────────────────┐    │
│  │  Per-Ticker Signal Loop (one asyncio.Task each)     │    │
│  │  Hawkes update → EPG gate → EXIT_D → LULD           │    │
│  └────────────────────┬────────────────────────────────┘    │
│                       │ order_queue (single shared)          │
└──────────────────────-┼─────────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────────┐
│                        PROCESS 3                            │
│                     Order Worker                            │
│  Single consumer of order_queue                             │
│  Risk checks → IBKR execution → PostgreSQL write              │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                   State + Persistence                       │
│  PostgreSQL (Docker container — all tables, all strategies) │
│  Async batch writer (1s flush) + atomic JSON state files    │
│  Telegram alerts + terminal dashboard                       │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Process Architecture

### Why asyncio (not threads or processes per ticker)

- Signal computation is <10µs per tick (Numba). Not the bottleneck.
- Bottleneck is I/O — Polygon WebSocket. asyncio handles this natively.
- Dynamic subscription management (add/remove tickers) is clean with asyncio tasks.
- 1-5 concurrent tickers is well within asyncio's capabilities.
- No GIL issues — Numba releases the GIL for compiled functions.

### Process 1 — Scanner Monitor

Polls Polygon snapshot every 15-30s. **Must collect the full qualifying snapshot — not just the triggered ticker** — because scanner context fields (heat, quartile, rank) require the full picture of all names active at that moment.

```python
# Poll Polygon snapshot endpoint
GET /v2/snapshot/locale/us/markets/stocks/gainers
# Filter: todaysChangePerc >= 0.30
# On hit:
#   1. Capture full snapshot of all qualifying names + their pct_change
#   2. Compute scanner context fields (see Phase G section 15)
#   3. Store snapshot to scanner_snapshots table
#   4. Push ticker + context to universe manager
# Interval: 15-30 seconds
```

**Scanner context fields computed at poll time (per triggered ticker):**

| Field | Definition |
|---|---|
| `scanner_rank` | Rank of this ticker by `pct_change` among all qualifying names (1 = fastest) |
| `scanner_n` | Count of qualifying names in this snapshot |
| `scanner_heat` | 75th percentile `pct_change` across all qualifying names |
| `scanner_quartile` | Momentum-weighted quartile (Phase G v2): sort by pct_change, walk cumulative sum — Q1 = first 25% of total momentum (dominant), Q4 = last 25% (secondary) |
| `multi_day_runner` | Boolean: did this ticker appear in the momentum catalog within the prior 5 calendar days |

Additional entry filters (pending Phase H validation — **not yet implemented**): minimum price, minimum pre-market volume, maximum float, rank gate, midday TOD exclusion.

> **Scanner quartile gate removed:** `scanner_quartile` is computed and stored as an analysis field. All Q1–Q4 tickers with ≥30% gap are admitted to the universe. No entry gate on quartile.

### Process 2 — Feed + Signal

Core asyncio event loop. Never does order execution or DB writes directly.

**Polygon WebSocket subscriptions:**
- `T.{ticker}` — real-time trades
- `Q.{ticker}` — real-time quotes

Subscriptions are added/removed dynamically as tickers enter/leave the universe.

**Feed dispatch:**
```python
async def feed_dispatch(raw_message: dict):
    ticker = raw_message.get("sym")
    if ticker not in universe:
        return  # late message after unsubscribe — safe to drop
    ctx = universe[ticker]
    try:
        ctx.queue.put_nowait(raw_message)
    except asyncio.QueueFull:
        log.warning(f"{ticker}: event queue full, dropping tick")
```

### Process 3 — Order Worker

Single asyncio consumer. The **only** coroutine that writes to risk state or touches the broker.

```python
async def order_worker():
    while True:
        request = await order_queue.get()
        await _execute_with_risk_check(request)
        order_queue.task_done()
```

---

## 4. Universe Management and Concurrency

### TickerContext

```python
@dataclass
class TickerContext:
    ticker: str
    queue: asyncio.Queue        # raw feed events, bounded (maxsize=5000)
    signal_state: LiveSignalState  # EPG gate, Hawkes, EXIT_D — per-ticker
    task: asyncio.Task          # the running signal coroutine
    state_ready_event: asyncio.Event  # set once historical replay is complete
    subscribed_at: float
    has_position: bool = False
```

### Universe Registry

```python
universe: Dict[str, TickerContext] = {}
closed_today: Set[str] = set()   # never re-add same ticker in same session
order_queue: asyncio.Queue = asyncio.Queue()
```

### Adding a Ticker

```python
async def add_ticker(ticker: str):
    if ticker in universe or ticker in closed_today:
        return

    state_ready = asyncio.Event()
    queue = asyncio.Queue(maxsize=5000)

    # Subscribe to feed immediately — events buffer while context loads
    await ws.send(json.dumps({
        "action": "subscribe",
        "params": f"T.{ticker},Q.{ticker}"
    }))

    task = asyncio.create_task(signal_loop(ticker, queue, state_ready))
    # Historical context fetch happens inside signal_loop before state_ready fires
    universe[ticker] = TickerContext(ticker, queue, None, task, state_ready, time.time())
```

### Removing a Ticker

Order of operations matters — pop from universe first, then cancel task, then unsubscribe. Late-arriving messages fall through the dispatch guard harmlessly.

```python
async def remove_ticker(ticker: str, reason: str):
    if ticker not in universe:
        return

    ctx = universe.pop(ticker)
    closed_today.add(ticker)

    ctx.task.cancel()
    try:
        await ctx.task
    except asyncio.CancelledError:
        pass

    await ws.send(json.dumps({
        "action": "unsubscribe",
        "params": f"T.{ticker},Q.{ticker}"
    }))
    log.info(f"Removed {ticker}. Reason: {reason}")
```

### Re-entry Same Ticker

`closed_today` blocks re-adds for the session. Same ticker, same session — do not re-add. The strategy edge is anchored to the initial gap event.

### Concurrency — The Critical Rules

1. **Signal loops never touch the broker or DB directly.** They push to `order_queue` only.
2. **`order_worker` is the sole writer to `RiskState`.** No locks needed — single consumer.
3. **`order_queue` serialises all order submissions.** Multiple concurrent tickers cannot race on position tracking.
4. **Per-ticker queue swap is the only coordination** between the feed dispatcher and signal loops.

---

## 5. Historical Context Fetch

When a ticker fires from the scanner it has been trading since 4:00am ET. Without historical replay, Hawkes cold-start hasn't run, `lambda_ref` is undefined, EventAnchor hasn't been tracking, and the EPG gate has no dollar volume history. The context fetch solves this.

### What to Fetch

Two concurrent REST calls at scanner fire time:

```python
async def fetch_historical_context(ticker: str, session_start_ns: int, now_ns: int):
    trades_task = asyncio.create_task(
        polygon_fetch_trades(ticker, session_start_ns, now_ns)
        # GET /v3/trades/{ticker}?timestamp.gte=...&timestamp.lte=...
        # &sort=timestamp&limit=50000 (paginate if needed)
    )
    bars_task = asyncio.create_task(
        polygon_fetch_1m_bars(ticker, session_start_ns, now_ns)
        # GET /v2/aggs/ticker/{ticker}/range/1/minute/{date}/{date}
    )
    return await asyncio.gather(trades_task, bars_task)
```

`session_start_ns` = 4:00am ET in nanoseconds UTC. Always — no assumption of RTH start.

### Replay Into Existing Code

The backtest's `_hawkes_replay_with_refit` is reused directly:

```python
def build_context_from_history(historical_trades, historical_bars, config):
    # Run Hawkes replay (same function as backtest)
    cold_start_params = _hawkes_replay_with_refit(
        t_sec=historical_trades.t_sec,
        sides=sides,
        ...
    )

    # Set lambda_ref: mu_buy + mu_sell from cold-start fit (NOT equilibrium formula)
    anchor = EventAnchor(lambda_ref=global_lref, k_multiplier=EPG_K)
    if cold_start_params is not None:
        anchor.set_lambda_ref(cold_start_params.mu_buy + cold_start_params.mu_sell)

    # Replay anchor + gate to find current EPG state
    gate = ParticipationGate(...)
    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], historical_trades.t_sec[i])
        if t_ev is not None:
            gate.activate(t_ev)
        dv = float(historical_trades.prices[i]) * float(historical_trades.sizes[i])
        gate.update(dv, historical_trades.t_sec[i])

    return LiveSignalState(
        hawkes_engine=HawkesEngine.from_params(cold_start_params),
        anchor=anchor,
        gate=gate,
        sides_buffer=sides[-REFIT_WINDOW:],
        setup_score=compute_setup_filter(historical_bars),
        last_historical_ts=historical_trades.timestamps[-1],
        trades_since_refit=N % REFIT_INTERVAL,
    )
```

### Dedup at the Handoff Boundary

The WS subscription starts before the REST fetch completes. Events accumulate in the queue. After replay, drop anything already covered:

```python
async def signal_loop(ticker, queue, state_ready_event):
    state = await state_ready_event.wait()  # blocks until replay done

    async for event in queue_iter(queue):
        if event["sip_timestamp"] <= state.last_historical_ts:
            continue  # already replayed
        state.process_live_event(event)
```

### Timing Reality

- Historical window at 9:45am scanner fire: 5h45m of trades
- Typical hot pre-market gap stock: 10,000–30,000 ticks in that window
- Polygon REST fetch: ~1-2 seconds
- Hawkes replay on 20,000 trades (Numba): <200ms
- **Total startup: ~2-4 seconds**

By the time the system is live, EventAnchor has already fired (~30s before scanner trigger) and you're 30+ seconds into the 300s EPG warmup. Nothing meaningful is missed.

### Fallback Tiers

| Condition | Action |
|---|---|
| ≥1,000 trades | Full replay, fitted `lambda_ref` |
| 100–999 trades | Partial cold-start, global `lambda_ref` fallback, log WARNING |
| <100 trades | Skip cold-start, global params, proceed with caution |
| REST fails / timeout | Global `lambda_ref`, EPG from zero state, log DEGRADED |

### Critical: Pass Gate Object, Not Parameters

After replay, pass the **gate instance** into `LiveSignalState` — not serialised parameters. The `prev_state` field is what makes the one-trade-per-window rule work correctly at the live/historical boundary. A reconstructed gate from parameters alone loses this.

---

## 6. Order Management

### IBKR Integration

Use `ib_insync`. TWS or IB Gateway must be running as a separate process on the same machine. `ib_insync` wraps the TWS API and handles reconnection.

### Order Types by Session

Pre-market and post-market **cannot use market orders**. Use marketable limits:

```python
async def build_order(request, quote, session_bucket):
    if session_bucket in ("pre_market", "post_market"):
        if request.side == "BUY":
            limit_price = round(quote.ask + 0.01, 2)
        else:
            limit_price = round(quote.bid - 0.01, 2)
        return BrokerOrder(
            order_type="LMT",
            limit_price=limit_price,
            tif="EXT",         # IBKR extended hours
            outside_rth=True
        )
    else:
        return BrokerOrder(order_type="MKT", tif="DAY")
```

**Unfilled limit orders**: cancel if unfilled after 5 seconds. Do not chase with a widening limit.

### Dead Man's Switch

If no heartbeat from the signal process for >30 seconds during a live position: cancel all open orders and flatten all positions immediately. Non-negotiable.

### Lee-Ready (Live)

Maintain `current_quote` updated on every `Q.{ticker}` message. For each incoming trade:

- `price > mid` → buy side
- `price < mid` → sell side  
- `price == mid` → tick test (compare to previous trade price)

Use last known quote always. No buffering for quote/trade arrival order. Accept that a small fraction of classifications will be slightly stale.

---

## 7. Risk Management

### RiskState

```python
@dataclass
class RiskState:
    daily_pnl: float = 0.0
    open_positions: Dict[str, float] = {}  # ticker → notional
    max_daily_loss: float = -500.0         # halt all trading for the day
    max_concurrent: int = 1                # start at 1, expand after Phase G
    max_notional: Dict[str, float] = field(default_factory=lambda: {
        "pre_market":     500.0,
        "regular_hours": 1000.0,
        "post_market":    500.0,
    })
```

**`order_worker` is the only writer to `RiskState`.** Never accessed from signal loops.

### Position Sizing Phases

| Phase | Method | Notes |
|---|---|---|
| Paper v1 | Flat $1,000 per trade | Validate signal, not sizing |
| Paper v2 | 0.1 Kelly | After paper phase complete |
| Live | 0.25 Kelly | Once live trade sample established |

Pre-market notional cap is tighter ($500) due to wider spreads and thinner depth.

### Daily Loss Limit

When `daily_pnl <= max_daily_loss`: halt all trading for the remainder of the session. Not just skip the next trade — full stop.

---

## 8. Pre-Market Specifics

The system operates pre-market (from 4:00am ET). No assumption of RTH-only operation anywhere in the architecture.

### What Changes Pre-Market

| Component | Pre-Market Behaviour |
|---|---|
| Order type | Limit orders only (marketable limit). No market orders. |
| LULD | `rth_only: true` in config. `luld_proximity.py` returns INACTIVE. Do not apply LULD logic pre-market. |
| Halt detection | Generic halt fallback needed: no quote update for >30s during position → soft halt, pause, do not force-exit immediately. Pre-market halts are exchange-discretion, no LULD formula. |
| EXIT_D | PF regression 1.73→0.90 treated as survivorship bias. EXIT_D not disabled pre-market. |
| Position size | $500 notional cap vs $1,000 RTH |
| Session bucket | `session_bucket()` in `runner.py` already handles `pre_market` / `regular_hours` / `post_market` correctly |

### Session-Conditional EXIT_D Config

```json
"exit_d": {
  "enabled": true,
  "theta": 0.65,
  "tau_min_sec": 4.0,
  "pre_market_override": {
    "enabled": false,
    "rationale": "Phase U regression pre-market. Treated as survivorship bias — monitor in paper trading."
  }
}
```

### Timestamps

All timestamps stored and processed as **nanoseconds UTC**. No timezone assumptions in data layer. Session boundaries are computed from ET offset (already implemented in `data/loaders/trades.py` with `_et_offset_ns()` and `_session_ns_bounds()`).

---

## 9. Database Integration

### Tool Choice: PostgreSQL in Docker

Single PostgreSQL container for all data, all strategies. Running in Docker locally means cloud migration is changing one environment variable (`DB_URL`) to point at RDS. No schema migration, no export step, no new code.

```yaml
# docker-compose.yml
services:
  trading:
    build: .
    depends_on:
      - db
    environment:
      - DB_URL=postgresql://epg:password@db:5432/epg_live

  db:
    image: postgres:16
    environment:
      POSTGRES_DB: epg_live
      POSTGRES_USER: epg
      POSTGRES_PASSWORD: password
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

**Why PostgreSQL over DuckDB for this system:**
- Multiple strategies writing concurrently — PostgreSQL MVCC handles this natively. DuckDB requires a coordinator workaround.
- Docker makes local setup trivial — no friction difference vs DuckDB.
- Cloud migration is one env var change to RDS. Battle-tested at every scale.
- Schema is identical SQL — no dialect translation if you ever need to switch.

**Fills use explicit transactions — non-negotiable:**
```python
async def write_fill(fill: FillRecord):
    async with conn.transaction():
        await conn.execute("INSERT INTO orders VALUES ($1...)", *fill.values())
        # raises on failure, transaction auto-rolls back
```

---

### Multi-Strategy Design

Market data (ticks, quotes) is strategy-agnostic — shared tables, no `strategy_id`. Everything else is tagged.

```sql
CREATE TABLE strategies (
    id           VARCHAR PRIMARY KEY,   -- e.g. 'epg_v1', 'momentum_v2'
    display_name VARCHAR,
    version      VARCHAR,
    config_json  TEXT,                  -- serialised strategy.json at deploy time
    deployed_at  BIGINT,                -- nanoseconds UTC
    active       BOOLEAN DEFAULT TRUE
);
```

Every strategy-specific table carries `strategy_id VARCHAR NOT NULL REFERENCES strategies(id)`.

**Feed dispatch for multiple strategies** — one WS subscription per ticker regardless of how many strategies are watching it. Broadcast the same tick to every strategy's queue:

```python
# Universe keyed by (strategy_id, ticker)
universe: Dict[Tuple[str, str], TickerContext] = {}

async def feed_dispatch(raw_message: dict):
    ticker = raw_message.get("sym")
    for (strat_id, sym), ctx in universe.items():
        if sym == ticker:
            try:
                ctx.queue.put_nowait(raw_message)
            except asyncio.QueueFull:
                log.warning(f"{strat_id}/{ticker}: queue full, dropping tick")
```

**IBKR position reconciliation** — IBKR aggregates positions by ticker, not strategy. Track per-strategy positions in DB. On startup, sum across strategies per ticker and reconcile against IBKR's reported aggregate. Halt and alert if they don't match.

---

### Schema — scanner_snapshots

One row per scanner poll that produces at least one qualifying ticker. Stores the full snapshot so scanner context fields can be recomputed or audited later. Required for Phase H validation and any future multi-strategy scanner analysis.

```sql
CREATE TABLE scanner_snapshots (
    id              BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    snapshot_ns     BIGINT      NOT NULL,  -- wall clock when poll ran (nanoseconds UTC)
    session_date    DATE        NOT NULL,
    n_qualifying    INTEGER,               -- count of names meeting ≥30% gap filter
    heat_p75        DOUBLE PRECISION,      -- 75th pct pct_change across all qualifying names
    snapshot_json   TEXT                   -- full JSON: [{ticker, pct_change, prev_close, ...}]
);
CREATE INDEX idx_snapshots_date ON scanner_snapshots (session_date);
```

### Schema — ticks

Strategy-agnostic. One row per incoming trade from Polygon WS.

```sql
CREATE TABLE ticks (
    ticker          VARCHAR     NOT NULL,
    session_date    DATE        NOT NULL,
    sip_timestamp   BIGINT      NOT NULL,   -- nanoseconds UTC
    price           DOUBLE PRECISION NOT NULL,
    size            INTEGER     NOT NULL,
    side            SMALLINT,               -- 1=buy, -1=sell, 0=unknown (Lee-Ready)
    session_bucket  VARCHAR                 -- 'pre_market'/'regular_hours'/'post_market'
);
CREATE INDEX idx_ticks_ticker_date ON ticks (ticker, session_date);
```

### Schema — quotes

Strategy-agnostic. One row per incoming quote from Polygon WS.

```sql
CREATE TABLE quotes (
    ticker          VARCHAR     NOT NULL,
    session_date    DATE        NOT NULL,
    sip_timestamp   BIGINT      NOT NULL,
    bid_price       DOUBLE PRECISION,
    ask_price       DOUBLE PRECISION,
    bid_size        INTEGER,
    ask_size        INTEGER,
    session_bucket  VARCHAR
);
CREATE INDEX idx_quotes_ticker_date ON quotes (ticker, session_date);
```

### Schema — positions

Per-strategy position tracking. IBKR sees aggregate; this table is the authoritative per-strategy view.

```sql
CREATE TABLE positions (
    strategy_id     VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker          VARCHAR     NOT NULL,
    session_date    DATE        NOT NULL,
    qty             INTEGER     NOT NULL,   -- positive=long, negative=short
    avg_entry_price DOUBLE PRECISION,
    open_ns         BIGINT,
    PRIMARY KEY (strategy_id, ticker, session_date)
);
```

### Schema — orders

One row per order submission. Written immediately on submit, updated on fill/cancel.

```sql
CREATE TABLE orders (
    id               BIGINT      PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id      VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker           VARCHAR     NOT NULL,
    session_date     DATE        NOT NULL,
    session_bucket   VARCHAR,
    submitted_ns     BIGINT      NOT NULL,
    filled_ns        BIGINT,
    side             VARCHAR     NOT NULL,  -- 'BUY'/'SELL'
    qty              INTEGER     NOT NULL,
    order_type       VARCHAR,               -- 'LMT'/'MKT'
    limit_price      DOUBLE PRECISION,
    fill_price       DOUBLE PRECISION,
    notional         DOUBLE PRECISION,
    status           VARCHAR,               -- 'PENDING'/'FILLED'/'CANCELLED'
    cancel_reason    VARCHAR,
    broker_order_id  VARCHAR,
    signal_reason    VARCHAR                -- 'EPG_RISING_EDGE'/'EXIT_D'/'EPG_CLOSE'/'HALT'
);
CREATE INDEX idx_orders_strategy_ticker ON orders (strategy_id, ticker, session_date);
```

### Schema — trades

One row per completed round-trip. Mirrors backtest `runner.py` trade record exactly.

```sql
CREATE TABLE trades (
    id                           BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id                  VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker                       VARCHAR     NOT NULL,
    session_date                 DATE        NOT NULL,
    session_bucket               VARCHAR,
    entry_order_id               BIGINT      REFERENCES orders(id),
    exit_order_id                BIGINT      REFERENCES orders(id),
    -- Timestamps
    entry_ns                     BIGINT,
    exit_ns                      BIGINT,
    entry_t_sec                  DOUBLE PRECISION,  -- seconds from 4:00am ET
    exit_t_sec                   DOUBLE PRECISION,
    hold_sec                     DOUBLE PRECISION,
    -- Prices and sizing
    entry_price                  DOUBLE PRECISION,
    exit_price                   DOUBLE PRECISION,
    qty                          INTEGER,
    pnl_pct                      DOUBLE PRECISION,
    pnl_dollar                   DOUBLE PRECISION,
    prev_close                   DOUBLE PRECISION,
    intraday_pct_at_entry        DOUBLE PRECISION,
    -- Entry context
    entry_type                   VARCHAR,           -- 'first_entry'/'re_entry'
    exit_reason                  VARCHAR,           -- 'exit_d'/'epg_window_close'/'luld_proximity'/'halt'/'manual'
    -- Signal state at entry
    epg_state_at_entry           VARCHAR,
    lambda_buy_at_entry          DOUBLE PRECISION,
    lambda_sell_at_entry         DOUBLE PRECISION,
    lambda_v_at_entry            DOUBLE PRECISION,
    lambda_v_peak_at_entry       DOUBLE PRECISION,
    cvd_at_entry                 DOUBLE PRECISION,
    exit_d_disabled              BOOLEAN,
    -- Phase G scanner context at entry (may differ from trigger if snapshot updated)
    scanner_rank_at_entry        INTEGER,
    scanner_quartile_at_entry    INTEGER,
    scanner_heat_at_entry        DOUBLE PRECISION,
    scanner_n_at_entry           INTEGER,
    -- Natural exit tracking
    natural_exit_ns              BIGINT,
    natural_exit_price           DOUBLE PRECISION,
    natural_exit_pnl_pct         DOUBLE PRECISION,
    natural_exit_reason          VARCHAR,
    -- Phase D watermark fields
    drawdown_from_window_high    DOUBLE PRECISION,
    current_window_high_at_entry DOUBLE PRECISION,
    prior_window_peak_at_entry   DOUBLE PRECISION
);
CREATE INDEX idx_trades_strategy_ticker ON trades (strategy_id, ticker, session_date);
```

### Schema — sessions

One row per strategy-ticker-session.

```sql
CREATE TABLE sessions (
    id                    BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id           VARCHAR     NOT NULL REFERENCES strategies(id),
    session_date          DATE        NOT NULL,
    ticker                VARCHAR     NOT NULL,
    scanner_snapshot_id   BIGINT      REFERENCES scanner_snapshots(id),
    scanner_fire_ns       BIGINT,
    prev_close            DOUBLE PRECISION,
    -- Phase G scanner context (at time of scanner trigger)
    scanner_rank          INTEGER,           -- rank by pct_change, 1=fastest
    scanner_n             INTEGER,           -- qualifying names in snapshot
    scanner_heat          DOUBLE PRECISION,  -- p75 pct_change across snapshot
    scanner_quartile      INTEGER,           -- 1=dominant, 4=secondary (v2 momentum-weighted)
    multi_day_runner      BOOLEAN,           -- in momentum catalog within prior 5 days
    -- Context fetch
    context_fetch_ms      INTEGER,
    cold_start_n          INTEGER,
    degraded_mode         BOOLEAN,
    -- Hawkes cold-start
    lambda_ref_global     DOUBLE PRECISION,
    lambda_ref_fitted     DOUBLE PRECISION,
    mu_buy_fitted         DOUBLE PRECISION,
    mu_sell_fitted        DOUBLE PRECISION,
    alpha_buy_fitted      DOUBLE PRECISION,
    alpha_sell_fitted     DOUBLE PRECISION,
    n_base_at_cold_start  DOUBLE PRECISION,
    n_refits              INTEGER,
    -- EPG
    t_event_ns            BIGINT,
    -- Setup filter
    setup_filter_score    DOUBLE PRECISION,
    setup_filter_passes   BOOLEAN,
    -- Close
    closed_ns             BIGINT,
    close_reason          VARCHAR
);
```

### Schema — hawkes_refits

One row per online refit. Required for signal state reconstruction post-session.

```sql
CREATE TABLE hawkes_refits (
    id               BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id      VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker           VARCHAR     NOT NULL,
    session_date     DATE        NOT NULL,
    refit_ns         BIGINT      NOT NULL,
    refit_n          INTEGER     NOT NULL,
    trades_at_refit  INTEGER,
    mu_buy           DOUBLE PRECISION,
    mu_sell          DOUBLE PRECISION,
    alpha_buy_self   DOUBLE PRECISION,
    alpha_sell_self  DOUBLE PRECISION,
    n_base           DOUBLE PRECISION,
    log_likelihood   DOUBLE PRECISION
);
```

### Schema — signal_events

One row per key state transition. Full session narrative without replaying.

```sql
CREATE TABLE signal_events (
    id                BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    strategy_id       VARCHAR     NOT NULL REFERENCES strategies(id),
    ticker            VARCHAR     NOT NULL,
    session_date      DATE        NOT NULL,
    event_ns          BIGINT      NOT NULL,
    event_type        VARCHAR     NOT NULL,
    -- 'T_EVENT_FIRE'/'EPG_WARMUP_START'/'EPG_PASS_OPEN'/'EPG_PASS_CLOSE'
    -- 'RISING_EDGE'/'EXIT_D_TIMER_START'/'EXIT_D_FIRE'
    -- 'LULD_PROXIMITY_FIRE'/'HALT_DETECTED'
    lambda_hat        DOUBLE PRECISION,
    lambda_ref        DOUBLE PRECISION,
    epg_state_before  VARCHAR,
    epg_state_after   VARCHAR,
    notes             VARCHAR
);
```

---

### Write Architecture — Signal Loop Never Touches DB

```
Signal Loop (per strategy, per ticker)
  ├── tick arrives      → hot_ticks.append(tick)          ← in-memory only
  ├── quote arrives     → hot_quotes.append(quote)         ← in-memory only
  ├── signal event      → hot_signal_events.append(event)  ← in-memory only
  └── order signal      → order_queue.put(request)

Order Worker (single consumer per strategy)
  ├── execute with broker
  ├── write fill → orders (explicit transaction)           ← immediate, ACID
  ├── update positions table
  ├── write refit → hawkes_refits
  └── write trade → trades (on exit confirmation)

Async Batch Writer (wakes every 1 second, shared across strategies)
  ├── swap hot/cold buffers
  └── COPY ticks, quotes, signal_events into PostgreSQL
```

PostgreSQL's `COPY` protocol is significantly faster than `INSERT` for bulk loads. Use `asyncpg` for the async driver — it supports `COPY` natively.

```python
async def batch_writer():
    global hot_ticks, cold_ticks
    while True:
        await asyncio.sleep(1.0)
        hot_ticks, cold_ticks = cold_ticks, hot_ticks
        if cold_ticks:
            await conn.copy_records_to_table(
                'ticks',
                records=cold_ticks,
                columns=['ticker','session_date','sip_timestamp','price','size','side','session_bucket']
            )
            cold_ticks.clear()
```

---

### Application Logs — Rotating Files (Not DB)

Goes to `logs/live_{date}.log` via `RotatingFileHandler`.

| Event | Level |
|---|---|
| System startup / shutdown | INFO |
| Polygon WS connect / disconnect / reconnect | INFO / WARNING |
| IBKR connect / disconnect | INFO / WARNING |
| Ticker added to universe (with strategy) | INFO |
| Ticker removed from universe + reason | INFO |
| Cold-start complete (n trades, lambda_ref_fitted) | INFO |
| Degraded mode activated | WARNING |
| Queue depth exceeded threshold | WARNING |
| LULD fallback rate >10% | WARNING |
| Dead man's switch triggered | CRITICAL |
| Kill switch activated | CRITICAL |
| Risk limit hit | WARNING |
| Order submitted / filled / cancelled | INFO |
| IBKR position reconciliation mismatch on startup | CRITICAL |
| Unhandled exception in signal loop | ERROR |

---

### Session Close → Parquet Export

Export to same directory structure the backtest pipeline reads. Uses pandas + psycopg — two extra lines vs DuckDB's native COPY, not a real cost.

```python
async def export_session_to_parquet(strategy_id, ticker, session_date, mom_pct):
    out_dir = DATA_ROOT / "filtered" / f"{ticker}_{session_date}_{mom_pct}"
    out_dir.mkdir(parents=True, exist_ok=True)

    df = await conn.fetch("""
        SELECT sip_timestamp, price, size
        FROM ticks
        WHERE ticker = $1 AND session_date = $2
        ORDER BY sip_timestamp
    """, ticker, session_date)

    pd.DataFrame(df, columns=['sip_timestamp','price','size'])\
      .to_parquet(out_dir / 'trades.parquet', index=False)
    # Same pattern for quotes.parquet
```

---

## 10. Data Source

**Polygon.io — maxed plan. Used for everything.**

| Use case | Endpoint |
|---|---|
| Scanner poller | `GET /v2/snapshot/locale/us/markets/stocks/gainers` |
| Historical trades (context fetch) | `GET /v3/trades/{ticker}?timestamp.gte=...&limit=50000` |
| Historical 1-min bars (setup filter) | `GET /v2/aggs/ticker/{ticker}/range/1/minute/{date}/{date}` |
| Live trades | WebSocket `T.{ticker}` |
| Live quotes | WebSocket `Q.{ticker}` |
| Previous close | `GET /v2/aggs/ticker/{ticker}/range/1/day/` |

### Why Not IBKR Data

`reqHistoricalTicks` returns 1,000 ticks per request with 2s mandatory pacing delay. A 30,000-tick context fetch takes ~60 seconds minimum. The context fetch must complete in 2-4 seconds. IBKR data alone cannot support this.

IBKR is used for **order execution only**. Pay for NYSE + NASDAQ non-professional subscriptions for execution quotes, not as a feed replacement.

### Why Not Databento

Usage-based pricing creates unpredictable costs on high-activity pre-market sessions. All existing data parsers are built around Polygon's field names (`sip_timestamp`, etc.) — switching requires rewriting infrastructure that is already correct.

---

## 11. Alerting and Kill Switch

### Alerting — Telegram Bot

Send alerts for:
- Position opened (ticker, side, price, notional, session bucket)
- Position closed (ticker, exit reason, P&L %, P&L $, hold time)
- Daily loss limit hit — trading halted
- Feed disconnect with open position
- IBKR connection lost
- Signal process heartbeat missed
- Cold-start degraded mode triggered

One bot, one private chat. Free, mobile-native, no infrastructure.

### Kill Switch

**Two mechanisms, both must flatten all positions and cancel all orders:**

1. **File watch**: System polls for existence of `HALT` file in the working directory. If found: cancel all, flatten all, write reason to log, exit cleanly.

```python
async def halt_watcher():
    while True:
        if Path("HALT").exists():
            log.critical("HALT file detected — flattening all positions")
            await emergency_flatten_all()
            Path("HALT").unlink()
            break
        await asyncio.sleep(1.0)
```

2. **Telegram command**: Bot listens for `/halt` command from authorised user ID only. Writes the HALT file, which the watcher picks up within 1 second.

---

## 12. Paper Trading Criteria

Based on industry standard practice. Do not go live before all are satisfied.

| Criterion | Threshold | Rationale |
|---|---|---|
| Minimum sessions | 20 trading sessions | Statistical minimum for signal validation |
| Signal accuracy | EPG gate transitions match backtest behaviour on same events (spot-check Hawkes λ, t_event timing) | Confirms live replay matches backtest |
| System stability | Zero critical errors over paper period | No feed drops, missed exits, orphaned positions |
| P&L direction | Directionally consistent with backtest expectations | Not necessarily equal — slippage, wider spreads expected |
| Cold-start coverage | >90% of sessions complete cold-start (≥1,000 trades) | Confirms data fetch latency acceptable |
| Degraded mode | Track degraded mode rate — investigate if >10% of sessions | Indicates systematic data fetch problem |

---

## 13. Cloud Migration Path

Designed for zero-code-change migration. Follow these rules from day one:

- **Docker Compose from the start** — trading container + PostgreSQL container, same `docker-compose.yml` locally and in cloud.
- **All config via environment variables** — `DB_URL`, broker keys, Polygon key, position limits, `DATA_ROOT`.
- **No hardcoded paths** — use `DATA_ROOT` env var (already in `data/schemas/mom_db.py`).
- **Cloud migration = one env var change** — point `DB_URL` at RDS instead of the local container. No schema migration, no data export, no new code.

```bash
# Local
DB_URL=postgresql://epg:password@db:5432/epg_live

# Cloud (AWS RDS example)
DB_URL=postgresql://epg:password@your-rds-endpoint.rds.amazonaws.com:5432/epg_live
```

When ready: spin up EC2 `t3.medium` (~$30/mo), push Docker image, provision RDS `db.t3.micro` (~$15/mo), update `DB_URL`. Done.

---

## 14. Open Items

| Item | Status | Notes |
|---|---|---|
| Max concurrent tickers | Informed by Phase G — start at 1 | Phase G: ~12.5 qualifying names/date. Phase H filters will reduce active universe. No hard cap in architecture — risk limits are the practical constraint. |
| Phase H scanner filters | Pending approval | Rank gate, heat gate, midday exclusion, multi-day runner preference — all Phase H candidates. None implemented. See Section 15. |
| EXIT_D pre-market monitoring | Paper trading | Log per-session whether EXIT_D fired pre-market vs RTH. Compare P&L distributions. |
| Unfilled limit order policy | Basic policy set (cancel after 5s) | May need refinement after observing pre-market fill rates in paper. |
| Kelly fraction estimation | Deferred | Need live trade sample before estimating edge probability distribution. |
| IB Gateway vs TWS | Not decided | IB Gateway preferred for headless server operation; TWS requires GUI. Resolve before cloud deploy. |
| Multi-day runner lookback | Pending Phase H | 5-calendar-day window used in Phase G. Confirm this is the right window before implementing as a live filter. |

---

## 15. Phase G & G v2 Research Findings

Analysis-only phases. **No findings are implemented.** All Phase H candidates require explicit approval before any code changes. This section is reference material for what the live system must collect and what Phase H will validate.

---

### Source Data

| Phase | Source | Trades | Dates |
|---|---|---|---|
| G | `results/phase_f/val_full/per_trade.parquet` | 6,004 | 80 sampled (14×2023, 66×2024) |
| G v2 | `results/phase_g/scanner_context.parquet` | 3,039 | Same 80 dates, scanner-joined subset |

Phase G join rate: **94.6%** (3,039 / matched trades had `scanner_rank` non-null). Meets ≥90% escalation threshold.

Phase G v2 `scanner_quartile` compute rate: **99.86%**. Meets ≥90% threshold.

**Mean qualifying names per date: 12.5** — informs max concurrent ticker expectations.

---

### Scanner Definition (Phase G)

"Daily-qualified": ticker achieved `q_tilde ≥ 0.65` streak of ≥15 bars at any point during session day. Once qualified, stays on the leaderboard for the full day. This produced 94.6% join rate. Strict "must be qualified at entry" produced only 19% — a structural limitation because entries happen before the streak accumulates in the first 20 minutes.

---

### G1 — Rank 1 (Fastest Mover) Underperforms

| Rank | n | EV | PF | 95% CI |
|---|---|---|---|---|
| 1 | 548 | +0.235% | 1.179 | [0.867, 1.655] |
| 2 | 429 | +0.753% | 1.621 | [0.974, 2.675] |
| 3 | 392 | +1.296% | 2.792 | [1.612, 4.618] |
| 4 | 277 | +1.005% | 2.882 | [1.495, 5.395] |
| 5 | 212 | +1.489% | 3.860 | [2.379, 6.623] |
| 9 | 83 | +1.672% | 6.038 | [2.569, 14.941] |

Rank 1 is the weakest entry: PF=1.18 vs PF=2.79–6.04 for ranks 3–9. The fastest-moving name at entry underperforms consistently. **Phase H candidate: rank filter avoiding rank 1.**

---

### G2 — Scanner Heat Monotonically Improves PF

Heat = 75th percentile `pct_change` across all qualifying names at snapshot time.

| Heat Bin | n | EV | PF |
|---|---|---|---|
| cold_Q1 | 719 | +0.514% | 1.457 |
| Q2 | 718 | +1.001% | 2.483 |
| Q3 | 718 | +1.036% | 2.375 |
| hot_Q4 | 719 | +1.162% | **2.616** |

Cold scanners (low overall momentum) produce PF=1.46. Hot scanners produce PF=2.62. The cold→Q2 step is the largest gap (+1.0 PF).

> **Scanner quartile gate removed:** All Q1–Q4 tickers with ≥30% gap are admitted. `scanner_quartile` retained as an analysis-only field in `sessions` and `trades` tables.

---

### G3 — LULD Upper Exits Cluster in Hot Scanners

| Segment | luld_upper share | vs population |
|---|---|---|
| Cold Q1 | 0.9% | 0.11x — strongly suppressed |
| Q2 | 10.2% | 1.19x |
| Q3 | 10.3% | 1.20x |
| Hot Q4 | 13.5% | **1.57x** |

High-value `luld_upper` exit (val-full PF=17.53, mean +4.63%) concentrates in hot scanner environments. Cold scanners essentially never produce parabolic LULD-ceiling exits. This explains much of the heat-vs-PF gradient.

---

### G4 — Multi-Day Runners Outperform Fresh Events

| Group | n | EV | PF | Win Rate |
|---|---|---|---|---|
| fresh_event | 2,018 | +0.834% | 1.935 | 48.9% |
| multi_day_runner | 1,021 | +1.141% | **2.757** | 49.6% |

Multi-day runners (ticker appeared in momentum catalog within prior 5 calendar days) show +0.31% higher EV and +0.82 PF gap. Win rate is nearly identical — the PF gap is driven by higher upside (more `luld_upper` fires). **Strongest standalone signal found in Phase G. Phase H candidate.**

---

### G5 — Time of Day: U-Shaped Intraday Pattern

**Top buckets:**

| Time (ET) | n | EV | PF |
|---|---|---|---|
| 09:30 | 241 | +2.315% | 3.109 |
| 09:40 | 171 | +1.729% | 2.948 |
| 15:40 | 50 | +1.756% | **6.038** |
| 14:50 | 52 | +1.562% | 5.074 |
| 14:00 | 52 | +1.302% | 4.031 |

**Worst buckets (near breakeven):**

| Time (ET) | n | EV | PF |
|---|---|---|---|
| 12:10 | 100 | +0.180% | 1.203 |
| 15:20 | 61 | +0.195% | 1.171 |
| 12:30 | 87 | +0.237% | 1.306 |
| 14:20 | 71 | +0.164% | 1.367 |

Open (9:30–10:00) and late-day (14:00–15:40) outperform. Midday (11:30–13:30) approaches breakeven. Matches known microstructure patterns. **Phase H candidate: midday exclusion window at cost of ~15–20% trade count.**

---

### G6 — Rank × Heat Interaction

Best combo: **Rank 3 + Hot Q4 = PF 6.46 (n=124)**. Only losing combo: Rank 2 + Cold Q1 = PF 0.75 (n=152). Rank 1 is flat across heat bins (PF ~1.20 in both — no heat sensitivity).

---

### G7 — Scanner Size: Weak Signal

Weak positive relationship between scanner size and performance. No strong actionable signal. Not a Phase H candidate.

---

### G8 — Entry Lag: Not Correlated

Correlation (time_of_day_sec × pnl_pct): 0.011. No actionable signal.

---

### Phase G v2 — Momentum-Weighted Quartile

Replaces `scanner_heat` population-level bins with `scanner_quartile` — a within-snapshot momentum-weighted quartile. Q1 = names accounting for the first 25% of cumulative snapshot momentum (dominant movers). Q4 = lowest relative momentum names in that snapshot.

**Algorithm**: sort qualifying names descending by `pct_change`, compute `total = sum(pct_change)`, walk accumulating `running` — assign Q1 until `running ≥ total/4`, then Q2 until `running ≥ total/2`, etc.

**GV2-1 — Monotone PF gradient Q1→Q4 (+1.806 PF spread):**

| Quartile | n | EV | PF |
|---|---|---|---|
| Q1 (dominant) | 697 | +0.296% | 1.252 |
| Q2 | 448 | +0.814% | 1.911 |
| Q3 | 476 | +1.280% | 2.517 |
| Q4 (secondary) | 1,249 | +1.194% | **3.058** |

Trading the dominant name (Q1) produces materially lower PF than secondary names. **Phase H candidate.**

**GV2-2 — Rank and quartile are confounded.** Rank 1 is Q1 in nearly all cases (544/544 trades). The rank 1 and Q1 underperformance are the same phenomenon expressed differently. `scanner_quartile` confirms and reframes Phase G's rank 1 finding — it does not reveal a new independent signal.

**GV2-3 — Q4 dominates the trade population (43%).** Most entries are already secondary names in their snapshot. The strategy is naturally positioned toward the better-performing segment.

**GV2-4 — EXIT_D fires disproportionately on Q1 (60% vs 39% for Q4).** Dominant movers have higher order-flow volatility and more mean-reverting momentum. Q4 names hold to EPG window close more often (53% vs 32% for Q1). Consistent with the rank 1 underperformance pattern.

**GV2-5 — LULD is not quartile-sensitive.** No quartile shows elevated LULD upper exits. LULD signal is orthogonal to momentum weighting within the snapshot.

---

### Phase H Candidates Summary

**None implemented. All require explicit approval before any code changes.**

| Signal | Phase G Finding | Phase G v2 Finding | Proposed Gate |
|---|---|---|---|
| Rank gate | Rank 1 PF=1.18 vs ranks 3–9 PF=2.79–6.04 | Q1 PF=1.25 confirms same signal | Avoid rank 1 at entry |
| Quartile gate | — | Q1→Q4 monotone +1.81 PF spread | Prefer Q3–Q4 entries |
| Heat gate | Cold Q1 PF=1.46, Hot Q4 PF=2.62 | — | **DECIDED — Q3/Q4 only. cold_Q1 and Q2 not traded.** |
| Multi-day runner | PF=2.76 vs 1.94 (+0.82 PF) — strongest standalone | — | Prefer tickers in momentum catalog within prior 5 days |
| TOD midday filter | 11:30–13:30 ET near-breakeven | — | Exclude midday window (~15–20% trade count cost) |
| Rank × Heat combo | Rank 3 + Hot Q4 = PF 6.46 (n=124) | — | Combined filter |

---

### What Phase G Means for Live Data Collection

The live system must collect and store all Phase G context fields at scanner trigger time so that Phase H validation can be run against live paper trading sessions. Fields already incorporated into `sessions` and `scanner_snapshots` schemas (Section 9):

- `scanner_rank`, `scanner_n`, `scanner_heat`, `scanner_quartile`, `multi_day_runner` in `sessions`
- Full snapshot JSON in `scanner_snapshots`
- `scanner_rank_at_entry`, `scanner_quartile_at_entry`, `scanner_heat_at_entry`, `scanner_n_at_entry` in `trades`

The entry-time fields matter because rank and quartile can shift between the scanner trigger (~30s before entry) and the actual entry tick if other qualifying names are moving faster.

---

*Document reflects architecture decisions as of May 2026. Update as Phase H proceeds.*
