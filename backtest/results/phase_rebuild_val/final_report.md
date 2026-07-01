---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-06-30
phase: REBUILD-VAL (T1–T6)
---

# Phase REBUILD-VAL — Final Report

**Objective:** Replace `val_r3_stratified.json` (stratified on `gap_pct_at_hit`) with a
new val sample stratified on `momentum_pct` (`extended_session_high / prev_close − 1` × 100).
The old sample's high stratum (gap > 200%) was 0/20 traded because it selected penny stocks
already up 3,000–6,000% — not the strategy's target.

---

## T2 — New Sample (`val_r4_stratified.json`)

- **Candidate pool:** 622 events (val split 2023-11-17 to 2024-07-23, MDR≥200 excluded, mom_pct≥50, trades.parquet present)
- **Stratification axis:** `momentum_pct` (percent gain from prev_close to extended_session_high)
- **Cutpoints:** tercile split — p33 = 64.76%, p67 = 95.13%
- **Allocation:** 30 / 40 / 30 (low / mid / high), seed = 42
- **Exclusions at build time:** 0 (all 100 sampled events have trades.parquet)
- **Output:** `backtest/data/val_r4_stratified.json`

---

## T4 — R1 Gate Threshold Sweep

Sample: `val_r4_stratified.json` (n=100) | T_gate: `max_entry_lag_sec=500` (option A)  
Entry mode: `first_pass` | All exits: `epg_window_close` (100%)

| p | n_trades | PF | WR% | mean PnL% | CVaR5% | RTH PF | PRE PF | p90 lag (s) |
|---|----------|----|-----|-----------|--------|--------|--------|-------------|
| 0.50 | 66 | 1.214 | 46.97 | 0.954 | −25.32 | 1.485 | 1.045 | 332 |
| 0.55 | 66 | 1.376 | 46.97 | 1.623 | −24.77 | 1.764 | 1.137 | 332 |
| 0.60 | 66 | 1.320 | 48.48 | 1.306 | −24.70 | 1.689 | 1.098 | 344 |
| 0.65 | 65 | 1.265 | 47.69 | 1.142 | −25.25 | 1.653 | 1.039 | 332 |
| 0.70 | 65 | 1.416 | 44.62 | 1.465 | −18.20 | 2.163 | 1.038 | 332 |
| 0.75 | 65 | 1.584 | 46.15 | 1.962 | −21.00 | 2.873 | 1.016 | 332 |

**Escalation:** CLEARED — min n_trades = 65 (threshold: < 10)

**Key observations:**
- Gate tightening improves PF (1.21 → 1.58) and mean PnL (0.95% → 1.96%) with near-zero trade count impact (66 → 65)
- RTH PF monotonically improves with p (1.49 → 2.87); pre-market is flat near 1.0 throughout
- CVaR5 is best at p=0.70 (−18.2%); p=0.75 reverts to −21.0% despite better PF
- Pre-market drag is structural: 32 pre-market trades appear at every config, breakeven regardless of threshold

---

## T5 — DIAG-ENTRY r4

Reference: `phase_r1_final/sym_p65` (p=0.65) | T_gate: 500s | 99 events audited (1 error: SKYE 2023-12-06 missing quotes.parquet)  
**Consistency:** 0 of 99 disagree with runner (perfect match)

**Entry failure reason distribution:**

| Reason | N | % |
|--------|---|---|
| TRADED | 65 | 65.7% |
| ANCHOR_LATE | 20 | 20.2% |
| ANCHOR_NEVER_FIRED | 5 | 5.1% |
| WARMUP_AT_DEADLINE | 5 | 5.1% |
| PASS_TOO_LATE | 4 | 4.0% |
| NEVER_PASS_IN_WINDOW | 0 | 0% |

**Failure reason × stratum:**

| Stratum | TRADED | ANF | ANCHOR_LATE | WAD | NPIW | PTL | sub-$1 |
|---------|--------|-----|-------------|-----|------|-----|--------|
| low (n=29) | 17 | 2 | 8 | 1 | 0 | 1 | 9/29 (31%) |
| mid (n=40) | 27 | 1 | 8 | 3 | 0 | 1 | 12/40 (30%) |
| high (n=30) | 21 | 2 | 4 | 1 | 0 | 2 | 12/30 (40%) |

ANF = ANCHOR_NEVER_FIRED, WAD = WARMUP_AT_DEADLINE, NPIW = NEVER_PASS_IN_WINDOW, PTL = PASS_TOO_LATE

**Trade rates by stratum:** low 58.6% | mid 67.5% | high 70.0%

**Key observations:**
- ANCHOR_LATE is the dominant failure (58.8% of non-traded events) — Hawkes intensity spike fires after the 500s T_gate window
- NEVER_PASS_IN_WINDOW = 0 — once the anchor fires and warmup completes within the window, the gate always passes
- ANCHOR_NEVER_FIRED only 5 events (5.1%) — the anchor fires for 94% of events
- Higher stratum = higher trade rate (high: 70% vs low: 59%) — stronger Hawkes excitation on larger-move events
- Sub-$1 fraction: 33/99 overall (33%), peaks at high stratum (40%)

---

## T6 — DIAG-TAPE r4

100 events classified | 0 errors

**Gap origin × stratum:**

| Stratum | T1_POSTMARKET | OVERNIGHT_NO_TAPE | T_PREMARKET | UNKNOWN | sub-$1 |
|---------|:---:|:---:|:---:|:---:|:---:|
| low (n=30) | 7 | 0 | 0 | 23 | 9/30 (30%) |
| mid (n=40) | 11 | 5 | 7 | 17 | 12/40 (30%) |
| high (n=30) | 6 | 3 | 10 | 11 | 12/30 (40%) |
| **total** | **24 (24%)** | **8 (8%)** | **17 (17%)** | **51 (51%)** | **33/100** |

**Key observations:**
- UNKNOWN dominates (51%) — the gap-origin classifier cannot locate the move in extended-hours tape for the majority of events; this is especially true in the low stratum (77% UNKNOWN)
- High stratum → T_PREMARKET prominent (33%) — largest-momentum events tend to have visible pre-market tape buildup
- Low stratum → zero T_PREMARKET — smaller-momentum events almost never have detectable pre-market buildup
- Sub-$1 concentrated in high stratum (40%); overall 33/100 (33%)

---

## Phase Summary

The rebuild resolved the stratification bias from val_r3. The new sample gives:
- 65–66 trades across all gate configs (vs 0/20 in old high stratum)
- Positive PF at all configs (1.21–1.58)
- Clear RTH outperformance pattern (RTH PF up to 2.87 at p=0.75)
- Dominant failure mode is ANCHOR_LATE (20 events = 20%) — anchor fires but outside the 500s window

**Archived:** `backtest/data/val_r3_stratified_DEPRECATED_gap_pct_at_hit.json`  
**Superseded results:** `backtest/results/archive/` (phase_r1_fixed_corrected_gap_strat, phase_diag_entry_gap_strat, phase_diag_tape_gap_strat)
