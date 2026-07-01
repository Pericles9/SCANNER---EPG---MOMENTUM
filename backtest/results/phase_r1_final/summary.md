---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-06-30
phase: REBUILD-VAL T4 (R1 symmetric sweep)
---

# Phase R1 Final — Gate Threshold Sweep on val_r4_stratified

Sample: `val_r4_stratified.json` — n=100, mom_pct tercile strata (p33=64.76, p67=95.13), allocation 30/40/30  
T_gate: `max_entry_lag_sec=500` (option A, confirmed 2026-06-30)  
Entry mode: `first_pass`  
Sweep: `p_open = p_close ∈ {0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95}`

---

## R1 Sweep Results

| p | n_trades | PF | WR% | mean PnL% | CVaR5% | RTH n/PF | PRE n/PF | p90 lag (s) |
|---|----------|----|-----|-----------|--------|----------|----------|-------------|
| 0.50 | 66 | 1.2140 | 46.97 | 0.954 | −25.32 | 34 / 1.485 | 32 / 1.045 | 332 |
| 0.55 | 66 | 1.3761 | 46.97 | 1.623 | −24.77 | 34 / 1.764 | 32 / 1.137 | 332 |
| 0.60 | 66 | 1.3199 | 48.48 | 1.306 | −24.70 | 34 / 1.689 | 32 / 1.098 | 344 |
| 0.65 | 65 | 1.2652 | 47.69 | 1.142 | −25.25 | 33 / 1.653 | 32 / 1.039 | 332 |
| 0.70 | 65 | 1.4159 | 44.62 | 1.465 | −18.20 | 33 / 2.163 | 32 / 1.038 | 332 |
| 0.75 | 65 | 1.5836 | 46.15 | 1.962 | −21.00 | 33 / 2.873 | 32 / 1.016 | 332 |
| **0.80** | **65** | **1.7557** | **50.77** | **2.343** | −21.76 | 33 / 2.516 | 32 / 1.295 | 332 |
| 0.85 | 64 | 1.6630 | 43.75 | 1.822 | −20.56 | 33 / 1.738 | 31 / 1.611 | 348 |
| 0.90 | 62 | 1.6283 | 46.77 | 1.604 | −20.60 | 31 / 1.934 | 31 / 1.441 | 333 |
| 0.95 | 62 | 1.5514 | 48.39 | 1.222 | −17.26 | 31 / 2.522 | 31 / 1.153 | 333 |

All exits: `epg_window_close` (100% at every config — no EXIT_D, no LULD in EPG-Rapid).

---

## Escalation Check

- **n_trades < 10 at any p:** CLEARED — minimum = 62 (p=0.90 and p=0.95)
- **No hard stops triggered**

---

## Key Observations

1. **PF peaks at p=0.80 (1.756), not 0.75.** The curve rises from 0.50 to 0.80 then falls back — 0.85+ all trail 0.80 on PF and mean PnL. n_trades is nearly flat from 0.50–0.80 (66→65), confirming the gate filters entry timing rather than event count.

2. **RTH drives all improvement.** RTH PF improves from 1.49 (p=0.50) to 2.87 (p=0.75) then pulls back to 2.52 (p=0.80) and 1.74 (p=0.85). No monotone relationship above p=0.75. Pre-market PF is near breakeven across the entire sweep (1.02–1.61) and shows no consistent sensitivity to gate tightening.

3. **CVaR5 best at p=0.95 (−17.26%).** p=0.70 was the prior best (−18.20%); tighter thresholds continue to reduce tail risk but at the cost of PF. p=0.80 sits at −21.76% — slightly worse tails than 0.75 despite better PF.

4. **p90 entry lag ≈ 332–348s.** Well within the 500s T_gate window across all configs.

5. **Pre-market drag is structural.** Pre-market trade count holds at 31–32 regardless of threshold. The pre-market signal quality gap is not addressable by gate tightening alone.

---

## Next Steps

- **T5:** `diag_entry_r4.py` — entry failure classification (ANCHOR_NEVER_FIRED / ANCHOR_LATE / TRADED etc.) using p=0.65 as reference. T4 per_trade.json is now available.
- **T6:** `diag_tape_r4.py` — gap origin × stratum classification (T1_POSTMARKET / OVERNIGHT_NO_TAPE / T_PREMARKET / UNKNOWN).
