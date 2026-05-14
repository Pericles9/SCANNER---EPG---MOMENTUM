---
tags:
  - type/results
  - domain/backtest
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/complete
created: 2026-05-13
last_reviewed: 2026-05-13
---

# Phase C Results — Gap Gate Removal + Backside Filter Candidates

## Purpose

Phase C removes the 30% intraday-gap gate and tests two backside entry filters as
replacements. The gap gate was a backtest-only heuristic that proxied the live scanner's
gap threshold. Removing it exposes all EPG PASS rising edges to the entry stack —
increasing sample size but also introducing entries that the live scanner would not have
flagged. Two filters are tested to recover entry quality:

- **Option A — Price high watermark drawdown:** Block entry if price has pulled back
  more than threshold% from its post-T_event high. Sweep: [2%, 3%, 5%, 7%].
- **Option C — CVD since T_event:** Block entry if cumulative dollar-volume imbalance
  since T_event is sell-dominant (CVD < 0). Binary; no threshold parameter.

**Bias note:** Removing the gap gate is a known look-ahead relative to Phase B.
The live scanner would have flagged events at various gap levels; the backtest now
enters on all PASS rising edges regardless. PF comparisons across Phase B and Phase C
are directional only — not like-for-like.

---

## Run Parameters (all variants)

| Parameter | Value |
|-----------|-------|
| Split | val |
| Sample | 100 events (stratified random, seed=42) |
| Year distribution | 16 × 2023, 84 × 2024 |
| Gap gate | **Disabled** (all variants) |
| EXIT_D | enabled, theta=0.65, tau_min=4.0s |
| Re-entry | enabled, tau_recovery=4.0s |
| LULD proximity threshold | 2.0% |
| LULD ref_window_sec | 300s |
| LULD warmup_sec | 60s |
| LULD RTH only | true |
| EPG k | 5 |
| EPG τ | 300s |
| EPG p | 0.65 |
| EPG warmup | 300s |
| Hawkes β | 0.1 (fixed) |
| Hawkes ρ | 0.99 |
| Refit interval | 50 trades |
| Cold-start size | 1,000 trades |
| Runner | `backtest/runner.py` |
| Config | `config/phase_c.json` |

Backside filter variants are activated via CLI: `--watermark-threshold` (Option A),
`--cvd-filter` (Option C).

---

## Variant Results

### 5-Variant Comparison

| Variant | PF | n_trades | win% | mean_pnl% | entries_blocked |
|---------|----|----------|------|-----------|-----------------|
| Phase B (gap gate 30%, no filter) | 1.3825 | 1,689 | 45.17% | +0.294% | — |
| T4 No filter (gap gate off, baseline) | 1.7391 | 3,588 | 46.46% | +0.524% | 0 |
| Option A: watermark 5% | 1.9443 | 1,945 | 46.68% | +0.615% | 572 |
| Option C: CVD filter | **2.0328** | 1,145 | 46.81% | +0.852% | 666 |
| Combined A+C | **2.3360** | 677 | 47.71% | +1.057% | 808 |

**Phase B is not a valid baseline for within-Phase-C comparisons.** The no-filter
baseline (T4, PF=1.7391) is the correct Phase C floor — it uses the same gap-gate-off
universe.

### Option A — Watermark Threshold Sweep

| Threshold | PF | n_trades | win% | mean_pnl% | blocked |
|-----------|-----|----------|------|-----------|---------|
| 2% | 1.9786 | 1,161 | 47.37% | +0.551% | 756 |
| 3% | 1.9793 | 1,473 | 47.45% | +0.575% | 686 |
| 5% | 1.9443 | 1,945 | 46.68% | +0.615% | 572 |
| 7% | 1.8813 | 2,246 | 46.53% | +0.586% | 467 |

Peak PF at 3% (1.9793). Selection rule: prefer loosest within 0.05 PF of peak →
**winner: 5%** (delta = 0.035 < 0.05). 7% falls outside tolerance (delta = 0.098).

---

## Key Findings

### F1 — Gap Gate Removal Alone Improves No-Filter PF (Bias-Adjusted Context)

