---
tags:
  - type/results
  - domain/backtest
  - domain/signal
  - project/scanner-epg-momentum
  - status/complete
created: 2026-05-29
phase: EPG-OPT2-SF
parent_phase: EPG-OPT2
verdict: "Setup filter is net-negative on val (mean delta_pf = -0.085). Does not add edge."
---

# Phase EPG-OPT2-SF — Setup Filter Integration Results

## Objective

Integrate the tradeable setup filter as a continuous entry-stack condition on the top decile
of EPG-OPT2 configs. An entry is allowed only when the EPG gate is in PASS **and** the setup
filter is currently qualified (Q_tilde ≥ threshold on the current 1-min bar). Determine whether
gating entries on filter qualification improves val PF.

**Primary success metric:** at least one top-decile config produces val PF ≥ 2.584 with the
filter active, OR the filter produces a consistent PF lift of ≥ 0.10 across the top decile.

---

## Critical Correction (Hotfix)

The first implementation included a **15-bar sustain gate** (`bar_idx >= first_qualify_bar`):
entries were blocked until Q_tilde had sustained ≥ threshold for 15 consecutive bars. **This was
never in the setup filter spec** — it was erroneously introduced into the phase prompt. All
results under that implementation were discarded.

**Correct behavior:** the filter checks the **current bar's** Q_tilde only. Entry is allowed as
soon as `Q_tilde[bar] >= threshold` (0.75 for the first 65 warmup bars, 0.65 after). No sustain
requirement. `first_qualify_bar` is retained as a diagnostic field only.

Fix location: `tools/sweep_runner_opt2.py` — `precompute_sf_trajectory` (qualified array now pure
current-bar threshold) and `sf_is_qualified_at` (no longer gates on `first_qualify_bar < 0`). The
SF integration lives in the sweep infrastructure, not `runner.py`, since that is the path T4/T5
exercise. Tests rewritten in `tests/test_runner_sf.py`; 152/152 pass.

Impact of the fix: mean entries-blocked dropped from ~38% (buggy) to 35.4% (corrected) on val,
and training block rates on top configs fell from ~19–61% to ~15–37%.

---

## Setup (unchanged from EPG-OPT2)

| Parameter | Value |
|-----------|-------|
| Training events | 300 (seed=42) |
| Val events | 100 (seed=99) |
| Top decile | 52 configs (top 10% of 525 non-DQ OPT2 configs by global CF+CR Borda) |
| Decile composition | 46 Stage 1 (ParticipationGate) + 6 Stage 3 F_ss (SlopeGate) |
| Borda metric | rank_capture_fraction + rank_capture_rate |
| DQ | PF < 1.20, pass_fraction < 7%, n_trades < 50 |
| Compute | full capacity (10 workers, no throttle); live system offline |

---

## T3: Setup Filter Trajectories

| Split | Events | Never qualify | Mean first_qualify_bar | ψ |
|-------|--------|--------------|----------------------|---|
| Training | 300 | 45 (15.0%) | 104 min | unevaluable* |
| Val | 100 | 4 (4.0%) | 101 min | unevaluable* |

*ψ fails 100% as a data artifact: filtered event files lack the 3-day lookback history
`compute_lookback_low()` needs, so `lookback_low` returns 0. ψ is a data-integrity check, not a
live filter, and blocks nothing (per spec). first_qualify_bar is now diagnostic-only.

---

## T4: Training with Corrected SF (52/52 non-DQ)

Top 10 by Borda (SF active), with OPT2 unfiltered PF for reference:

