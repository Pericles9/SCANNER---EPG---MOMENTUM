# Startup Crash Recovery — Runbook

> **Superseded:** the old DB-vs-IBKR mismatch *halt* (`_reconcile_positions`) and the
> `startup_position_triage` resume/watch logic have been removed. There is now **one**
> startup path: `live/recovery/crash_recovery.py`. The `SKIP_POSITION_CHECK` env var is
> gone — there is nothing to skip.

## What It Is

At startup, after IBKR connects but before any signal loops or scanner polling begin,
`live/main.py` calls `run_crash_recovery(ib, pool, telegram, session_date)`.

A PC / process crash is functionally identical to a dead man's switch trigger, so the
recovery posture is the same: **cancel everything, flatten everything, start clean.**

There is deliberately **no smart resume**. EPG windows are 30–120s; Docker + IBKR +
Polygon reconnect time almost always outlives any live window, and resuming a position
with stale Hawkes state is more dangerous than taking the loss.

## What It Does

1. **Cancel all open orders** at IBKR (`reqAllOpenOrders` → per-order `cancelOrder` with
   5s confirmation; `reqGlobalCancel` as a backstop). The DB is not trusted for order
   state after a crash.
2. **Fetch live IBKR positions** (`reqPositions`, equities, non-zero) — the ground truth.
3. **Flatten every position:**
   - `regular_hours` → market order, `tif=DAY`.
   - `pre_market` / `post_market` → marketable limit, `tif=EXT`, `outsideRth=True`,
     crossing the spread (`bid-0.01` long / `ask+0.01` short), widening to `0.05` then
     `0.10` on each failed 30s/30s/60s attempt.
   - **Outside trading hours** (market closed) → no order; ticker is **DEFERRED** for
     manual review and an alert fires immediately.
   - Never fills after the full ladder → **STUCK**; an exception → **ERROR**.
4. **Reconcile the DB to flat** in one transaction: zero every `positions` row for the
   strategy, mark dangling `PENDING` orders `CANCELLED` (`cancel_reason='CRASH_RECOVERY'`).
5. **Audit row** per affected ticker in `signal_events`
   (`event_type='CRASH_RECOVERY_CLOSE'`, JSON notes with qty / fill / order_type /
   session_bucket / reprice_count / final_status). IBKR orphans (no DB row) are recorded
   with `strategy_id='UNKNOWN'`.
6. **One summary Telegram alert** (🟡 clean, 🔴 if anything deferred/stuck/errored).
   Clean starts (nothing open) send nothing.

## Caller Behaviour (`main.py`)

| Result | Action |
|---|---|
| `stuck_tickers` or `error_tickers` non-empty | Log CRITICAL, alert, **halt startup** (`sys.exit(1)`). Manual resolution required. |
| `deferred_tickers` non-empty | Log WARNING, continue; tickers added to `risk_state.manual_review_required`. |
| `closed_tickers` only | Log WARNING, continue normally. |
| `had_open_positions = False` | Log INFO "clean start", continue. |

> **Idempotency:** DB rows are zeroed even for DEFERRED/STUCK positions still held at
> IBKR. That's intentional — the automated system disclaims them (alert + manual-review
> flag), and on the next restart IBKR (the ground truth) re-surfaces the position and
> recovery flattens it. Zeroing the DB never loses a position because the broker is
> re-queried on every startup.

---

## Manual Resolution (STUCK / ERROR halt, or DEFERRED review)

1. Open TWS / IB Gateway and inspect the flagged ticker(s).
2. **STUCK / ERROR:** the broker still holds the position. Either close it manually in
   TWS, or — if the market is open and liquidity has returned — simply restart the
   container; crash recovery will retry the flatten automatically.
3. **DEFERRED:** the market was closed at startup. When it next opens, restart the
   container (recovery re-runs and flattens), or close manually in TWS.
4. There is no DB surgery required: crash recovery already reconciled the DB to flat.

---

## Prevention

- Prefer the kill switch (`/kill` or `live/kill.flag`) to stop the system — it flattens
  before exit. But an unclean stop (`docker kill`, power loss) is now safe: crash
  recovery handles it on the next start.
- Avoid manually closing positions in TWS while the live system is running.

---

## DB Queries for Diagnosis

```sql
-- Positions (should all be qty=0 after recovery)
SELECT * FROM positions WHERE strategy_id = 'epg_v1' AND qty != 0;

-- Crash recovery audit trail
SELECT ticker, event_ns, notes
FROM signal_events
WHERE event_type = 'CRASH_RECOVERY_CLOSE'
ORDER BY event_ns DESC
LIMIT 20;

-- Orders cancelled by crash recovery
SELECT ticker, side, status, cancel_reason
FROM orders
WHERE cancel_reason = 'CRASH_RECOVERY'
ORDER BY submitted_ns DESC
LIMIT 20;
```
