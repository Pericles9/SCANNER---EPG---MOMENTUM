---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-06-30
phase: REBUILD-VAL T6 (DIAG-TAPE r4)
---

# Phase DIAG-TAPE r4 — Gap Origin Classification on val_r4_stratified

Sample: `val_r4_stratified.json` (n=100, mom_pct tercile strata 30/40/30)
Events analysed: 100 | Errors: 0

## Gap Origin × Stratum

| Stratum | T1_POSTMARKET | OVERNIGHT_NO_TAPE | T_PREMARKET | UNKNOWN | sub-$1 frac |
|---------|---|---|---|---|---|
| low | 7 | 0 | 0 | 23 | 9/30 |
| mid | 11 | 5 | 7 | 17 | 12/40 |
| high | 6 | 3 | 10 | 11 | 12/30 |


## Overall Distribution

| Category | N | % |
|---|---|---|
| T1_POSTMARKET | 24 | 24.0% |
| OVERNIGHT_NO_TAPE | 8 | 8.0% |
| T_PREMARKET | 17 | 17.0% |
| UNKNOWN | 51 | 51.0% |


## Errors

None.
