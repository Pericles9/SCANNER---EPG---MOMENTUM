# Phase FIX-PREVCLOSE — Summary

_Completed: 2026-06-29 22:08_

## What was fixed

**Bug:** `_try_prior_trades_parquet` (source 3 of prev_close fallback chain) used a 20:00 ET cutoff instead of 16:00 ET (RTH close). For 13/37 T1_POSTMARKET events, source 3 was the only resolver, causing inflated prev_close values and artificially high scanner thresholds.

**Fix:** Changed `_RTH_CLOSE_HOUR_ET = 20` → `_RTH_CLOSE_HOUR_ET = 16` in `backtest/data/loaders/prev_close.py`. One line.

## Impact on scanner catalog

- 40/86 old val events got earlier scanner hits (26) or later (14)
- 0 events went missing; 0 previously-missed events now found in old val
- Mid stratum pool expanded: 16 → 27 events available
- Val sample grew: 86 → 97 events (50/27/20 low/mid/high)

## Sweep results (corrected val sample, 97 events)

At p=0.65 (primary comparison arm):
- n_trades: 46 → 24
- Profit Factor: 2.6584 → 1.4308
- Win Rate: 54.35% → 41.67%
- Mean PnL: 8.27% → 1.15%
- CVaR5: -27.74% → -17.50%

**Escalation:** SOME CHECKS FAILED — see comparison_old_vs_corrected.md

## Sparse warmup residual (T11)

ANCHOR_LATE + WARMUP_AT_DEADLINE events at p=0.65: **58**

Sparse warmup is a SEPARATE issue — measured here but NOT fixed in this phase.

## Files produced

| File | Description |
|------|-------------|
| `backtest/data/loaders/prev_close.py` | Bug fix: 20h→16h RTH close cutoff |
| `backtest/tests/test_prev_close.py` | Regression tests (3 tests) |
| `backtest/data/val_r3_stratified.json` | Corrected val sample (97 events) |
| `backtest/results/phase_prevclose_fix/CHANGELOG.md` | Pre-fix documentation |
| `backtest/results/phase_r1_fixed_corrected/symmetric_sweep.json` | Corrected sweep results |
| `backtest/results/phase_r1_fixed_corrected/entry_audit_sparse_check.json` | T11 entry audit |
| `backtest/results/phase_r1_fixed_corrected/event_charts/p065/` | T12 per-event charts |
| `backtest/results/phase_prevclose_fix/spot_check_before.json` | T2 pre-fix spot check |
| `backtest/results/phase_prevclose_fix/spot_check_after.json` | T5 post-fix spot check |
| `backtest/results/phase_prevclose_fix/catalog_diff.json` | T8 catalog diff |

## Next steps (pending Cooper review)

- R1-Final: Select final p_close from corrected sweep results
- R2: SF entry tuning
- Sparse warmup fix (ANCHOR_LATE / WARMUP_AT_DEADLINE) — separate phase