The no-filter baseline (PF=1.7391) exceeds Phase B (PF=1.3825) with 2.1× more trades.
**This is not a clean improvement:** removing the gap gate admits entries that the live
scanner would not have flagged, artificially inflating PF relative to the live-tradeable
universe. The no-filter baseline establishes the ceiling for what unfiltered PASS edges
look like, not a deployable improvement.

### F2 — CVD Filter Delivers the Highest Single-Filter PF

CVD filter PF=2.0328 with 666 entries blocked (out of ~1,811 first-entry PASS edges).
Win rate increases to 46.81% vs 46.46% for no-filter. Mean PnL increases from +0.524%
to +0.852%. CVD correctly identifies and rejects sell-dominant rising edges.

CVD exceeded the pre-set 2.0 PF escalation threshold. User-approved continuation:
*"go ahead and finish the phase."* The escalation is noted but not treated as a blocker
given the explicit approval.

### F3 — Watermark Filter Adds Value but at Lower PF than CVD

Watermark 5% PF=1.9443 vs CVD PF=2.0328. The watermark filter blocks 572 entries
(vs 666 for CVD), implying the two filters do not overlap perfectly — each catches
different bad entry candidates. This is confirmed by the combined run: 808 total blocks
with 572 watermark + 236 CVD-only (implying 430 entries were blocked by watermark but
not CVD, and 236 entries were blocked by CVD but not watermark).

### F4 — Combined A+C Highest PF but Thinnest Sample

Combined A+C PF=2.336, n=677. Each individual filter improves PF; combining them
compounds the improvement at the cost of sample size. n=677 is above the minimum
threshold (50) but represents less than 20% of the no-filter trade count (3,588).
**Combined A+C is not recommended for deployment:** the extreme PF likely reflects
over-selection on the 100-event val sample, and the thin sample makes the estimate
unreliable.

### F5 — Win Rate Gradient Is Monotonically Consistent

All five variants show a consistent win-rate ordering:
Phase B (45.17%) < no-filter (46.46%) < watermark (46.68%) < CVD (46.81%) < combined (47.71%).
The gradient is small in absolute terms but consistent in direction, supporting the
hypothesis that the filters are capturing genuine entry quality signal rather than
noise artifacts.

### F6 — CVD Selected as Best Deployable Single Filter

Based on highest single-filter PF (2.0328) and coherent bias narrative (buy-dominant
cumulative flow since T_event as an entry criterion), CVD is selected as the Phase C
winner. It has the simplest implementation (binary, no threshold parameter), largest
n among filtered variants (1,145 trades), and a mechanistic justification aligned with
the EPG momentum hypothesis.

---

## Bias Documentation

Phase C contains a known look-ahead bias introduced by gap gate removal:

| Bias | Direction | Magnitude | Mitigation |
|------|-----------|-----------|------------|
| Gap gate removal exposes sub-30% gap entries | Upward PF bias | Unknown; PF floor lifted 1.38→1.74 | Document; do not compare Phase C absolute PF to Phase B as evidence of improvement |
| CVD filter tested on same sample used to select it | Overfitting risk | Moderate; binary filter, no threshold to optimize | Validate CVD on holdout (Phase D) |
| 100-event sample is same val seed=42 used throughout | In-sample | Same risk level as Phase B | Holdout validation planned |

The backside filter comparison is internally valid (all 5 variants share the same gap-gate-off universe), but external validity vs Phase B's PF is compromised.

---

## Escalation Events

| Trigger | Threshold | Actual | Disposition |
|---------|-----------|--------|-------------|
| T4 PF < Phase B PF | < 1.3825 | 1.7391 — NO TRIGGER | Passed |
| Option A n_trades < 50 | < 50 | 1,945 — NO TRIGGER | Passed |
| Option C n_trades < 50 | < 50 | 1,145 — NO TRIGGER | Passed |
| Any variant PF > 2.0 | > 2.0 | CVD: 2.0328 — **TRIGGERED** | User approved continuation |
| Null cvd_since_t_event > 5% | > 5% | Not measured | Not triggered |

