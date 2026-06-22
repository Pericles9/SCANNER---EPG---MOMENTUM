# Scanner Hit Floor Fix — C0 Summary

**Date:** 2026-06-22  
**Status:** COMPLETE — ESCALATION TRIGGERED (PF < 1.30 hard stop)  
**Approval required before:** any R0 re-run or R1 work

---

## What Changed

The EPG-Rapid runner processes all session trades from 4am to warm the Hawkes model.
The Hawkes anchor fires on the RTH open intensity surge, not on price momentum. In 65.4%
of val-split events the anchor and gate warmup complete BEFORE the stock reaches the 30%
threshold that triggers the live scanner.

A hard floor was added: no tick before `t_scanner_hit_sec` (the first trade where
`price >= prev_close * 1.30`) is eligible for entry. Events where price never crosses
the threshold (scanner never fires) are skipped entirely.

---

## Files Changed

| File | Change |
|------|--------|
| `backtest/scripts/build_scanner_hit_catalog.py` | New — builds `data/filtered/scanner_hit_catalog.json` |
| `data/filtered/scanner_hit_catalog.json` | New — 6395 entries, val-split scanner timestamps |
| `backtest/runner_rapid.py` | Floor guard, catalog load, new diagnostic fields |
| `backtest/docs/EPG_Rapid_Strategy (1).md` | §1, §2, §3 updated; §3.0 new section |
| `backtest/docs/EPG_Rapid_Test_Phases (1).md` | C0 section, R0/R1 invalidation notes, cross-phase note |
| `backtest/results/phase_r0/INVALIDATED.md` | New — marks pre-fix R0 results invalid |
| `backtest/results/phase_r1/INVALIDATED.md` | New — marks pre-fix R1 results invalid |
| `backtest/results/phase_r0/rapid_r0/run_summary.json` | `_invalidated: true` injected |
| `backtest/results/phase_r0/baseline_r0/run_summary.json` | `_invalidated: true` injected |
| `backtest/results/phase_r1/symmetric_sweep.json` | `_invalidated: true` injected |
| `backtest/results/phase_r1/asymmetric_sweep.json` | `_invalidated: true` injected |

---

## XBP 2023-12-04 Verification (A3)

Single-event verify. Before fix: entry at 9:35:13 ET, stock DOWN 4.4%, scanner not hit
until 10:11:59 ET (`entry_lag_from_scanner_hit = −2,207s`). After fix: entry at 10:21:55 ET
(595.6s after scanner hit). Gate was FAIL at scanner hit; entry fired on next PASS tick.

See `xbp_verification.json`.

---

## A4 Post-Fix Baseline (100-event val, seed=42)

| Metric | Pre-fix (INVALID) | Post-fix |
|--------|-------------------|----------|
| n_events_processed | 83 (of 100) | 55 (of 100) |
| n_events_skipped | — | 44 (`no_scanner_hit` + `missing_prev_close` + other) |
| n_trades | 83 | 52 |
| Profit Factor | 2.077 | **0.8277** |
| Win% | 50.6% | 48.08% |
| CVaR5 | −17.58% | −22.15% |
| mean_entry_lag_from_scanner | — (invalid) | 1,750s (median 6.9s, p90 5,010s) |
| pct_events_gate_PASS_at_scanner_hit | — | 41.8% |

### Escalation

**PF=0.8277 < 1.30 hard-stop threshold.** The scanner floor reveals that the prior
PF=2.077 was materially inflated by pre-scanner entries — trades entered before the stock
reached the 30% threshold, which is impossible in live trading.

41.8% of events had the gate in PASS state at the first scanner hit tick, meaning entry
fired essentially immediately after the threshold was crossed. Median lag from scanner hit
is only 6.9s. The p90 (5,010s) reflects chatter events where the gate had cycled to FAIL
by scanner hit and the next PASS came much later.

Pre-market is worse than RTH both before and after the fix:
RTH PF=0.9627, pre-market PF=0.6714.

---

## Diagnostic Fields Added to runner_rapid.py

**Per-trade:** `entry_lag_from_scanner_sec` — seconds from scanner hit to entry tick
(positive = after scanner hit, now always ≥ 0 by construction of the floor).

**Event-level:** `gate_at_scanner_hit` — gate state name at the first tick at or after
scanner hit (PASS/FAIL/WARMUP/INACTIVE).

**Run summary:** `mean_entry_lag_from_scanner_sec`, `median_entry_lag_from_scanner_sec`,
`p90_entry_lag_from_scanner_sec`, `pct_events_gate_pass_at_scanner_hit`.

---

## Approval Gate

Do NOT re-run R0 or R1 until Cooper has reviewed this summary and given explicit direction.
The escalation (PF < 1.30) requires strategy-level consideration before proceeding.
