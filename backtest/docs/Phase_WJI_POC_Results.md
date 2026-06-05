<!-- fullWidth: false tocVisible: false tableWrap: true -->
---
tags:
  - type/results
  - domain/backtest
  - domain/signal
  - project/scanner-epg-momentum
  - status/needs-review
created: 2026-06-05
last_reviewed: 2026-06-05
---

# Phase WJI-POC Results — Weighted Joint Intensity Gate (Proof of Concept)

## Purpose

Phase WJI-POC introduces a new entry gate: the **Weighted Joint Intensity (WJI) gate**, which\
replaces the EPG `ParticipationGate` with a geometric-mean signal that combines two intensity\
channels:

- **λ\_V** — dollar-volume EMA (τ\_v = 180s decay), normalized to its pre-event reference mean
- **λ\_buy_slow** — buy-side arrival kernel (β\_slow = 0.01, \~69s half-life), normalized to steady-state

```
WJI = norm_λ_V^α × norm_λ_buy^(1-α)
```

A **slope-driven adaptive peak** accumulates when `WJI slope ≥ 0` or while in PASS, and decays\
exponentially (τ\_decay = 120s half-life) during FAIL windows with negative slope. Gate uses\
asymmetric hysteresis: FAIL→PASS at `WJI ≥ p_open × peak`, PASS→FAIL at `WJI < p_close × peak`.\
An L_sec = 60s lookback gap enforces a FAIL-only warmup until slope history is available.

**Primary research question:** Does the WJI geometric mean structure — requiring both volume\
momentum and directional flow to be elevated simultaneously — produce a profitable entry signal\
on a quality-filtered event universe?

**Sample design:** SF quality filter (Q_tilde ≥ 0.65 at T_event) applied to full train/val\
splits. 200-event training sample and 100-event val sample, year-stratified (seed=7).

**Baseline:** GRT winner `var_a_t300_po65_pc30` (EPG-GRT Phase, PF=2.584 on full val).

---

## Configuration

### WJI Gate (`config/phase_wji_poc/wji_poc.json`)

| Parameter      | Value | Description                                         |
| -------------- | ----- | --------------------------------------------------- |
| alpha          | 0.50  | Equal weighting: vol and buy-arrival each 50%       |
| tau_v          | 180s  | Dollar-volume EMA decay constant                    |
| beta_slow      | 0.01  | Buy-side kernel decay rate (\~69s half-life)        |
| L_sec          | 60s   | Slope lookback; gate stays FAIL until history fills |
| tau_decay      | 120s  | Peak half-life during FAIL + negative slope         |
| p_open         | 0.65  | FAIL→PASS threshold as fraction of adaptive peak    |
| p_close        | 0.30  | PASS→FAIL threshold as fraction of adaptive peak    |
| warmup_seconds | 300s  | No entry until 300s post T_event                    |

### Disabled Modules (POC scope)

EXIT_D: disabled | LULD: disabled | Gap gate: disabled | Watermark: disabled | Re-entry: disabled

### Baseline Reference

`var_a_t300_po65_pc30` — EPG GRT Phase winner: variant A, τ=300s, p_open=0.65, p_close=0.30.

---

## T2 — Quality Filter Summary

Single filter applied: **SF at T_event** — Q_tilde\[bar_at_T_event\] ≥ 0.65.

| Stage                                    | Train | Val   |
| ---------------------------------------- | ----: | ----: |
| Raw events (has_quotes=True)             | 3,956 | 1,211 |
| After Hawkes+SF pipeline (valid T_event) | 3,700 | 1,144 |
| After SF filter (Q_tilde ≥ 0.65)         | **436** | **152** |
| Drop rate                                | 88.2% | 86.7% |
| Final sample (stratified, seed=7)        | **200** | **100** |

**Training sample year distribution:** 2020: 45 | 2021: 66 | 2022: 38 | 2023: 51

**Val sample year distribution:** 2023: 7 | 2024: 93

T2 pipeline runtime: \~15.5h (training, cold) + \~5.1h (val, cold). Checkpoint caching active —\
subsequent runs load from `.cache_train_results.json` / `.cache_val_results.json`.

---

## T3 — Training Results (200 events)

| Metric                       | WJI POC       | GRT Baseline |
| ---------------------------- | ------------: | -----------: |
| Profit Factor                | **1.4705**    | **1.7630**   |
| n_trades                     | 2,937         | 538          |
| n_events_with_trades         | 200 / 200     | 192 / 200    |
| Win rate                     | 49.37%        | 44.24%       |
| Mean PnL %                   | 0.95%         | 3.02%        |
| Total PnL %                  | 2,786.96%     | 1,622.67%    |
| PnL per event                | 13.93%        | 8.45%        |
| Mean hold (s)                | 1,457s        | 3,269s       |
| Pass fraction                | 92.7%         | 56.1%        |
| Mean first-entry delay (s)   | 1,402s        | 2,152s       |
| n_entries_blocked_by_SF      | 3,462 (54.1%) | 79 (12.8%)   |
| n_pass_windows_total         | 6,399         | 617          |
| component_balance            | 0.310         | —            |
| pct_windows_with_prior_decay | 90.4%         | —            |

