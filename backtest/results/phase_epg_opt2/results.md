---
tags:
  - type/results
  - domain/backtest
  - project/scanner-epg-momentum
  - status/complete
created: 2026-05-29
phase: EPG-OPT2
escalation: T8 (all Stage 1+2 val PF < GRT baseline), T7 (chart failure rate 25%)
---

# Phase EPG-OPT2 — Results

## Objective

Extend Phase EPG-GRT with: (1) lower p_close floor (0.15 vs 0.30), (2) peak cooling to address
multi-leg ratchet, (3) SlopeGate variant (F_ss/F_sl). New Borda ranking: capture_fraction +
capture_rate (replaces PF + PnL + CR from GRT).

**Baseline:** Phase EPG-GRT val winner = var_a_t300_po65_pc30 → s1_t300_po65_pc30, val PF=2.584.

---

## Experimental Setup

| Parameter | Value |
|-----------|-------|
| Training events | 300 (seed=42, 2020-11-14 to 2023-11-17) |
| Chart events | 20 (leg-structure stratified seed=7) |
| Val events | 100 (seed=99, 2023-11-17 to 2024-07-23) |
| Stage 1 configs | 84 (Variant A, p_close ∈ {0.15…p_open}, no cooling) |
| Stage 2 configs | 189 (cooling: top 15 Stage 1 + t120, 12+9 combo grid) |
| Stage 3 configs | 252 (144 F_ss + 108 F_sl) |
| Borda metric | rank_capture_fraction + rank_capture_rate |
| DQ criteria | PF < 1.20, pass_fraction < 7%, n_trades < 50 |

### Capture Fraction Definition

`capture_fraction = mean(pnl_pct / available_move_pct)` per trade, where
`available_move_pct = (max_price_during_hold − entry_price) / entry_price × 100`.

**Important:** All configs produce negative mean CF (typically -1 to -10) because losing
trades with any price excursion above entry contribute negative values. The metric still
discriminates: less-negative CF = less adverse selection on entries, not absolute efficiency.

---

## T2: Leg Classification (300 training events)

| Class | Count | % |
|-------|-------|---|
| multi_leg_3plus | 226 | 75.3% |
| failed | 30 | 10.0% |
| multi_leg_2 | 24 | 8.0% |
| single_leg | 20 | 6.7% |

75% of events have 3+ qualifying PASS windows (≥90s). Multi-leg ratchet is the dominant pattern.

---

## T3 Stage 1: Rankings (84 configs, all non-DQ)

### Top 10 by Borda

| Rank | Config | Borda | PF | CF | CR | n_trades | delay(s) | pass_frac |
|------|--------|-------|----|----|----|---------|---------|-----------|
| 1 | s1_t120_po60_pc60 | 4 | 1.774 | -1.34 | 0.004305 | 5,754 | 1374 | 0.315 |
| 2 | s1_t180_po55_pc55 | 7 | 1.848 | **-0.70** | 0.003128 | 4,883 | 982 | 0.400 |
| 3 | s1_t180_po60_pc60 | 18 | 1.841 | -2.06 | 0.003401 | 4,831 | 1175 | 0.369 |
| 4 | s1_t240_po55_pc55 | 32 | 1.863 | -2.15 | 0.002689 | 4,239 | 793 | 0.441 |
| 5 | s1_t180_po55_pc35 | 35 | 2.178 | -1.69 | 0.002269 | 1,287 | 982 | 0.474 |
| 6 | s1_t300_po60_pc60 | 40 | 1.911 | -2.52 | 0.002576 | 3,687 | 753 | 0.444 |
| 7 | s1_t180_po55_pc30 | 43 | 2.234 | -1.64 | 0.002011 | 1,139 | 982 | 0.502 |
| 8 | s1_t180_po65_pc65 | 45 | 1.789 | -3.03 | 0.003582 | 4,809 | 1246 | 0.341 |
| 9 | s1_t180_po55_pc40 | 47 | 2.104 | -2.57 | 0.002450 | 1,458 | 982 | 0.451 |
| 10 | s1_t300_po65_pc65 | 48 | 1.954 | -2.92 | 0.002896 | 3,788 | 906 | 0.412 |

