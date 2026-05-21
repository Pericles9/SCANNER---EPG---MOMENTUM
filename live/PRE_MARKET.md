# Pre-Market Checklist

Run this every trading morning before 4:00am ET.
Market open is 9:30am ET. Pre-market activity starts 4:00am ET.

---

## 3:45am — Infrastructure

- [ ] Confirm IB Gateway is running and logged in to your paper account.
      If it is not running, start it now. It must be fully connected before
      the trading container starts. Login takes 30–60 seconds.

- [ ] Confirm Docker is running.
      `docker ps` should show the `db` container up.
      If not: `docker-compose -f live/docker-compose.yml up -d db`

- [ ] Confirm no kill flag exists.
      `ls live/kill.flag` — should return "No such file or directory".
      If it exists: `rm live/kill.flag`

---

## 3:50am — Smoke Test

Run the smoke test. All 6 checks must pass before starting the trading container.

```bash
python live/smoke_test.py
```

If any check fails, do not start trading. Fix the issue and re-run.
Common failures and fixes are printed by the smoke test itself.

**Note on Check 6 (Polygon WebSocket):** Before 4:00am ET, no SPY ticks will flow.
The check may show a timeout warning — this is expected. Verify checks 1–5 pass,
then re-run after 4:00am ET to confirm the WebSocket feed is live.

---

## 3:55am — Start Trading Container

```bash
docker-compose -f live/docker-compose.yml up trading
```

Watch the startup logs for these lines in order:

| Log line | Means |
| -------- | ----- |
| `DB migration applied` | migrate_v1.sql ran clean |
| `Numba warmed` | Numba JIT compilation complete |
| `IBKR connected — equity: $X` | Account equity loaded |
| `Polygon WebSocket connected` | Feed is live |
| `Scanner monitor started` | Polling Polygon every 20s |
| `EPG live system started — YYYY-MM-DD — paper trading` | System ready |

If any of these do not appear within 90 seconds, check logs and fix before 4:00am ET:

```bash
docker logs live_trading_1 --tail 50
```

---

## 4:00am — Confirm Feed Is Live

Send `/status` to the Telegram bot.

Expected response includes:

- `Daily PnL: $0.00`
- `Loss limit: OK`
- `Open positions (0):`
- `Trades today: 0 | realized PnL: $0.00`

If the response shows `Loss limit: HIT` — a previous session hit the daily loss limit.
Check whether `auto_kill_on_daily_loss` fired and whether any positions remain open in IB Gateway.

---

## During the Session

- The system runs unattended. You do not need to watch it.
- Send `/status` any time to check state.
- Send `/kill` only to flatten all positions and stop the system immediately.
- To place `kill.flag` manually: `touch live/kill.flag`
- Hourly PnL summary arrives automatically via Telegram.

---

## End of Session (4:00pm ET)

The system closes sessions and exports parquet files as tickers are removed from the universe.
Check Telegram for the final daily PnL summary message.

Verify session exports landed:

```bash
ls $DATA_ROOT/filtered/
```

Verify DB session records are closed:

```sql
SELECT ticker, closed_ns, close_reason, theoretical_equity_end
FROM sessions
WHERE session_date = CURRENT_DATE
ORDER BY id;
```

All rows should have a non-null `closed_ns`.

---

## If Something Goes Wrong

**System not responding to Telegram:**

```bash
docker ps                                     # confirm container is running
docker logs live_trading_1 --tail 100         # check for exceptions
```

**Position open but system crashed:**

1. Log in to IB Gateway and close the position manually.
2. Do not restart the trading container until IB Gateway shows zero open positions.
3. On next startup, the reconciliation check will flag any DB/IBKR mismatch.
   Resolve manually before the system will accept new orders.

**Emergency flatten without Telegram:**

```bash
touch live/kill.flag    # kill_flag_watcher detects within 5 seconds
```

System executes: cancel all orders → flatten all positions → sys.exit(0).
Confirm IB Gateway positions blotter shows flat before restarting.

**Numba warmup taking too long (> 2 minutes):**

This happens on the first cold start after a Docker image rebuild. Normal on first run.
Subsequent starts reuse cached bytecode and complete in seconds.
If it hangs indefinitely: `docker restart live_trading_1`

**DB migration fails at startup:**

```bash
docker logs live_trading_1 | head -20    # look for psql error
psql $DB_URL -f live/db/migrate_v1.sql  # run manually and check output
```

Common cause: DB_URL env var not set or DB container not healthy yet.
