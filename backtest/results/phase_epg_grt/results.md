---
tags:
  - type/results
  - domain/backtest
  - project/scanner-epg-momentum
  - status/complete
created: 2026-05-28
phase: EPG-GRT
selected_config: var_a_t120_po65_pc65
---

# Phase EPG-GRT — Gate Reaction Time Optimization Results

## Objective

Find the best ParticipationGate (and alternative gate variant) parameters for the EPG entry
filter. Specifically: test asymmetric hysteresis (separate p_open / p_close thresholds),
shorter/longer EMA half-lives (τ), and four alternative gate architectures (AbsoluteThreshold,
HawkesCumulative, HawkesBuySide, BurstRatio).

All downstream exits disabled during sweep (EXIT_D off, LULD off, re-entry off, gap gate off,
watermark off). Only exit: EPG window close (PASS → FAIL/INACTIVE). This isolates gate
reaction time as the sole variable.

---

## Experimental Setup

| Parameter | Value |
|-----------|-------|
| Training events | 300 (stratified seed=42, 2020-11-14 to 2023-11-17) |
| Chart events | 10 (stratified seed=7, same split) |
| Val events | 100 (stratified seed=99, 2023-11-17 to 2024-07-23) |
| Configs swept | 129 (Variant A: 72, B: 7, C: 15, D: 15, E: 20) |
| Workers | 8 parallel |
| global_fallback_ref (training) | 166.493650 |
| global_fallback_ref (val) | 85.273576 |
| EPG K | 5 |
| EPG warmup | 300s |

### Variant Grid

| Variant | Gate Type | Parameters | N configs |
|---------|-----------|------------|-----------|
| A | ParticipationGate (asymmetric) | τ ∈ {120,180,240,300}; p_open ∈ {0.55,0.60,0.65}; p_close ∈ {0.30…p_open} | 72 |
| B | AbsoluteThresholdGate | k_abs ∈ {1.5,2.0,3.0,5.0,7.0,10.0,15.0} | 7 |
| C | HawkesCumulativeGate | β_slow ∈ {0.05,0.10,0.20}; k_slow ∈ {1.5,2.0,3.0,4.0,5.0} | 15 |
| D | HawkesBuySideGate | β_slow ∈ {0.05,0.10,0.20}; k_slow ∈ {1.5,2.0,3.0,4.0,5.0} | 15 |
| E | BurstRatioGate | window_n ∈ {30,60,90,120}; threshold_r ∈ {1.5,2.0,2.5,3.0,5.0} | 20 |

---

## T3: Training Sweep — Full Rankings

### Ranking by Borda Score (rank_pf + rank_pnl + rank_cr, lower = better)

Top 20 non-DQ configs. All are Variant A.

| Rank | Config | Borda | PF | WR% | mean_pnl% | n_trades | hold(s) | pass_frac | cap_rate |
|------|--------|-------|----|-----|-----------|---------|---------|-----------|----------|
| 1 | var_a_t300_po65_pc40 | 78 | 2.4485 | 52.26 | 3.97 | 928 | 2030 | 0.509 | 0.00195 |
| 2 | var_a_t300_po60_pc40 | 83 | 2.4251 | 51.99 | 3.78 | 1006 | 1937 | 0.520 | 0.00195 |
| 3 | var_a_t300_po65_pc30 | 85 | 2.5088 | 51.86 | 4.62 | 779 | 2900 | 0.562 | 0.00159 |
| 4 | var_a_t300_po60_pc30 | 86 | 2.5105 | 51.21 | 4.48 | 828 | 2812 | 0.573 | 0.00159 |
| 5 | var_a_t180_po60_pc30 | 87 | 2.2847 | 51.44 | 3.43 | 1042 | 1659 | 0.488 | 0.00207 |
| 6 | var_a_t240_po65_pc30 | 87 | 2.3746 | 51.05 | 4.06 | 858 | 2318 | 0.526 | 0.00175 |
| 7 | var_a_t180_po65_pc30 | 88 | 2.2548 | 50.72 | 3.51 | 968 | 1717 | 0.477 | 0.00205 |
| 8 | var_a_t300_po60_pc35 | 88 | 2.4065 | 51.98 | 4.03 | 910 | 2331 | 0.544 | 0.00173 |
| 9 | var_a_t300_po65_pc35 | 88 | 2.4056 | 52.71 | 4.19 | 850 | 2424 | 0.534 | 0.00173 |
| 10 | var_a_t240_po65_pc35 | 90 | 2.3448 | 51.70 | 3.72 | 942 | 1910 | 0.497 | 0.00195 |
| 11 | var_a_t300_po55_pc30 | 90 | 2.4720 | 49.83 | 4.30 | 875 | 2724 | 0.582 | 0.00158 |
| 12 | var_a_t120_po55_pc30 | 97 | 2.1470 | 50.18 | 2.61 | 1401 | 1018 | 0.436 | 0.00257 |
| 13 | var_a_t180_po60_pc35 | 97 | 2.1750 | 51.59 | 3.07 | 1165 | 1349 | 0.460 | 0.00227 |
| 14 | var_a_t300_po65_pc50 | 97 | 2.2125 | 50.57 | 3.14 | 1137 | 1417 | 0.465 | 0.00222 |
| 15 | var_a_t240_po60_pc35 | 97 | 2.3195 | 51.14 | 3.53 | 1011 | 1838 | 0.507 | 0.00192 |
| 16 | var_a_t300_po55_pc40 | 98 | 2.3379 | 50.82 | 3.48 | 1098 | 1832 | 0.532 | 0.00190 |
| 17 | var_a_t300_po65_pc45 | 98 | 2.2601 | 51.26 | 3.39 | 1028 | 1694 | 0.487 | 0.00200 |
| 18 | var_a_t300_po55_pc35 | 99 | 2.3625 | 52.05 | 3.83 | 976 | 2237 | 0.555 | 0.00171 |
| 19 | var_a_t240_po65_pc40 | 100 | 2.1834 | 50.67 | 3.26 | 1038 | 1594 | 0.474 | 0.00204 |
| 20 | var_a_t240_po60_pc30 | 100 | 2.3358 | 51.04 | 3.83 | 915 | 2237 | 0.535 | 0.00171 |