| # | Config | Stage | Borda | PF (SF) | PF (OPT2) | CF | CR | blocked% |
|---|--------|-------|-------|---------|-----------|-----|-----|----------|
| 1 | s1_t120_po60_pc60 | s1 | 3 | 1.629 | 1.774 | -1.31 | 0.003628 | 15.4% |
| 2 | s1_t180_po55_pc55 | s1 | 13 | 1.650 | 1.848 | -0.46 | 0.002564 | 17.4% |
| 3 | s1_t180_po60_pc60 | s1 | 18 | 1.665 | 1.841 | -2.23 | 0.002804 | 17.5% |
| 4 | s1_t240_po55_pc55 | s1 | 22 | 1.624 | 1.863 | -1.90 | 0.002103 | 19.9% |
| 5 | s1_t120_po65_pc65 (user) | s1 | 28 | 1.551 | 1.691 | -3.61 | 0.003460 | 14.7% |
| 6 | s1_t300_po60_pc60 | s1 | 30 | 1.651 | 1.911 | -2.60 | 0.002008 | 22.4% |
| 7 | s1_t180_po55_pc35 | s1 | 33 | 1.826 | 2.178 | -2.08 | 0.001750 | 36.5% |
| 8 | s1_t120_po60_pc40 | s1 | 35 | 1.720 | 2.017 | -3.43 | 0.002286 | 31.9% |
| 9 | s1_t300_po65_pc65 | s1 | 37 | 1.710 | 1.954 | -3.48 | 0.002316 | 19.9% |
| 10 | s3_fss_t180_l60_ko10_kc0 | s3 | 37 | 1.363 | 1.398 | -3.69 | 0.002924 | 23.2% |

SF lowers training PF on every top config vs OPT2 unfiltered. No escalation.

---

## T5: Val Comparison — SF vs no-SF (same seed=99 100-event sample)

Both filtered and unfiltered runs executed on the identical 100-event val sample.
Metrics ordered by what this phase ranks on: total_pnl_pct, capture_fraction, capture_rate.
(PF and per-config PF deltas are in `val_comparison.json`; they are not the deciding metric.)

### Aggregate across all 52 configs

| Metric | SF mean | no-SF mean | mean Δ | configs SF improved |
|--------|---------|-----------|--------|--------------------|
| **total_pnl_pct** | 1270 | 1895 | **−624** | **0 / 52** |
| capture_fraction | −3.111 | −2.847 | −0.264 | 20 / 52 |
| capture_rate | 0.003472 | 0.003638 | −0.000166 | 6 / 52 |
| n_trades | 1875 | 2449 | −574 | — |
| entries blocked | 35.4% | — | — | — |

### Top 12 by total_pnl_pct with SF

| Config | Stage | tot_pnl SF | tot_pnl noSF | Δ | CF_sf | CR_sf | n_sf | blk% |
|--------|-------|-----------|-------------|-----|-------|-------|------|------|
| s3_fss_t180_l30_ko5_kc0 | s3 | 2822 | 3715 | −894 | −4.58 | 0.004671 | 18863 | 24% |
| s3_fss_t300_l30_ko5_kc0 | s3 | 2706 | 3577 | −871 | −6.13 | 0.004749 | 15781 | 22% |
| s3_fss_t180_l30_ko10_kc0 | s3 | 2626 | 3436 | −810 | −4.25 | 0.004688 | 16132 | 21% |
| s3_fss_t300_l30_ko10_kc0 | s3 | 2474 | 3247 | −774 | −7.96 | 0.004747 | 13064 | 19% |
| s3_fss_t180_l60_ko10_kc0 | s3 | 2228 | 2778 | −550 | −4.48 | 0.003945 | 8654 | 21% |
| s3_fss_t180_l90_ko10_kc0 | s3 | 1718 | 2395 | −677 | −7.84 | 0.003061 | 5971 | 22% |
| s1_t180_po65_pc65 | s1 | 1453 | 1960 | −507 | −3.33 | 0.006564 | 1165 | 19% |
| s1_t300_po60_pc60 | s1 | 1282 | 1810 | −528 | −3.71 | 0.004040 | 814 | 27% |
| s1_t240_po55_pc55 | s1 | 1249 | 1745 | −496 | −0.84 | 0.003972 | 982 | 22% |
| s1_t120_po65_pc65 (user) | s1 | 1248 | 1571 | −323 | −1.17 | 0.006823 | 1412 | 15% |
| s1_t180_po60_pc60 | s1 | 1238 | 1721 | −483 | −4.47 | 0.005004 | 1143 | 20% |
| s1_t180_po55_pc40 | s1 | 1229 | 1800 | −571 | −2.20 | 0.003728 | 309 | 36% |