---

## Phase C Configuration (winner: CVD filter)

```json
{
  "gap_gate": {"enabled": false},
  "watermark_filter": {"enabled": false},
  "cvd_filter": {"enabled": true},
  "exit_d": {"enabled": true, "theta": 0.65, "tau_min_sec": 4.0},
  "reentry": {"enabled": true, "tau_recovery_sec": 4.0},
  "luld": {"proximity_pct_threshold": 2.0}
}
```

Winning config is in `config/phase_c.json`. CVD filter is enabled via `--cvd-filter`
CLI flag; config file has `cvd_filter.enabled = false` as default with override
documented.

---

## Output Files

| File | Contents |
|------|----------|
| `results/phase_c/no_filter_baseline/run_summary.json` | T4 baseline (no filter, no gap gate) |
| `results/phase_c/cvd_filter/run_summary.json` | Option C CVD filter results |
| `results/phase_c/watermark_0.02/run_summary.json` | Watermark sweep 2% |
| `results/phase_c/watermark_0.03/run_summary.json` | Watermark sweep 3% |
| `results/phase_c/watermark_0.05/run_summary.json` | Watermark sweep 5% (winner) |
| `results/phase_c/watermark_0.07/run_summary.json` | Watermark sweep 7% |
| `results/phase_c/combined_ac/run_summary.json` | Combined A+C (watermark 5% + CVD) |
| `results/phase_c/watermark_sweep.json` | Watermark sweep comparison table |
| `results/phase_c/comparison_table.json` | 5-variant comparison table |
| `results/phase_c/event_charts/` | Per-event charts (CVD variant, 56 events) |
| `results/phase_c/event_charts/index.html` | Sortable index of per-event charts |

---

## Next Steps

| Priority | Task |
|----------|------|
| High | Phase D: Validate watermark 5% filter on holdout split (now best single-filter candidate after CVD bug fix) |
| High | Re-evaluate CVD filter role — fixed PF=1.7544 barely exceeds no-filter baseline; may not warrant standalone Phase D validation |
| Medium | Characterize watermark-blocked entries: session, gap size, hold time if not blocked |
| Medium | Determine if gap gate removal has a viable live-trading analog (e.g., filter on gap<15% rather than gap<30%) |
| Low | Re-run watermark sweep with holdout validation before adopting |

---

## Phase C.5 — CVD Accumulator Bug Fix (2026-05-13)

### Bug Description

A systematic bug was found in the Phase C CVD accumulator (`backtest/runner.py` lines 540–542):

```python
# BUGGY (Phase C original):
direction = 1.0 if sides[i] == 1 else -1.0
cvd_since_t_event += cur_p_tick * float(td.sizes[i]) * direction
```

Ambiguous trades (`sides[i] == 0`, classified by neither Lee-Ready step) were mapped to
`direction = -1.0` (sell) by the `else` branch. Across the 81-event val sample, 9.5% of all
trades (672,706 / 7,046,943) were ambiguous, raising the effective sell rate from 43.9% to
53.4% under the buggy code. This caused the filter to block 281 additional entries (666 → 385
blocks) that the correct classifier would have admitted.

**Fix:** `cvd_since_t_event += cur_p_tick * float(td.sizes[i]) * float(sides[i])`

`float(0) = 0.0`, so ambiguous trades now contribute zero to CVD. Fix is in
`backtest/runner.py` line 541. Unit tests: `tests/test_cvd_accumulator.py` (6 tests).

### Signing Verification (T3)

Four independent checks run on the fixed CVD accumulator before re-running:

| Check | Test | Result | Value |
|-------|------|--------|-------|
| A | OFI ambiguity rate per event (flag if >30%) | CLEAR | 0/81 events flagged |
| B | Spearman rho(CVD_fixed, I(t)) at rising edges | CLEAR | rho=−0.055, p=0.084, n=971 |
| C | Spearman rho(CVD_fixed, 30s fwd return) | CLEAR | rho=−0.016, p=0.617, n=971 |
| D | Visual spot-check: uptrend CVD behavior | FALSE POSITIVE | 75% blocked for XBP (genuine distribution, 0 decision changes) |