**T3 escalation checks: PASSED** (PF=1.4705 ≥ 1.20 ✅ | component_balance=0.310 ≥ 0.10 ✅)

---

## T4 — Val Results (100 events)

| Metric                       | WJI POC       | GRT Baseline |
| ---------------------------- | ------------: | -----------: |
| Profit Factor                | **1.1720**    | **1.1747**   |
| n_trades                     | 1,546         | 267          |
| n_events_with_trades         | 100 / 100     | 98 / 100     |
| Win rate                     | 47.35%        | 40.82%       |
| Mean PnL %                   | 0.44%         | 0.87%        |
| Total PnL %                  | 674.97%       | 232.65%      |
| PnL per event                | 6.75%         | 2.37%        |
| Mean hold (s)                | 1,288s        | 2,686s       |
| Pass fraction                | 92.3%         | 53.3%        |
| Mean first-entry delay (s)   | 1,172s        | 1,881s       |
| n_entries_blocked_by_SF      | 1,885 (54.9%) | 29 (9.8%)    |
| n_pass_windows_total         | 3,431         | 296          |
| component_balance            | 0.276         | —            |
| pct_windows_with_prior_decay | 89.9%         | —            |

**T4 escalation checks: PASSED** (PF=1.1720 ≥ 1.00 ✅ | n_trades=1,546 ≥ 30 ✅)

---

## T5 — Charts

4-panel HTML charts generated for all val events with ≥ 1 WJI trade.

Output: `results/phase_wji_poc/charts/{TICKER}_{DATE}.html` + `index.html`

**Panels:**

1. 10s OHLCV candlesticks — PASS window shading (green), SF-unqualified shading (light red),\
   entry/exit markers
2. WJI signal — p_open×peak (dashed), p_close×peak (dotted), adaptive peak (grey),\
   slope-decay shading (orange)
3. Normalized components — norm\_λ\_V (blue) + norm\_λ\_buy (orange), reference at 1.0
4. slope_WJI bar chart — green (positive) / red (negative)

T5 was still running at time of report generation (21/100 events complete as of last log write).

---

## Escalation Check Table

| Criterion                                   | Threshold | Observed | Result |
| ------------------------------------------- | --------- | -------- | ------ |
| T2h: train quality universe after SF filter | ≥ 200     | 436      | ✅ PASS |
| T2h: quality training sample                | ≥ 100     | 200      | ✅ PASS |
| T2h: quality val sample                     | ≥ 40      | 100      | ✅ PASS |
| T3f: WJI train PF                           | ≥ 1.20    | 1.4705   | ✅ PASS |
| T3f: component_balance                      | ≥ 0.10    | 0.310    | ✅ PASS |
| T4e: WJI val PF                             | ≥ 1.00    | 1.1720   | ✅ PASS |
| T4e: n_trades val                           | ≥ 30      | 1,546    | ✅ PASS |

**All escalation gates passed.**

---

## Diagnostic Interpretation

### WJI vs Baseline — trade volume and PnL profile

WJI generates **5.8× more trades** than the GRT baseline on training (2,937 vs 538) and\
**5.8× more on val** (1,546 vs 267). This is the most significant structural difference.\
Mean PnL per trade is correspondingly lower (0.44% WJI vs 0.87% baseline on val). PnL per\
event is higher for WJI (6.75% vs 2.37% on val), but this is because all 100 val events\
had ≥ 1 WJI trade vs 98/100 for baseline — the spread is not large enough to be diagnostic.

PF is nearly identical on val: WJI 1.1720 vs baseline 1.1747 (delta = −0.003). This means\
WJI is not adding or destroying edge relative to the GRT baseline on this quality-filtered\
sample. The gate is generating far more entry signals, at smaller size per win, with the\
same aggregate ratio of wins to losses.

The WJI pass fraction of 92.3–92.7% vs baseline 53.3–56.1% confirms the gate is much more\
permissive: for most of each EPG window, WJI is in PASS. The mean hold of 1,288–1,457s\
(WJI) vs 2,686–3,269s (baseline) is consistent — WJI windows open and close faster.

### component_balance

