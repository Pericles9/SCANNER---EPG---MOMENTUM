---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-07-02
phase: Tail-Risk · Structural Mitigation
reference_config: "val_r4_stratified · p=0.80 · no-gate baseline"
---

# Structural Mitigation — Consolidated Verdict (T7)

Three prediction-free structural mitigations tested on the R1-Final (p=0.80, no-gate) baseline,
all recomputed from a single canonical dataset (`raw_trajectories.json`, T1; max reconciliation
error 0.0%). Anti-overfitting rule honoured: every threshold is
diagnostic-derived or an explicit convention — none swept for PF/CVaR5.

**Chart:** [final_comparison_bar.html](charts/final_comparison_bar.html)

| config | n | PF | WR% | mean PnL% | CVaR5% | verdict |
|---|---|----|----|-----------|--------|---------|
| Baseline (no gate) | 65 | 1.7557 | 50.77 | 2.343 | -21.76 | reference |
| **T_gate=300** (R1.5-Final) | 65 | **1.9141** | 44.62 | 2.369 | **-14.53** | only candidate cutting the tail at acceptable cost |
| T3 — −9.59% MAE stop | 65 | 1.3871 | 47.69 | 1.368 | -19.69 | **NEGATIVE** |
| T4 — staged entry | 65 | 1.4742 | 49.23 | 1.538 | -24.2 | **NEGATIVE** |
| T5 — spread guard | — | — | — | — | — | **NULL** (T5a no signal, not tested) |

## Which candidate cut tail risk with minimal cost?

- **T3 disaster stop — abandon.** PF 1.7557→1.3871, mean 2.343→1.368,
  CVaR5 only +2.07pp. The MAE gap (T2) is real but a *mirage for a fixed stop*:
  MAE is transient — 7 losers dip through −9.59% and recover to shallower final
  losses, so the stop locks in ~−10% on recoverable trades and clips 2
  winners, destroying 107.0pp to rescue only 6 deep-tail trades.
- **T4 staged entry — abandon.** PF 1.7557→1.4742, and CVaR5 gets **worse**
  (-2.44pp). Confirmation is non-selective: winners confirm at 90.9%,
  losers at 93.8% (losers *more*). Deep losers bleed slowly (>60s), pass the checkpoint,
  and receive the second tranche — the mechanism adds to the tail while giving up EV on early winners.
- **T5 spread guard — abandon (null).** Entry spread does not separate winners from losers
  (MWU p=0.7379, effect 0.049); the deepest losses had the *tightest* spreads.

## Takeaway

All three prediction-free structural mitigations fail. The common thread across this project's
now-four failed tail attempts (SF-entry, ROC gate, volume-at-entry, and this trio) is that
**losers on these names are not distinguishable from winners at or shortly after entry — by price
level (MAE), by momentum confirmation (60s hold), or by liquidity (spread).** The only mechanism
that reduces CVaR5 without wrecking PF remains the **time-gate exit at 300s** (R1.5-Final), which
cuts on realized adverse P&L after a fixed clock rather than trying to predict. [[project-phase-state]]

No design decision made here. Cooper decides whether to adopt T_gate=300 and formally shelve the
three structural candidates.

## Escalation Check

| Condition | Result |
|---|---|
| Required field missing | CLEARED — T1 reconciles to baseline at 0.0% error |
| Threshold selected by sweeping for best PF/CVaR5 | CLEARED — stop=95th-pct winner MAE (diagnostic); staged checkpoint=60s (round number); spread guard not proposed (null) |
| T3 false-positive count > 1 | **FLAGGED** — FP=2 winners cut; reported openly, not hidden |
| T5a finds no spread difference | Triggered → T5b/c skipped, null reported |