### Ranking by Profit Factor (training)

| Rank | Config | PF | mean_pnl% | n_trades | WR% |
|------|---------|----|-----------|---------|-----|
| 1 | var_a_t300_po60_pc30 | 2.5105 | 4.48 | 828 | 51.21 |
| 2 | var_a_t300_po65_pc30 | 2.5088 | 4.62 | 779 | 51.86 |
| 3 | var_a_t300_po55_pc30 | 2.4720 | 4.30 | 875 | 49.83 |
| 4 | var_a_t300_po65_pc40 | 2.4485 | 3.97 | 928 | 52.26 |
| 5 | var_a_t300_po60_pc40 | 2.4251 | 3.78 | 1006 | 51.99 |
| 6 | var_a_t300_po60_pc35 | 2.4065 | 4.03 | 910 | 51.98 |
| 7 | var_a_t300_po65_pc35 | 2.4056 | 4.19 | 850 | 52.71 |
| 8 | var_a_t240_po65_pc30 | 2.3746 | 4.06 | 858 | 51.05 |
| 9 | var_a_t300_po55_pc35 | 2.3625 | 3.83 | 976 | 52.05 |
| 10 | var_a_t240_po65_pc35 | 2.3448 | 3.72 | 942 | 51.70 |

Baseline (var_a_t300_po65_pc65): PF=1.9500, rank #61.

### Ranking by Mean PnL% per Trade (training)

| Rank | Config | mean_pnl% | PF | n_trades | hold(s) |
|------|---------|-----------|-----|---------|---------|
| 1 | var_a_t300_po65_pc30 | 4.62 | 2.5088 | 779 | 2900 |
| 2 | var_a_t300_po60_pc30 | 4.48 | 2.5105 | 828 | 2812 |
| 3 | var_a_t300_po55_pc30 | 4.30 | 2.4720 | 875 | 2724 |
| 4 | var_a_t300_po65_pc35 | 4.19 | 2.4056 | 850 | 2424 |
| 5 | var_a_t240_po65_pc30 | 4.06 | 2.3746 | 858 | 2318 |
| 6 | var_a_t300_po60_pc35 | 4.03 | 2.4065 | 910 | 2331 |
| 7 | var_a_t300_po65_pc40 | 3.97 | 2.4485 | 928 | 2030 |
| 8 | var_a_t240_po60_pc30 | 3.83 | 2.3358 | 915 | 2237 |
| 9 | var_a_t300_po55_pc35 | 3.83 | 2.3625 | 976 | 2237 |
| 10 | var_a_t300_po60_pc40 | 3.78 | 2.4251 | 1006 | 1937 |

### Ranking by Win Rate (training)

