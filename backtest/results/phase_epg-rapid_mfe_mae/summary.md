# Phase DIAG-MFE-MAE — Summary

**Date:** 2026-06-24
**Source:** R1-Fixed p=0.65 arm — 46 trades, all `epg_window_close` exits, actual PF=2.6584, actual CVaR5=-27.74%.
**Analysis-only.** No backtest run, no parameter/code change. Per-event chart requirement waived (Agent_Prompt_Standard.md §7).

## Key stats

- N trades = 46; wins = 25; losses = 21.
- MAE: mean 11.29% / median 10.34% (raw); mean 0.475 / median 0.446 (gap fraction).
- MFE: mean 23.29% / median 11.11% (raw); mean 0.961 / median 0.468 (gap fraction).
- Mean t_MAE_relative = 0.336; mean t_MFE_relative = 0.401 (0=entry, 1=exit).

## MFE-before-MAE rate

- All trades: 43.5% saw their MFE before their MAE.
- Losses only: 81.0% of losing trades peaked before their worst tick.

## Recommended R1.5 sweep ranges (data-derived only — Cooper selects)

Lowest candidate level where false-stop rate (fires on a winner) first drops below 25%:
- **Option 1 (scanner-anchored):** none below 25%
- **Option 2 (gap-retracement):** X=0.50 (false 24%, true 48%, PFΔ -0.901)

Full per-level false/true-stop and simulated-PF table: `stop_simulation.json` / Chart 5.

## Edge cases / exclusions

- T1c: none — all 46 trades have gap_move > 0; all gap-fraction metrics valid.
- T2a: none — every hold window contained ticks.
