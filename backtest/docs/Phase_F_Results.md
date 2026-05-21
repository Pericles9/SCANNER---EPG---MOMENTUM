---
tags:
  - type/results
  - domain/backtest
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/complete
created: 2026-05-16
last_reviewed: 2026-05-16
---

# Phase F Results — Asymmetric LULD (Upper Band Only)

## Purpose

Phase F tests the **asymmetric LULD hypothesis** from Phase E: suppress the lower-band exit
entirely and retain only the upper-band exit. Phase E N=1 showed luld_upper at PF=12.57 (47
fires) while luld_lower ran at PF=0.06 (20 fires, mean -3.93%). Phase F removes the lower
band to stop EXIT_D from being pre-empted on declining trades.

**Key changes from Phase E:**

| | Phase E (N=1) | Phase F |
|-|---------------|---------|
| LULD lower band | Enabled, PF=0.06 | **Disabled** |
| LULD upper band | Enabled, PF=12.57 | Enabled, PF=13.47 (sample) / 17.53 (full val) |
| EXIT_D theta | 0.65 | 0.65 |
| EXIT_D tau_min | 4.0s | 4.0s |
| Config | `config/phase_e.json` | `config/phase_f.json` |

**Primary success metric:** PF > 2.6529 (Phase D baseline).

---

## Run Parameters

| Parameter | Value |
|-----------|-------|
| Split | val (sample + full), test (sample) |
| Sample | 100 events (stratified random, seed=42) for sample runs |
| Gap gate | Disabled |
| EXIT_D | enabled, theta=0.65, tau_min=4.0s |
| Re-entry | enabled, tau_recovery=4.0s |
| Intra-window watermark | 2% |
| LULD N spread multiple | 1 |
| LULD upper band | enabled |
| LULD lower band | **disabled** |
| LULD RTH only | true |
| Config | `config/phase_f.json` |
| Runner flag | `--luld-lower-disabled` |

---

## Results Summary

### Cross-Split Comparison Table

| Metric | Phase D val-sample | Phase F val-sample (T4) | Phase F val-full (T5) | Phase F test-sample (T7) |
|--------|--------------------|-------------------------|-----------------------|--------------------------|
| **Profit Factor** | 2.6529 | 2.2976 | 1.9194 | **2.1849** |
| **n_trades** | 345 | 476 | 6,004 | 611 |
| **win_rate** | 47.8% | 50.63% | 48.57% | 48.12% |
| **mean_pnl%** | +1.33% | +1.082% | +0.814% | +0.733% |
| **events_input** | 100 | 100 | 1,228 | 100 (of 1,186) |
| **events_with_trades** | — | 81 | 1,027 | 88 |
| **elapsed** | — | 1,984s | 20,714s | 1,733s |

### Escalation vs Baseline

| Check | Result |
|-------|--------|
| val-full PF > 2.6529 (Phase D baseline) | **FAIL** — 1.9194 |
| test-sample PF > val-full PF (no overfitting) | **PASS** — 2.1849 > 1.9194 |
| luld_lower fires == 0 (all splits) | **PASS** |
| n_events_input >> 100 (full val) | **PASS** — 1,228 events |

---

## Val Sample Results (T4 — 100 events, seed=42)

| Metric | Value |
|--------|-------|
| PF | 2.2976 |
| n_trades | 476 |
| win_rate | 50.63% |
| mean_pnl_pct | +1.082% |
| elapsed | 1,984s |

**Exit reason breakdown:**

| Exit | Count | % | PF | Mean PnL% |
|------|-------|---|----|-----------|
| luld_upper | 61 | 12.82 | 13.47 | +4.204% |
| exit_d | 232 | 48.74 | 1.956 | +1.018% |
| epg_window_close | 183 | 38.45 | 1.174 | +0.124% |
| luld_lower | 0 | 0.00 | — | — |

**Session breakdown:**

| Session | Trades | PF |
|---------|--------|----|
| Regular hours | 323 | 2.906 |
| Pre-market | 152 | 1.636 |
| Post-market | 1 | — |

---

## Val Full Results (T5 — 1,228 events)

| Metric | Value |
|--------|-------|
| PF | 1.9194 |
| n_trades | 6,004 |
| win_rate | 48.57% |
| mean_pnl_pct | +0.814% |
| elapsed | 20,714s (5.75h) |

**Exit reason breakdown:**

| Exit | Count | % | PF | Mean PnL% |
|------|-------|---|----|-----------|
| luld_upper | 598 | 9.96 | **17.53** | +4.633% |
| exit_d | 2,885 | 48.05 | 1.707 | +0.719% |
| epg_window_close | 2,521 | 41.99 | 1.018 | +0.016% |
| luld_lower | 0 | 0.00 | — | — |

**Session breakdown:**

| Session | Trades | PF |
|---------|--------|----|
| Regular hours | 3,871 | 2.279 |
| Pre-market | 2,084 | 1.497 |
| Post-market | 49 | 3.169 |

**Year breakdown:**

| Year | Trades | PF | Win% | Mean PnL% |
|------|--------|----|------|-----------|
| 2023 | 985 | 1.729 | 48.73% | +0.672% |
| 2024 | 5,019 | 1.959 | 48.54% | +0.842% |

**Intra-window blocks:** 9,444 re-entries suppressed by 2% watermark.

---

## Test Sample Results (T7 — 100 events of 1,186, seed=42)

Test split: 2024-07-23 → 2024-12-31. Run exactly once, no iteration.

| Metric | Value |
|--------|-------|
| PF | 2.1849 |
| n_trades | 611 |
| win_rate | 48.12% |
| mean_pnl_pct | +0.733% |
| elapsed | 1,733s |