| Rank | Config | WR% | PF | mean_pnl% |
|------|---------|-----|----|-----------|
| 1 | var_a_t300_po65_pc35 | 52.71 | 2.4056 | 4.19 |
| 2 | var_a_t300_po65_pc40 | 52.26 | 2.4485 | 3.97 |
| 3 | var_a_t300_po55_pc35 | 52.05 | 3.3625 | 3.83 |
| 4 | var_a_t300_po60_pc35 | 51.98 | 2.4065 | 4.03 |
| 5 | var_a_t300_po60_pc40 | 51.99 | 2.4251 | 3.78 |
| 6 | var_a_t300_po65_pc30 | 51.86 | 2.5088 | 4.62 |
| 7 | var_a_t180_po60_pc35 | 51.59 | 2.1750 | 3.07 |
| 8 | var_a_t300_po60_pc30 | 51.21 | 2.5105 | 4.48 |
| 9 | var_a_t300_po65_pc45 | 51.26 | 2.2601 | 3.39 |
| 10 | var_a_t300_po60_pc45 | 51.10 | 2.2480 | 3.19 |

### Ranking by Capture Rate (training)

Capture rate = n_trades / (n_events × mean_window_duration_sec). Higher = catches more
of each available window.

| Rank | Config | cap_rate | PF | n_trades | hold(s) | WR% |
|------|---------|----------|----|---------|---------|-----|
| 1 | var_a_t120_po60_pc60 | 0.004305 | 1.774 | 5754 | 138 | 43.29 |
| 2 | **var_a_t120_po65_pc65** | **0.004256** | **1.691** | **5620** | **123** | **42.30** |
| 3–5 | (other τ=120 symmetric configs) | ~0.003–0.004 | ~1.7–1.9 | ~4000–5000 | ~130–200 | ~42–45 |
| … | var_e_n120_t2.0 | 0.004223 | 1.740 | 5607 | 143 | 42.36 |
| … | var_a_t120_po55_pc30 | 0.002565 | 2.147 | 1401 | 1018 | 50.18 |
| … | var_a_t300_po65_pc40 | 0.001954 | 2.4485 | 928 | 2030 | 52.26 |
| last | var_a_t300_po65_pc30 | 0.001593 | 2.5088 | 779 | 2900 | 51.86 |

High capture rate and high PF are strongly inversely correlated. Short τ + symmetric threshold
maximizes entry frequency at the cost of per-trade quality.

### Ranking by n_trades (training)

