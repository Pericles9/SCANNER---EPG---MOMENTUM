# Phase VAL-FULL — T5 Direct Comparison (Generalization Verdict)

**Verdict: DEGRADED (>30% relative PF drop — flag)**  (PF relative change -42.0%)

| Metric | val_r4 (n=65) | val-full (n=311) | Δ (abs) |
|---|---:|---:|---:|
| Profit Factor | 1.7557 | 1.0188 | -0.7369 |
| Win Rate % | 50.77 | 36.98 | -13.7900 |
| Mean PnL % | 2.343 | 0.0834 | -2.2596 |
| CVaR5 % | -21.7643 | -22.1742 | -0.4099 |

val_r4 = the original stratified 100-event sample, 65 traded, locked p=0.80 config (`results/phase_r1_final/sym_p80`). val-full = the 522-event held-out pool, same config, no re-tuning.

**ESCALATION FLAG: full-pool PF differs from val_r4 PF by more than 30% relative.**
