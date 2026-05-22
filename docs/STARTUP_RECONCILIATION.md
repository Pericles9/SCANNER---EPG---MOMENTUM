# Startup Position Reconciliation — Runbook

## What It Is

At startup, `live/main.py` calls `_reconcile_positions()` before any trading begins.
It compares IBKR open positions to the `positions` table in the DB for today's session.
A mismatch halts the system immediately.

## Why It Halts

The DB is the authoritative record of trades taken this session. If IBKR shows positions
that the DB doesn't know about (or vice versa), trading on stale state risks adding to a
position we think is flat, or exiting a position we think doesn't exist.

The halt is non-negotiable by default. The escape hatch is `SKIP_POSITION_CHECK=true`,
which downgrades the halt to a warning and seeds IBKR positions as truth.

---

## Interpreting the Log

A mismatch produces a CRITICAL block like:

```
CRITICAL  live.main: POSITION MISMATCH DETECTED
  Mismatched tickers : TSLA, NVDA
  IBKR qty           : {'TSLA': 100, 'NVDA': 0}
  DB qty             : {'TSLA': 0, 'NVDA': 50}
  Resolution         : reconcile manually, then restart with SKIP_POSITION_CHECK=true
  Runbook            : docs/STARTUP_RECONCILIATION.md
```

- **IBKR qty > 0, DB qty = 0** — IBKR holds a position the DB has no record of.
  Likely cause: yesterday's position was not closed, or a manual trade was placed.
- **IBKR qty = 0, DB qty > 0** — DB shows a position that IBKR no longer holds.
  Likely cause: IBKR filled an exit that wasn't recorded (process crashed mid-fill),
  or the position was manually closed in TWS.

---

## Resolution Procedures

### Case A — Stale paper positions from a prior session

The paper account has leftover positions that were never closed (e.g., the process
crashed before market close yesterday).

1. Open TWS / IB Gateway.
2. Manually close all open positions in the paper account.
3. Verify the `positions` table is empty for today's date:
   ```sql
   SELECT * FROM positions WHERE session_date = CURRENT_DATE;
   ```
4. If DB rows exist for today, delete them:
   ```sql
   DELETE FROM positions WHERE session_date = CURRENT_DATE;
   ```
5. Restart the container normally (no flag needed).

### Case B — Process crashed mid-fill (DB behind IBKR)

IBKR executed a fill but the DB transaction did not complete before the crash.

1. Identify the ticker(s) in the mismatch log.
2. Check IBKR execution history for today's fills on those tickers.
3. Manually insert the missing position row:
   ```sql
   INSERT INTO positions (strategy_id, ticker, session_date, qty, avg_entry_price, open_ns)
   VALUES ('epg_v1', 'TSLA', CURRENT_DATE, 100, 245.50, extract(epoch from now()) * 1e9);
   ```
   Use the actual fill price and qty from IBKR execution history.
4. Restart normally.

### Case C — Minor discrepancy, need to trade now

If you have verified the IBKR position is correct and simply want to proceed:

1. Set `SKIP_POSITION_CHECK=true` in the `.env` file or docker-compose environment.
2. Restart: `docker compose up -d trading`
3. The system will log a WARNING, seed IBKR positions as truth, and continue.
4. **Remove `SKIP_POSITION_CHECK` after the session.** It must not persist across sessions.

---

## Prevention

- Always use the kill switch (`/kill` or `live/kill.flag`) to stop the system.
  This flattens all positions before exit.
- Do not manually close positions in TWS while the live system is running.
- Do not kill the container with `docker kill` or `docker stop` while a fill is in flight.
  Use `/kill` first, wait for "all positions flat" confirmation, then stop the container.

---

## DB Queries for Diagnosis

```sql
-- All open positions for today
SELECT * FROM positions WHERE session_date = CURRENT_DATE;

-- Recent fills
SELECT ticker, side, fill_price, filled_qty, status, filled_ns
FROM orders
WHERE session_date = CURRENT_DATE
ORDER BY filled_ns DESC
LIMIT 20;

-- Any trades closed today
SELECT ticker, entry_price, exit_price, pnl_dollar, exit_reason
FROM trades
WHERE session_date = CURRENT_DATE
ORDER BY exit_ns DESC;
```