| Rank | Config | n_trades | PF | WR% | mean_pnl% |
|------|---------|---------|-----|-----|-----------|
| 1 | var_a_t120_po60_pc60 | 5754 | 1.774 | 43.29 | 0.596 |
| 2 | **var_a_t120_po65_pc65** | **5620** | **1.691** | **42.30** | **0.525** |
| 3 | var_e_n120_t2.0 | 5607 | 1.740 | 42.36 | 0.436 |
| 4–6 | (other τ=120 symmetric, var_e low-threshold) | ~4000–5500 | ~1.5–1.8 | ~42–45 | ~0.4–0.6 |
| … | var_a_t300_po65_pc65 (baseline) | 2743 | 1.950 | 47.88 | 1.31 |
| … | var_a_t300_po65_pc40 (Borda #1) | 928 | 2.4485 | 52.26 | 3.97 |
| last | var_a_t300_po65_pc30 (best PF) | 779 | 2.5088 | 51.86 | 4.62 |

### DQ'd Configs (12 total)

All 12 disqualified configs are Variant E (BurstRatioGate) with threshold_r ≥ 2.5.
DQ criterion: pass_fraction < 7% OR n_trades < 50.
At high threshold_r the gate is nearly always closed, producing too few trades to evaluate.

---

## T4: Selection

Selection criteria: Borda top 3 + per-variant-best (excluding top 3) + baseline.

| Role | Config | Borda | Train PF | Train WR% | n_trades |
|------|--------|-------|---------|-----------|---------|
| top_1 | var_a_t300_po65_pc40 | 78 | 2.4485 | 52.26 | 928 |
| top_2 | var_a_t300_po60_pc40 | 83 | 2.4251 | 51.99 | 1006 |
| top_3 | var_a_t300_po65_pc30 | 85 | 2.5088 | 51.86 | 779 |
| per_variant_best_a | var_a_t300_po60_pc30 | 86 | 2.5105 | 51.21 | 828 |
| per_variant_best_b | var_b_k1.5 | — | — | — | — |
| per_variant_best_c | var_c_b005_k1.5 | — | — | — | — |
| per_variant_best_d | var_d_b005_k1.5 | — | — | — | — |
| per_variant_best_e | var_e_n120_t2.0 | — | — | — | — |
| baseline | var_a_t300_po65_pc65 | #61 | 1.9500 | 47.88 | 2743 |

Config JSON files written to `config/phase_epg_grt/`.

---

## T5: Year Stability (top 10 by Borda, training split 2020–2023)

Run on full training split, each year separately (not a sample). Only Variant A appears in top 10.

| Config | 2020 PF | 2021 PF | 2022 PF | 2023 PF | Overall PF | n (overall) |
|--------|---------|---------|---------|---------|-----------|-------------|
| var_a_t300_po65_pc30 | 2.89 | **3.38** | 2.27 | 1.87 | 2.51 | 779 |
| var_a_t300_po60_pc30 | 3.03 | **3.31** | 2.35 | 1.77 | 2.51 | 828 |
| var_a_t300_po65_pc40 | 2.92 | 3.20 | 2.20 | 1.80 | 2.45 | 928 |
| var_a_t300_po60_pc40 | **3.04** | 3.15 | 2.24 | 1.68 | 2.43 | 1006 |
| var_a_t300_po60_pc35 | **3.06** | 3.08 | 2.21 | 1.69 | 2.41 | 910 |
| var_a_t300_po65_pc35 | 2.86 | 3.16 | 2.17 | 1.78 | 2.41 | 850 |
| var_a_t240_po65_pc30 | 2.79 | 3.20 | 2.04 | 1.78 | 2.37 | 858 |
| var_a_t240_po65_pc35 | 3.01 | 2.86 | 2.18 | 1.65 | 2.34 | 942 |
| var_a_t180_po60_pc30 | 2.82 | 2.73 | 2.09 | 1.70 | 2.28 | 1042 |
| var_a_t180_po65_pc30 | 2.83 | 2.73 | 2.07 | **1.61** | 2.25 | 968 |

Per-year win rates (selected configs):

| Config | 2020 WR% | 2021 WR% | 2022 WR% | 2023 WR% |
|--------|---------|---------|---------|---------|
| var_a_t300_po65_pc30 | 53.54 | 51.20 | 54.55 | 48.68 |
| var_a_t300_po60_pc30 | 54.85 | 49.72 | 52.74 | 47.93 |
| var_a_t300_po65_pc40 | 55.19 | 53.62 | 52.44 | 48.24 |
| var_a_t300_po60_pc40 | 57.75 | 50.22 | 52.07 | 48.03 |
| var_a_t180_po65_pc30 | 53.09 | 49.32 | 52.10 | 48.50 |

**Observations:**
- Every config is profitable in every year. Minimum PF across all 40 cells: 1.61.
- Systematic PF decline 2020 → 2023 (peak: 2021, weakest: 2023). Consistent across all configs.
- τ=300 substantially outperforms τ=180/240, especially in 2021 (peak year).
- pc=30 (lower close threshold) edges pc=35/40 by ~0.05–0.08 PF overall.
- Year stability data was not run for the user-selected config (var_a_t120_po65_pc65).

---

## T7: Val Validation (seed=99, 100 events, 96 ok / 4 skipped)

Val split: 2023-11-17 to 2024-07-23. Year allocation: 2023=16 events, 2024=84 events.
**T7 checks PASSED — no escalation.**

| Config | Val PF | n_trades | WR% | mean_pnl% | hold(s) | pass_frac | cap_rate |
|--------|--------|---------|-----|-----------|---------|-----------|----------|
| var_a_t300_po65_pc30 | **2.584** | 284 | 54.23 | 6.375 | 2462 | 0.551 | 0.00259 |
| var_a_t300_po65_pc40 | 2.441 | 329 | 51.67 | 4.987 | 1751 | 0.503 | 0.00285 |
| var_a_t300_po60_pc30 | 2.375 | 306 | 51.31 | 5.707 | 2367 | 0.561 | 0.00241 |
| var_a_t300_po60_pc40 | 2.278 | 359 | 49.03 | 4.561 | 1667 | 0.513 | 0.00274 |
| **var_a_t300_po65_pc65 (baseline)** | 2.113 | 1002 | 47.60 | 1.747 | 390 | 0.413 | 0.00448 |
| var_b_k1.5 | 2.054 | 1038 | 43.93 | 1.607 | 2382 | 0.898 | 0.00068 |
| var_d_b005_k1.5 | 1.837 | 2219 | 40.83 | 0.897 | 727 | 0.810 | 0.00123 |
| var_c_b005_k1.5 | 1.695 | 2209 | 43.19 | 0.768 | 678 | 0.790 | 0.00113 |
| var_e_n120_t2.0 | 1.669 | 2342 | 44.75 | 0.452 | 107 | 0.144 | 0.00422 |

Hard stops checked:
- All top configs PF > baseline (2.28–2.58 > 2.11) ✓
- All top config n_trades ≥ 100 (284–359 ≥ 100) ✓

Note: var_a_t120_po65_pc65 was not included in T7 (selected post-analysis by user; T7 ran
on selection.json which was generated by Borda ranking before user review).

---

## Selected Config: var_a_t120_po65_pc65

Selected by user based on chart analysis (T6 visual review).

### Parameters

| Parameter | Value |
|-----------|-------|
| Variant | A (ParticipationGate) |
| τ (half-life) | 120s |
| p_open | 0.65 |
| p_close | 0.65 (symmetric — no asymmetric hysteresis) |

### Training Sweep Metrics

| Metric | Value | Training Rank (of 117 non-DQ) |
|--------|-------|-------------------------------|
| Profit Factor | 1.6910 | #84 |
| Win Rate | 42.30% | — (near bottom) |
| Mean PnL% | 0.5253% | #83 |
| n_trades | 5620 | #2 (2nd highest) |
| Mean hold | 123.42s | — (very short) |
| pass_fraction | 0.2883 | — |
| Capture Rate | 0.004256 | **#2** |
| Borda Score | 169 | ~#95 |

### Val Metrics

Not directly measured in T7. The nearest comparable is var_e_n120_t2.0 (val PF=1.669,
similar high-frequency profile) and var_a_t300_po65_pc65 baseline (val PF=2.113, same
p_open/p_close=0.65 but longer τ).

### Trade-off Profile

var_a_t120_po65_pc65 makes a fundamentally different trade than the Borda top configs:

| | var_a_t120_po65_pc65 | var_a_t300_po65_pc40 (Borda #1) |
|--|---------------------|--------------------------------|
| PF (train) | 1.69 | 2.45 |
| WR (train) | 42.3% | 52.3% |
| mean_pnl% | 0.53% | 3.97% |
| n_trades | 5620 | 928 |
| hold | 123s | 2030s |
| cap_rate | 0.00426 | 0.00195 |
| Borda rank | ~#95 | #1 |

The τ=120 / symmetric config generates ~6× more trades at ~1/7th the per-trade quality.
Capture rate is 2× higher. Edge existence unclear vs. transaction cost once slippage is factored.

---

## Variant-Level Comparison Summary

| Variant | Best Config | Train PF | Val PF | Val n_trades | Val WR% |
|---------|-------------|---------|--------|-------------|---------|
| A (asym hyst) | var_a_t300_po65_pc30 | 2.5088 | 2.584 | 284 | 54.2% |
| A (symmetric baseline) | var_a_t300_po65_pc65 | 1.9500 | 2.113 | 1002 | 47.6% |
| B (AbsThreshold) | var_b_k1.5 | — | 2.054 | 1038 | 43.9% |
| C (HawkesCumul) | var_c_b005_k1.5 | — | 1.695 | 2209 | 43.2% |
| D (HawkesBuySide) | var_d_b005_k1.5 | — | 1.837 | 2219 | 40.8% |
| E (BurstRatio) | var_e_n120_t2.0 | — | 1.669 | 2342 | 44.8% |

Variants B–E all underperform the symmetric baseline on val. Asymmetric hysteresis in
Variant A is the mechanism driving the lift over baseline.

---

## Artifacts

| File | Description |
|------|-------------|
| [sweep/all_configs.json](sweep/all_configs.json) | All 129 config aggregate metrics from T3 |
| [sweep/variant_{a-e}.json](sweep/) | Per-variant results |
| [ranked_all.json](ranked_all.json) | 129 configs sorted by Borda score with per-metric ranks |
| [selection.json](selection.json) | 9 selected configs (top 3 + per-variant-best + baseline) |
| [val_validate.json](val_validate.json) | T7 val results for 9 selected configs |
| [year_stability.json](year_stability.json) | T5 per-year PF/WR for top 10 Borda configs |
| [charts/master_index.html](charts/master_index.html) | Master index: 1161 charts, 129 configs × 9 events |
| config/phase_epg_grt/*.json | 9 strategy config files ready for Phase EPG-OPT |