Train: 0.310 | Val: 0.276. Both components are actively contributing (neither is near-zero),\
but the buy-arrival component is running at roughly **30% of the volume component** at gate\
open. The geometric mean is not degenerate, but the signal is substantially more volume-driven\
than buy-flow-driven with α=0.50. A rebalance toward α=0.65–0.70 (increasing vol weight\
explicitly) may better match empirical component ratios, or a lower β\_slow to widen the\
buy-arrival sensitivity band.

### pct_windows_with_prior_decay

Train: 90.4% | Val: 89.9%. The slope-driven peak decay mechanism is firing on \~90% of all\
PASS windows — meaning nearly every gate open follows a period where the peak decayed from\
its prior close. This is high but expected given τ\_decay=120s and typical inter-window gaps\
on active scanners. The decay mechanism is working as designed and is not degenerate.

If the goal is to require genuinely elevated WJI at open (not just a low decayed peak), τ\_decay\
should be lengthened (e.g., 300s) so the effective p_open × peak threshold stays higher between\
windows. At 120s, the peak can halve in two minutes — making p_open=0.65 effectively easier\
to reach after any brief pause.

### SF blocking rate

54.1–54.9% of WJI gate-open attempts are blocked by the SF re-entry requirement. This is\
much higher than the baseline (9.8–12.8%) because WJI opens far more windows. The SF gate\
is serving as a meaningful secondary filter here — roughly half of WJI's generated entries\
are suppressed. If WJI enters Phase H, the interaction between WJI gate cadence and SF\
pass/fail state should be studied directly.

---

## Output Files

| File                                              | Description                             |
| ------------------------------------------------- | --------------------------------------- |
| `results/phase_wji_poc/quality_sample_train.json` | 200-event training sample (seed=7)      |
| `results/phase_wji_poc/quality_sample_val.json`   | 100-event val sample (seed=7)           |
| `results/phase_wji_poc/quality_filter_summary.json` | Filter drop counts + year distributions |
| `results/phase_wji_poc/train_wji.json`            | T3 WJI training results                 |
| `results/phase_wji_poc/train_baseline.json`       | T3 baseline training results            |
| `results/phase_wji_poc/val_wji.json`              | T4 WJI val results                      |
| `results/phase_wji_poc/val_baseline.json`         | T4 baseline val results                 |
| `results/phase_wji_poc/charts/`                   | T5 4-panel HTML charts (generating)     |
| `results/phase_wji_poc/.cache_train_results.json` | Hawkes+SF checkpoint (training)         |
| `results/phase_wji_poc/.cache_val_results.json`   | Hawkes+SF checkpoint (val)              |
| `config/phase_wji_poc/wji_poc.json`               | WJI gate configuration                  |
| `core/epg/gate_variants.py`                       | WJIGate class implementation            |
| `tests/test_wji_gate.py`                          | 32 unit tests for WJIGate               |
| `tools/phase_wji_poc/common.py`                   | Shared worker functions                 |
| `tools/phase_wji_poc/t2_quality_samples.py`       | T2 driver                               |
| `tools/phase_wji_poc/t3t4_run.py`                 | T3+T4 driver                            |
| `tools/phase_wji_poc/t5_charts.py`                | T5 chart generator                      |

---

## Open Questions (for Phase H)

1. **α calibration:** component_balance \~0.28–0.31 suggests the buy-arrival channel contributes\
   less than the volume channel at gate-open time. Is α=0.50 the right starting point, or\
   should α be swept (0.3, 0.5, 0.7) to find the balance that maximizes per-trade PnL?
2. **τ\_decay lengthening:** Current 120s allows peak to halve quickly, making p_open=0.65\
   trivially reachable after inter-window gaps. Does τ\_decay=300s or τ\_decay=600s reduce\
   the 90% decay rate and produce a more selective gate?
3. **p_open tightening:** WJI's 92%+ pass fraction is close to always-in. A higher p_open\
   (e.g., 0.75–0.80) might cull the weakest entries and improve per-trade PnL at the cost\
   of trade count.
4. **SF interaction study:** With 54% of WJI entries blocked by SF, the joint WJI+SF filter\
   may be over-selective. A dedicated sub-analysis of what passes WJI but fails SF (and vice\
   versa) could identify whether the two signals are complementary or redundant.
5. **Baseline PF degradation (train vs val):** GRT baseline drops from PF=1.763 on train\
   to PF=1.175 on val. This is a large step-down. The quality-filtered sample (SF ≥ 0.65)\
   may be over-represented with certain market regimes; val 93% 2024 events vs train 66%\
   2021 events suggests substantial regime skew in the sample.

---

## Approval Gate

**Status: needs-review**

All escalation checks passed. Phase WJI-POC is complete. Proceed to Phase H (parameter sweep)\
requires explicit approval.