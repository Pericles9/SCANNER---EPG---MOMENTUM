# EPG Live — Staging Reference

Staged rollout from local development to paper trading. Complete each stage in order.
Do not skip a stage. Return to the previous stage on any blocker.

---

## Stage 1 — Local unit tests

**Goal:** Code is correct before any live connection is attempted.

```bash
# From repo root
D:\Trading Research\.venv\Scripts\python.exe -m pytest tests/ -v
```

All tests must pass. A failing test at this stage means a regression was introduced — fix before proceeding.

**Config state:** `strategy.json` unchanged from research values. `position_sizing.mode: "flat"`.

---

## Stage 2 — DB smoke test (Docker only)

**Goal:** PostgreSQL comes up, schema applies clean, Python can connect.

```bash
# Start DB container only
docker-compose up db -d

# Apply schema
docker exec -i epg_db psql -U epg -d epg_live < live/init_db.sql

# Verify tables exist
docker exec epg_db psql -U epg -d epg_live -c "\dt"
```

Expected: 10 tables listed (`strategies`, `scanner_snapshots`, `ticks`, `quotes`,
`positions`, `orders`, `trades`, `sessions`, `hawkes_refits`, `signal_events`).

Insert the strategy row once:
```sql
INSERT INTO strategies (id, name, version) VALUES ('epg_v1', 'EPG Momentum', '1.0');
```

**Config state:** `DB_URL` in `.env` points to local Docker container.

---

## Stage 3 — Scanner + feed dry run (no IBKR, no orders)

**Goal:** Scanner polls Polygon, context fetch works, signal loop fires without crashing.
No order submission. No IBKR connection required.

Temporarily stub out `IBKRClient` (comment out `ibkr.connect()` call in `main.py` and
replace `ibkr` with a mock that returns `equity=0` and empty positions). Run for 5 minutes
during pre-market to confirm:

- Scanner pulls names and writes `scanner_snapshots` rows
- Context fetch completes in < 5s for at least one ticker
- `ticks` and `quotes` rows accumulate in DB
- `signal_events` rows appear on state transitions
- No uncaught exceptions in logs

**Checklist:**
- [ ] `scanner_snapshots` rows written with correct `qualifying_n` and `snapshot_json`
- [ ] Context fetch logs `context_fetch_ms` < 5000
- [ ] Degraded mode logged if pre-market tick count < 100
- [ ] EPG gate transitions appear as `signal_events` rows
- [ ] No queue full warnings in first 5 minutes

**Config state:** `POLYGON_API_KEY` set. `IBKR_*` vars can be dummy values.
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` set for alert confirmation.

---

## Stage 4 — Full paper trading (IB Gateway paper account)

**Goal:** End-to-end paper trading. Real IBKR connection, real fills on paper account.

**Pre-flight checklist:**

- [ ] IB Gateway is running and logged in to paper account
- [ ] IB Gateway API connections enabled (port 4002 for paper)
- [ ] `IBKR_HOST`, `IBKR_PORT=4002`, `IBKR_CLIENT_ID` in `.env`
- [ ] Paper account has sufficient buying power (≥ $10,000 recommended)
- [ ] `kill.flag` does not exist in `live/`
- [ ] Telegram bot responds to `/status` before market open
- [ ] `strategy.json` `risk.max_daily_loss` set to a conservative value for first session
- [ ] `position_sizing.mode: "flat"` confirmed (Kelly disabled for first session)
- [ ] `risk.auto_kill_on_daily_loss: false` confirmed (manual kill preferred for first session)

**Session startup sequence:**
1. Start Docker: `docker-compose up -d`
2. Confirm DB healthy: `docker logs epg_db`
3. Run main: `docker logs -f epg_trading`
4. Watch for: "EPG live system started" Telegram message
5. Watch for: first scanner hit and context fetch log lines
6. Confirm first fill alert arrives via Telegram

**Intraday monitoring:**
- Telegram `/status` every 30–60 min
- `docker logs -f epg_trading | grep -E "(CRITICAL|ERROR|FILL|KILL)"`
- Check `open_positions` in `/status` matches IB Gateway positions blotter

**EOD:**
- Session auto-closes as tickers are removed from universe
- Check `sessions` table for `closed_ns` and `theoretical_equity_end` populated
- Check `data/filtered/` for parquet exports

**Config changes from Stage 3:**
- `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID` set to real values
- Stub IBKRClient removed from `main.py` (restored to production code)

---

## Stage 5 — Kelly sizing (after ≥ 20 paper trades)

**Goal:** Enable Kelly position sizing once enough trade history has accumulated.

**Pre-conditions:**
- ≥ 20 closed trades in the current session's `_trade_history` (in-memory)
- Reviewed paper trading PnL — strategy is working as expected
- Confirmed `theoretical_equity` is tracking sensibly

**Config change:**
```json
"position_sizing": {
    "mode": "kelly",
    "kelly_fraction": 0.1,
    "kelly_lookback_trades": 50,
    "kelly_min_sample": 20
}
```

Note: `_trade_history` resets each process restart. Kelly kicks in only after
`kelly_min_sample` trades accumulate in the current session. Before that, falls back
to flat notional automatically.

---

## Kill Switch Reference

| Trigger | Action |
|---------|--------|
| Telegram `/kill` | Executes kill sequence: cancel all orders → flatten all positions → sys.exit(0) |
| `touch live/kill.flag` | Same as above, detected within 5 seconds |
| `risk.auto_kill_on_daily_loss: true` | Auto-fires FlattenAllRequest when daily loss limit hit |
| Manual IB Gateway close-all | Bypasses system — reconcile DB manually before restarting |

After any kill: verify IB Gateway shows zero open positions before restarting.

---

## Environment Variable Checklist

| Variable | Stage 1 | Stage 2 | Stage 3 | Stage 4 |
|----------|---------|---------|---------|---------|
| `DB_URL` | — | required | required | required |
| `POLYGON_API_KEY` | — | — | required | required |
| `IBKR_HOST` | — | — | dummy OK | required |
| `IBKR_PORT` | — | — | dummy OK | required |
| `IBKR_CLIENT_ID` | — | — | dummy OK | required |
| `TELEGRAM_BOT_TOKEN` | — | — | required | required |
| `TELEGRAM_CHAT_ID` | — | — | required | required |
| `DATA_ROOT` | — | — | optional | required |
