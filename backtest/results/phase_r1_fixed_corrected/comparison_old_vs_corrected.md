# R1-Fixed: Old vs Corrected (prev_close fix) Comparison

_Generated: 2026-06-29 22:08_

## Escalation Checks

| Check | Old | New | Pass? |
|-------|-----|-----|-------|
| corrected n_trades p=0.65 >= 28 | 28.0 | 24.0 | ✗ FAIL (ESCALATION) |
| best corrected CVaR5 >= -15% | -24.4 | -8.3 | ✓ PASS |

### T10a — Archive Integrity

**PASS**: T10a PASS — 0 files in phase_r1_fixed_pre_prevclose_fix/ modified after sweep start

## Symmetric Sweep Comparison

> **Note:** old = `phase_r1_fixed_pre_prevclose_fix` run on `val_mdr150_diagnostic.json` (100 events); new = `phase_r1_fixed_corrected` on corrected `val_r3_stratified.json` (97 events). Different val samples — n_trades delta is informational only.

| p | old_n | new_n | Δn | old_PF | new_PF | ΔPF | old_WR% | new_WR% | old_meanPnL | new_meanPnL | old_CVaR5 | new_CVaR5 |
|---|-------|-------|-----|--------|--------|-----|---------|---------|-------------|-------------|-----------|-----------|
| 0.50 | 48 | 25 | -23 | 3.9693 | 1.2389 | -2.730 ↓ | 58.33 | 48.00 | 12.91 | 0.80 | -29.92 | -17.50 |
| 0.55 | 48 | 25 | -23 | 3.6631 | 1.4361 | -2.227 ↓ | 56.25 | 52.00 | 11.15 | 1.41 | -30.07 | -20.36 |
| 0.60 | 46 | 25 | -21 | 2.8452 | 1.6068 | -1.238 ↓ | 54.35 | 52.00 | 9.46 | 1.99 | -28.89 | -17.50 |
| 0.65 | 46 | 24 | -22 | 2.6584 | 1.4308 | -1.228 ↓ | 54.35 | 41.67 | 8.27 | 1.15 | -27.74 | -17.50 |
| 0.70 | 46 | 24 | -22 | 2.3073 | 1.2579 | -1.049 ↓ | 45.65 | 37.50 | 6.50 | 0.59 | -26.05 | -8.42 |
| 0.75 | 45 | 24 | -21 | 2.2216 | 1.7864 | -0.435 ↓ | 44.44 | 50.00 | 5.86 | 1.63 | -24.37 | -8.27 |

## Val Sample Changes

| Metric | Before fix | After fix |
|--------|-----------|----------|
| n_events (val sample) | 86 | 97 |
| strata: low/mid/high | 50/16/20 | 50/27/20 |
| catalog_diff UNCHANGED | 46 | — |
| catalog_diff EARLIER | 26 | — |
| catalog_diff LATER | 14 | — |
| catalog_diff NOW_FOUND | 0 | — |
| catalog_diff NOW_MISSING | 0 | — |

## Spot-Check Summary (T5)

| Ticker | Date | old_threshold | new_threshold | Δ | T5a |
|--------|------|--------------|--------------|-----|-----|
| EVAX | 2024-01-23 | 1.560 | 1.469 | −0.091 | OK |
| LMFA | 2024-05-16 | 0.741 | 0.741 | +0.000 | FAIL (edge case: PM decline) |
| SNTG | 2024-07-12 | 2.782 | 2.699 | −0.083 | OK |
| FRGE | 2024-03-25 | 2.015 | 2.002 | −0.013 | OK |
| JANX | 2024-02-27 | 17.407 | 17.082 | −0.325 | OK |

*LMFA: Prior event dir date (2024-03-11) had PM price decline — 16:00 ET price > 20:00 ET price. Fix applied correctly but direction was unfavorable for this one event.*

## T11 — Sparse Warmup Residual (p=0.65 arm)

Failure reason distribution:

| Reason | Count | % |
|--------|-------|---|
| TRADED | 24 | 25.3% |
| ANCHOR_NEVER_FIRED | 10 | 10.5% |
| ANCHOR_LATE | 40 | 42.1% |
| WARMUP_AT_DEADLINE | 18 | 18.9% |
| NEVER_PASS_IN_WINDOW | 3 | 3.2% |
| PASS_TOO_LATE | 0 | 0.0% |

**Sparse warmup residual (ANCHOR_LATE + WARMUP_AT_DEADLINE): 58**

## Conclusions

**ESCALATION**: 1 criterion/criteria failed: corrected n_trades p=0.65 >= 28

Do not proceed to R1-Final until escalation items are resolved.

_Do not begin R1-Final (final p_close selection), R2 (SF entry tuning), or any follow-on phase until Cooper has reviewed these results and given explicit approval._