**Exit reason breakdown:**

| Exit | Count | % | PF | Mean PnL% |
|------|-------|---|----|-----------|
| luld_upper | 45 | 7.36 | **11.73** | +4.152% |
| exit_d | 301 | 49.26 | 1.958 | +0.640% |
| epg_window_close | 265 | 43.37 | 1.430 | +0.259% |
| luld_lower | 0 | 0.00 | — | — |

**Session breakdown:**

| Session | Trades | PF |
|---------|--------|----|
| Regular hours | 370 | 2.224 |
| Pre-market | 241 | 2.133 |

---

## Key Findings

### F1 — Asymmetric LULD Suppresses Lower-Band Drag

Removing the lower-band exit eliminated 20 losing trades (Phase E N=1: PF=0.06, mean -3.93%)
from the sample and yielded zero luld_lower fires across all Phase F runs. EXIT_D now owns
the downside entirely, which is its design purpose.

### F2 — Upper-Band Exit Remains Highly Valuable and Stable

| Split | luld_upper count | luld_upper PF | luld_upper mean_pnl% |
|-------|-----------------|---------------|----------------------|
| val-sample | 61 | 13.47 | +4.204% |
| val-full | 598 | **17.53** | +4.633% |
| test-sample | 45 | 11.73 | +4.152% |

The upper-band exit is consistent across all three splits: PF 11–18, mean +4.1–4.6%. It
catches parabolic moves that exhaust at the LULD ceiling before EXIT_D or EPG window close
can fire. This is a robust and repeatable exit signal.

### F3 — epg_window_close Is The Primary Drag

| Split | epg_window_close count | epg_window_close % | epg_window_close PF |
|-------|------------------------|-------------------|----------------------|
| val-sample | 183 | 38.45% | 1.174 |
| val-full | 2,521 | 41.99% | **1.018** |
| test-sample | 265 | 43.37% | 1.430 |

On val-full, epg_window_close accounts for 42% of trades at near-breakeven (PF=1.018,
mean +0.016%). These are trades held open until the EPG participation signal transitions
from PASS → FAIL/INACTIVE — effectively time-limit exits. The val-full result (1.018) is
much weaker than the val-sample (1.174) and test-sample (1.430), suggesting the 100-event
sample is optimistic for this exit reason.

### F4 — Sample-to-Full Val PF Gap (+0.38 PF)

val-sample PF=2.2976 vs val-full PF=1.9194, a gap of 0.378. The 100-event sample
consistently overstates performance vs the full distribution. This is consistent with
stratified random sampling selecting events with higher-than-average EPG quality or momentum.
Future phases should weight the full-val number more heavily when assessing true performance.

### F5 — Test Generalizes Above Val-Full (No Overfitting Signal)

test PF=2.1849 > val-full PF=1.9194 (+0.27). The strategy did not overfit to the val
period. The test pre-market session (PF=2.133) also recovered from the val-full pre-market
regression (PF=1.497), suggesting the pre-market weakness in val-full may be
period-specific (2023–mid-2024) rather than structural.

### F6 — Overall PF Still Below Phase D Baseline

Phase D baseline (val-sample, lower-band-only, Phase C intra-window filter): PF=2.6529.
Phase F val-full: PF=1.9194. The gap of 0.73 is primarily driven by:
1. epg_window_close degrading from ~1.4 (sample) to 1.018 (full val)
2. Pre-market PF=1.497 dragging the session-weighted average

The strategy is live-capable at PF~2.18 (test) but does not surpass the Phase D benchmark.

---

## Output Files

| File | Contents |
|------|----------|
| `results/phase_f/val_sample/run_summary.json` | T4 val sample summary |
| `results/phase_f/val_sample/per_trade.parquet` | T4 per-trade records |
| `results/phase_f/val_full/run_summary.json` | T5 full val summary |
| `results/phase_f/val_full/per_trade.parquet` | T5 per-trade records (6,004 trades) |
| `results/phase_f/test_full/run_summary.json` | T7 test sample summary |
| `results/phase_f/test_full/per_trade.parquet` | T7 per-trade records |
| `results/phase_f/charts_val_sample/` | 8 aggregate charts (val sample) |
| `results/phase_f/charts_val_full/` | 8 aggregate charts (val full) |
| `results/phase_f/charts_test_full/` | 8 aggregate charts (test sample) |
| `results/phase_f/event_charts_val_full/` | Per-event charts — 4 of 50 (replay cache gap) |
| `results/phase_f/event_charts_val_sample/` | Per-event charts (val sample) |
| `config/phase_f.json` | Phase F config |
| `tools/phase_f/aggregate_charts.py` | 8-chart aggregate diagnostic runner |
| `tools/phase_f/run_charts.py` | Per-event chart runner |
| `tools/phase_f/chart.py` | 4-panel Plotly per-event chart builder |

**Note on per-event charts:** `event_charts_val_full/` contains only 4 of 50 requested
charts. The chart runner requires Hawkes replay cache files (`.pkl`) built during earlier
phase runs. Caches exist for the 100-event val sample only; the remaining 1,128 full-val
events have no pre-built caches. This is a tooling gap, not a strategy issue.

---

## Related

- Strategy spec: [[Scanner-EPG-Momentum]]
- Phase E results: [[Phase_E_Results]]
- Phase D results: [[Phase_D_Results]]
- Phase F config: `config/phase_f.json`
- Aggregate chart runner: `tools/phase_f/aggregate_charts.py`
- Per-event chart runner: `tools/phase_f/run_charts.py`