### Reads from the data
- **total_pnl_pct: SF lower on all 52 configs.** Mean −624 pnl points. The filter removes
  ~574 trades per config on average and net cumulative PnL falls with them.
- **capture_rate: SF lower on 46/52.** Per-unit-time PnL efficiency does not improve.
- **capture_fraction: SF better on 20/52, worse on 32/52, mean −0.26.** SF does remove some
  low-quality entries (raising the pnl/available-move ratio on those 20), but not consistently,
  and not enough to offset the total_pnl loss.

---

## Success Metric Evaluation

| Clause | Result |
|--------|--------|
| ≥1 top-decile config improved under SF on the ranking metrics | **Failed on total_pnl (0/52); marginal on CF (20/52, mean −0.26)** |
| Consistent improvement across the decile | **Failed** — every config loses total_pnl; capture_rate down on 46/52 |

(The prompt's literal clause was phrased on PF; included in `val_comparison.json` for completeness.
On PF, mean Δ = −0.085, 47/52 lower — same direction as the ranking metrics.)

---

## Why the Filter Costs PnL

The filter blocks ~35% of entries (15–53% per config). These are momentum-gap events: the EPG
gate fires T_event early, and a large share of entries occur in the first impulse before the
1-min Q_tilde composite crosses 0.65. Those blocked entries are net-contributors to total_pnl —
removing them lowers cumulative PnL on every config. The high-volume Stage 3 F_ss configs (the
total_pnl leaders, 13k–19k trades) lose the most absolute PnL (−770 to −894); the lower-frequency
Stage 1 configs lose less in absolute terms but still uniformly negative.

The filter is a sustained-liquidity quality screen; this strategy's PnL is front-loaded into the
early impulse the filter is slowest to confirm. They are misaligned.

---

## Escalation Check Table

| Criterion | Threshold | Observed | Result |
|-----------|-----------|----------|--------|
| T3f: training configs all DQ | all DQ | 0 DQ | Pass |
| T4f: val PF all < 1.20 with SF | all below | min SF PF = 1.34 | Pass |
| T4f: mean pct_entries_blocked | > 60% | 35.4% | Pass |

No escalations in the corrected run.

---

## Artifacts

| File | Description |
|------|-------------|
| [combined_ranking.json](combined_ranking.json) | 525 non-DQ OPT2 configs, global CF+CR Borda |
| [top_decile_configs.json](top_decile_configs.json) | 52 top-decile configs |
| [sf_summary_train.json](sf_summary_train.json) / [sf_summary_val.json](sf_summary_val.json) | Per-event filter trajectories |
| [sf_leg_class_stats.json](sf_leg_class_stats.json) | first_qualify_bar by leg class |
| [sweep_train_sf_ranked.json](sweep_train_sf_ranked.json) | Training, SF active, Borda-ranked |
| [sweep_val_sf.json](sweep_val_sf.json) | Val, SF active |
| [sweep_val_noSF.json](sweep_val_noSF.json) | Val, SF inactive (same sample) |
| [val_comparison.json](val_comparison.json) | SF vs no-SF delta table |
| [charts/master_index.html](charts/master_index.html) | 2-panel charts, top 10 filtered val configs, SF-blocked bars shaded red |

---

## Conclusion

With the corrected current-bar filter, the setup filter is **net-negative** as an entry gate:
mean val PF delta = −0.085, 47/52 configs degraded, and the highest-edge configs lose the most
(up to −0.32 PF). The filter blocks the early-impulse entries that carry this strategy's alpha.
Recommendation deferred to review — but the data does not support adding the setup filter to the
entry stack for these configs.