Check D was triggered by XBP 2023-12-04 (uptrend event, 75% of rising edges CVD-blocked
under fixed accumulator). Investigation showed genuine sell-dominant flow at those edges
(buy-dollar ratios 0.30–0.50); the buggy and fixed accumulators made identical pass/block
decisions for this event. False positive confirmed; proceeded to T4.

Visual charts: `results/phase_c_fix/validation_charts/`

### Phase C.5 Results — Fixed CVD vs Buggy (INVALID)

**The Phase C CVD results (PF=2.0328) are INVALID.** Do not use them for deployment decisions.

| Variant | Status | PF | n_trades | win% | mean_pnl% | blocks |
|---------|--------|----|----------|------|-----------|--------|
| No filter (T4 baseline) | VALID | 1.7391 | 3,588 | 46.46% | +0.524% | 0 |
| Watermark 5% | VALID | 1.9443 | 1,945 | 46.68% | +0.615% | 572 |
| CVD filter (buggy) | INVALID | 2.0328 | 1,145 | 46.81% | +0.852% | 666 |
| CVD filter (fixed) | VALID | 1.7544 | 2,214 | 45.93% | +0.584% | 385 |

**Winner change:** Watermark 5% (PF=1.9443) is now the best single-filter candidate.
Fixed CVD (PF=1.7544) exceeds the no-filter baseline by only 0.015 PF and is below watermark.

**Why PF dropped:** The bug over-blocked 281 entries. Those entries had lower average quality
than the 385 correctly-blocked entries — admitting them pulled PF down from 2.03 to 1.75.
The bug was accidentally finding better entries by rejecting too aggressively.

### Phase C.5 Output Files

| File | Contents |
|------|----------|
| `results/phase_c_fix/cvd_bug_diagnostic.json` | Bug quantification, T1c edge analysis |
| `results/phase_c_fix/ofi_diagnostics.json` | T3 verification — Checks A, B, C, D |
| `results/phase_c_fix/validation_charts/` | T3j visual spot-check charts (3 events) |
| `results/phase_c_fix/cvd_filter_fixed/run_summary.json` | T4 fixed CVD re-run results |
| `results/phase_c_fix/comparison_table_updated.json` | T5 updated 5-variant table |

---

## Phase C Original Run Artifacts

| File | Contents |
|------|----------|
| `results/phase_c/no_filter_baseline/run_summary.json` | T4 baseline (no filter, no gap gate) |
| `results/phase_c/cvd_filter/run_summary.json` | Option C CVD filter results — **INVALID (buggy accumulator)** |
| `results/phase_c/watermark_0.02/run_summary.json` | Watermark sweep 2% |
| `results/phase_c/watermark_0.03/run_summary.json` | Watermark sweep 3% |
| `results/phase_c/watermark_0.05/run_summary.json` | Watermark sweep 5% (winner) |
| `results/phase_c/watermark_0.07/run_summary.json` | Watermark sweep 7% |
| `results/phase_c/combined_ac/run_summary.json` | Combined A+C (watermark 5% + CVD) — **INVALID (buggy CVD)** |
| `results/phase_c/watermark_sweep.json` | Watermark sweep comparison table |
| `results/phase_c/comparison_table.json` | 5-variant comparison table — **INVALID for CVD variants** |
| `results/phase_c/event_charts/` | Per-event charts (CVD variant, 56 events) — buggy CVD panel |
| `results/phase_c/event_charts/index.html` | Sortable index of per-event charts |

---

## Related

- Strategy spec: [[Scanner-EPG-Momentum]]
- Phase B results: [[Phase B Results]]
- Phase C config: `config/phase_c.json`
- CVD filter runner: `backtest/runner.py` (`--cvd-filter` CLI flag)
- Comparison table (original, partially invalid): `results/phase_c/comparison_table.json`
- Comparison table (updated, fixed CVD): `results/phase_c_fix/comparison_table_updated.json`