User-selected: s1_t120_po65_pc65 — PF=1.691, CF=-3.23, CR=0.004256, n=5,620 (Borda ~#95).

### p_close Curve (τ=300, p_open=0.65)

PF peaks sharply at lowest p_close; new floor at 0.15 confirms the curve had not turned over at 0.30.

| p_close | PF | CF | n_trades |
|---------|-----|-----|---------|
| **0.15** | **3.063** | -2.35 | 553 |
| 0.20 | 2.852 | -2.62 | 617 |
| 0.25 | 2.579 | -6.01 | 704 |
| 0.30 | 2.509 | -5.57 | 779 |
| 0.35 | 2.406 | -5.13 | 850 |
| 0.40 | 2.449 | -4.78 | 928 |
| 0.65 (symmetric) | 1.954 | -2.92 | 3,788 |

PF improvement from extending to pc=0.15: +55% over symmetric (3.06 vs 1.95).

---

## T4 Stage 2: Cooling Rankings (189 configs, all non-DQ)

### Top 10 by Borda

| Rank | Config | Borda | PF | CF | CR | base config |
|------|--------|-------|----|----|----|-------------|
| 1 | s2_t120_po65_pc65_mc30_tc240 | 25 | 1.372 | -2.59 | 0.001397 | s1_t120_po65_pc65 |
| 2 | s2_t120_po65_pc65_mc120_tc120 | 32 | 1.373 | -2.69 | 0.001371 | s1_t120_po65_pc65 |
| 3 | s2_t120_po60_pc60_mc30_tc240 | 50 | 1.375 | -2.89 | 0.001300 | s1_t120_po60_pc60 |
| 4 | s2_t120_po60_pc60_mc180_tc120 | 55 | 1.393 | -3.02 | 0.001329 | s1_t120_po60_pc60 |
| 6 | s2_t300_po60_pc60_mc120_tc240 | 60 | 1.484 | -2.22 | 0.001039 | s1_t300_po60_pc60 |
| 9 | s2_t300_po65_pc65_mc180_tc240 | 66 | 1.504 | -2.81 | 0.001160 | s1_t300_po65_pc65 |

**Warning:** t120 cooling configs produce pathological trade counts (39k–95k, vs 5,620 base).
Aggressive cooling decays peak to near-zero, causing gate to stay nearly permanently open.
DQ threshold (PF < 1.20) does not catch these; Borda penalizes via lower CR. Not structurally
dangerous — high n_trades with positive PF — but meaningless for live trading.

**t120 best cooling combo (user config):** s2_t120_po65_pc65_mc30_tc240
(mc=30s, τ_cool=240s, borda=25 overall, PF=1.37, n=53,567)

Cooling consistently degrades all configs vs Stage 1 base:
- Best Stage 2 CR (0.001397) << Best Stage 1 CR (0.004305)
- Best Stage 2 PF (1.504) << Best Stage 1 PF by PF rank (3.063)

---

## T5 Stage 3: SlopeGate Rankings

**Bug found and fixed:** SlopeGate lookback buffer used `while buf[0].ts < cutoff: pop()` which
deleted the pre-cutoff reference entry on irregular tick intervals. Real tick data has gaps that
span cutoff → no `lv_past` → slope always undefined → gate never opens. All 144 F_ss configs DQ'd
on first run. Fixed by changing to `while buf[1].ts ≤ cutoff: pop()` (retains oldest pre-cutoff
entry). 2 regression tests added to `tests/test_slope_gate.py`.

### Stage 3 F_ss Top 10 (slope open / slope close)

| Rank | Config | Borda | PF | CF | CR | n_trades | pass_frac | delay(s) |
|------|--------|-------|----|----|----|---------|----------|---------|
| 1 | s3_fss_t300_l30_ko10_kc0 | 5 | 1.343 | -3.90 | 0.003226 | 57,382 | 0.488 | 881 |
| 2 | s3_fss_t180_l30_ko10_kc0 | 14 | 1.307 | -3.96 | 0.002937 | 70,389 | 0.492 | 706 |
| 3 | s3_fss_t180_l60_ko10_kc0 | 17 | 1.398 | -3.47 | 0.002740 | 38,544 | 0.483 | 930 |
| 4 | s3_fss_t300_l30_ko5_kc0 | 21 | 1.332 | -4.27 | 0.003055 | 68,081 | 0.510 | 665 |
| 5 | s3_fss_t180_l30_ko5_kc0 | 22 | 1.304 | -4.01 | 0.002829 | 81,188 | 0.510 | 599 |
| 6 | s3_fss_t180_l90_ko10_kc0 | 28 | 1.465 | -3.98 | 0.002650 | 27,265 | 0.476 | 1134 |

F_ss best: k_close=0 (gate stays open until slope goes negative). High trade count (50k–80k)
due to frequent reopening. τ=300 or τ=180, L=30s are dominant.

### Stage 3 F_sl Top 10 (slope open / level close)

| Rank | Config | Borda | PF | CF | CR | n_trades | pass_frac | delay(s) |
|------|--------|-------|----|----|----|---------|----------|---------|
| 1 | s3_fsl_t180_l60_ko20_pc50 | 31 | 1.591 | -3.64 | 0.001018 | 6,697 | 0.814 | 1307 |
| 2 | s3_fsl_t180_l60_ko20_pc35 | 40 | **1.737** | **-2.00** | 0.000885 | 4,463 | 0.866 | 1307 |
| 3 | s3_fsl_t180_l120_ko10_pc35 | 48 | 1.735 | -3.42 | 0.000894 | 4,406 | 0.851 | 1269 |
| 4 | s3_fsl_t180_l90_ko5_pc50 | 51 | 1.576 | -4.44 | 0.000961 | 7,928 | 0.838 | 838 |
| 8 | s3_fsl_t120_l120_ko20_pc20 | 59 | **1.847** | -3.65 | 0.000883 | 3,530 | 0.861 | 1476 |

F_sl best: τ=180, L=60s, k_open=2.0, pc=0.35 gives PF=1.74, CF=-2.00 (best CF in Stage 3).
High pass_fraction (85–87%) — once slope triggers open, level close holds long positions.

F_sl > F_ss by PF (1.74 vs 1.40), CF (-2.00 vs -3.47), and lower trade count.

---

## T6: Year Stability (12 configs, 2020–2023)

PF range threshold: > 0.60 = regime-sensitive.

| Config | 2020 | 2021 | 2022 | 2023 | Range | Sensitive |
|--------|------|------|------|------|-------|-----------|
| s1_t120_po60_pc60 | 1.82 | 1.97 | 1.78 | 1.55 | 0.42 | No |
| s1_t180_po60_pc60 | 1.97 | 1.98 | 1.85 | 1.58 | 0.40 | No |
| s1_t180_po55_pc55 | 2.06 | 1.91 | 1.88 | 1.57 | 0.49 | No |
| s1_t180_po65_pc65 | 1.97 | 1.90 | 1.85 | 1.48 | 0.49 | No |
| s2_t120_po65_pc65_mc120_tc120 | 1.32 | 1.48 | 1.48 | 1.25 | 0.23 | No |
| s2_t120_po65_pc65_mc30_tc240 | 1.37 | 1.49 | 1.43 | 1.24 | 0.25 | No |
| s3_fss_t300_l30_ko10_kc0 | 1.33 | 1.35 | 1.47 | 1.25 | 0.22 | No |
| s3_fsl_t180_l60_ko20_pc50 | 1.65 | 1.83 | 1.64 | 1.27 | 0.56 | No |
| s1_t240_po55_pc55 | 2.13 | 2.14 | 1.85 | 1.43 | 0.71 | **YES** |
| s1_t300_po60_pc60 | 2.28 | 2.24 | 1.78 | 1.48 | 0.80 | **YES** |
| s1_t180_po55_pc35 | 2.48 | 2.62 | 2.03 | 1.67 | 0.95 | **YES** |
| s1_t180_po55_pc30 | **2.67** | **2.60** | **2.18** | **1.65** | 1.02 | **YES** |

Pattern: symmetric high-frequency configs (CF+CR Borda winners) are year-stable; asymmetric
higher-PF configs are regime-sensitive. The Borda metric implicitly selects for stability.

---

## T8: Val Validation (seed=99, 96/100 events) — ESCALATED

**Escalation:** All Stage 1+2 val candidates PF < GRT baseline (2.584).

| Config | Stage | Val PF | n_trades | CF | CR |
|--------|-------|--------|---------|----|----|
| **s1_t300_po65_pc30 (GRT baseline)** | GRT | **2.584** | 284 | -2.21 | 0.00259 |
| s1_t180_po60_pc60 | 1 (Borda #3) | 2.040 | 1,436 | -3.77 | 0.00532 |
| s1_t120_po60_pc60 | 1 (Borda #1) | 2.033 | 1,665 | -3.68 | 0.00656 |
| s1_t180_po55_pc55 | 1 (Borda #2) | 1.984 | 1,442 | -3.71 | 0.00478 |
| s1_t120_po65_pc65 | 1 (user) | 1.971 | 1,668 | **-1.50** | **0.00693** |
| s2_t120_po65_pc65_mc30_tc240 | 2 | 1.367 | 20,696 | -2.92 | 0.00168 |
| s3_fsl_t180_l60_ko20_pc50 | 3 | 1.487 | 2,193 | -4.36 | 0.00114 |
| s3_fss_t300_l30_ko10_kc0 | 3 | 1.393 | 16,123 | -6.04 | 0.00455 |

Stage 3 both below PF 2.0 → inconclusive (non-hard-stop per spec).

---

## T7: Chart Generation — ESCALATED (structural)

2672/2672 expected charts generated (all OK events). 668 "failures" are structural: 4 of 20
chart events (failed leg class — NGLpC, AHTpI, NXPLW×2) had no T_event and cannot be charted
regardless of config. Failure rate = 668 / (167 × 20) = 20%, exceeding the 10% threshold.

Charts: [results/phase_epg_opt2/charts/master_index.html](results/phase_epg_opt2/charts/master_index.html)

---

## Key Findings

### 1. New Borda metric (CF+CR) vs EPG-GRT Borda (PF+PnL+CR)

The metrics select opposing config families:
- CF+CR → short-hold symmetric gates (τ=120–180, pc=p_open), stable but low PF
- PF+PnL+CR → long-hold asymmetric gates (τ=300, pc=0.15–0.30), regime-sensitive but high PF

The CF+CR selection does NOT improve val PF over the EPG-GRT winner. The new metric
measures stability and entry efficiency, not absolute edge.

### 2. p_close floor extension to 0.15

PF peaks at pc=0.15 (PF=3.06, +55% over symmetric). The GRT floor at 0.30 had not reached
the optimum. However, these configs are regime-sensitive and not selected by the new Borda metric.

### 3. Peak cooling

Cooling consistently degrades performance vs Stage 1 base across all metrics. The t120 configs
with aggressive cooling produce pathological trade counts (50k–95k). τ=300 cooling combos are
less pathological but still underperform Stage 1 τ=300 base.

### 4. SlopeGate

F_sl (slope open, level close) outperforms F_ss (slope open, slope close) on all metrics.
Best F_sl: τ=180, L=60s, k_open=2.0, pc=0.35 (PF=1.74, CF=-2.00). Still well below GRT
baseline on val (PF=1.49 vs 2.58). Stage 3 inconclusive.

### 5. GRT winner remains best

s1_t300_po65_pc30 (= var_a_t300_po65_pc30 from EPG-GRT) produces val PF=2.584, unchanged.
No config tested in Phase EPG-OPT2 improves on this.

---

## Escalation Check Table

| Criterion | Threshold | Observed | Result |
|-----------|-----------|----------|--------|
| T3d: all 84 Stage 1 DQ'd | all DQ | 0 DQ | Pass |
| T3d: all pc≤0.20 DQ'd | all DQ | 0 DQ | Pass |
| T4e: all Stage 2 DQ'd | all DQ | 0 DQ | Pass |
| T5e: all F_ss DQ'd (first run) | all DQ | 144 DQ | ESCALATE (fixed + re-ran) |
| T5e: all F_sl DQ'd (first run) | all DQ | 108 DQ | ESCALATE (fixed + re-ran) |
| T7d: chart failure rate | > 10% | 20% structural | ESCALATE (see §T7) |
| T8e: all Stage 1+2 val < baseline | all below | all below 2.584 | **ESCALATE** |
| T8e: both Stage 3 val < PF 2.0 | both below | 1.49, 1.39 | Flag only (not hard stop) |
| T8e: any val n_trades < 80 | < 80 | min=284 | Pass |

---

## Artifacts

| File | Description |
|------|-------------|
| [results/phase_epg_opt2/sweep/stage1_ranked.json](results/phase_epg_opt2/sweep/stage1_ranked.json) | 84 Stage 1 configs, Borda ranked |
| [results/phase_epg_opt2/sweep/stage2_ranked.json](results/phase_epg_opt2/sweep/stage2_ranked.json) | 189 Stage 2 cooling configs |
| [results/phase_epg_opt2/sweep/stage3_fss_ranked.json](results/phase_epg_opt2/sweep/stage3_fss_ranked.json) | 144 F_ss configs |
| [results/phase_epg_opt2/sweep/stage3_fsl_ranked.json](results/phase_epg_opt2/sweep/stage3_fsl_ranked.json) | 108 F_sl configs |
| [results/phase_epg_opt2/year_stability.json](results/phase_epg_opt2/year_stability.json) | Year stability, 12 configs |
| [results/phase_epg_opt2/val_seed99/comparison.json](results/phase_epg_opt2/val_seed99/comparison.json) | Val comparison table |
| [results/phase_epg_opt2/charts/master_index.html](results/phase_epg_opt2/charts/master_index.html) | 2672 charts, 167 configs × 16 events |
| [results/phase_epg_opt2/leg_classification.json](results/phase_epg_opt2/leg_classification.json) | 300-event leg classification |
