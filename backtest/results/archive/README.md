# Archive — Retired Phase Results (gap_pct_at_hit stratification)

**Archived:** 2026-06-30

## Why these were archived

The phase results in this directory were produced on `val_r3_stratified.json`, a 100-event
val sample that was stratified on `gap_pct_at_hit` — the percentage gain already realized at
the moment the scanner fires. The high stratum (gap_pct_at_hit > 200%) was 0/20 traded,
which was initially interpreted as a sparse-warmup failure but is actually the gate correctly
refusing entries into penny stocks already up 3,000–6,000% with no continuation. These
events are not the strategy's target and should not dominate the val sample.

The replacement sample `val_r4_stratified.json` stratifies on `momentum_pct`
(extended_session_high / prev_close − 1, as a percentage) using tercile cutpoints derived
from the 622-event val-split candidate pool (p33 = 64.76%, p67 = 95.13%). Allocation is
30/40/30 (low/mid/high). This gives balanced representation of small, medium, and large-move
events regardless of when the scanner fired relative to the move.

## Archived directories

| Directory | Original name | Superseded by |
|-----------|--------------|---------------|
| `phase_r1_fixed_corrected_gap_strat/` | `phase_r1_fixed_corrected/` | `phase_r1_final/` |
| `phase_diag_entry_gap_strat/` | `phase_diag_entry/` | `phase_diag_entry_r4/` |
| `phase_diag_tape_gap_strat/` | `phase_diag_tape/` | `phase_diag_tape_r4/` |

These results are retained for reference — the findings are not wrong, just produced on a
sample with biased stratification. Do not treat them as authoritative for any active phase.
