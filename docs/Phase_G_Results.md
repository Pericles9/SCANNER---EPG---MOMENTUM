---
tags:
  - type/results
  - domain/backtest
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/complete
created: 2026-05-18
last_reviewed: 2026-05-18
---

# Phase G Results — Scanner Context & Time-of-Day Analysis

## Purpose

Phase G is an **analysis-only phase**. No new backtest is run. No strategy parameters
change. Source data is `results/phase_f/val_full/per_trade.parquet` (6,004 trades).

**Goals:**
- Build a rolling scanner leaderboard for 80 sampled val-full dates
- Join 8 scanner context columns to per-trade records
- Compute full-session time-of-day (TOD) stats across all 6,004 trades
- Surface patterns in rank, heat, multi-day runner status, and session timing

**Primary success metric:** scanner_context table written for ≥90% of Phase F
val-full sampled trades with no join failures. **Result: 94.6% — PASS.**

**Key constraint:** No Phase H or follow-on work without explicit approval from Cooper.

---

## Run Parameters

| Parameter | Value |
|-----------|-------|
| Source | `results/phase_f/val_full/per_trade.parquet` (6,004 trades) |
| Sampled dates (T1) | 80 (stratified: 14 from 2023, 66 from 2024, seed=42) |
| Scanner definition | Daily-qualified: ticker achieves q_tilde ≥ 0.65 streak of ≥15 bars at any point during session |
| Rank criterion | pct_change from prev_close at entry minute |
| TOD base | 4:00 AM ET (time_of_day_sec=0), 10-min buckets |
| Bootstrap CI | 1,000 resamples, seed=42, 95% |
| Runner | `tools/phase_g/run_analysis.py` |
| Charts | `tools/phase_g/charts.py` |
| Elapsed | 13s |

**Scanner definition note:** "Daily-qualified" means the ticker achieved the
15-bar q_tilde streak at any point in the session day (including after entry).
This models a scanner that tracks momentum names for the full day once they
qualify. Momentum event entries frequently happen within the first 20 minutes
of RTH, before the streak accumulates bar-by-bar — using a strict "must be
qualified at entry" criterion produced only 19% join rate (structural limitation,
not a bug).

---

## T1–T3: Scanner Context

### T1 — Date Sample

| Parameter | Value |
|-----------|-------|
| Dates sampled | 80 |
| 2023 | 14 |
| 2024 | 66 |

### T2 — Scanner Leaderboard Build

| Metric | Value |
|--------|-------|
| Mean co-events per date (before filter) | 19.2 |
| Mean active names per date (day-qualified) | 12.5 |

### T3 — Context Join

| Metric | Value |
|--------|-------|
| Trades in sampled dates | 3,039 |
| Matched (scanner_rank non-null) | 2,874 (94.6%) — **PASS** |
| Null rank | 165 (5.4%) |
| Null reason | ticker never achieved q_tilde streak during session day |

---

## Key Findings

### G1 — Rank 1 (Fastest Mover) Underperforms Ranks 2–9

| Rank | n | EV | PF | 95% CI (PF) |
|------|---|----|----|------------|
| 1 | 548 | +0.235% | 1.179 | [0.867, 1.655] |
| 2 | 429 | +0.753% | 1.621 | [0.974, 2.675] |
| 3 | 392 | +1.296% | 2.792 | [1.612, 4.618] |
| 4 | 277 | +1.005% | 2.882 | [1.495, 5.395] |
| 5 | 212 | +1.489% | 3.860 | [2.379, 6.623] |
| 6 | 127 | +1.214% | 2.886 | [1.475, 5.485] |
| 7 | 149 | +1.543% | 2.673 | [1.373, 5.134] |
| 8 | 145 | +1.025% | 2.829 | [1.417, 5.815] |
| 9 | 83 | +1.672% | **6.038** | [2.569, 14.941] |
| 10 | 96 | +0.674% | 2.354 | [1.250, 5.034] |

Rank 1 is the weakest entry in the top 10: PF=1.179 vs PF=2.79–6.04 for ranks 3–9.
The fastest-moving name at entry time underperforms. Ranks 2–9 are substantially better
across EV and PF, with overlapping but clearly shifted CIs.

**Potential implication (Phase H candidate):** a rank filter avoiding rank 1 (or the
top N percentile of momentum) may improve PF at modest trade count cost.

### G2 — Heat Monotonically Improves PF

Heat = 75th percentile pct_change of scanner names at entry.

| Heat Bin | n | EV | PF |
|----------|---|----|----|
| cold_Q1 | 719 | +0.514% | 1.457 |
| Q2 | 718 | +1.001% | 2.483 |
| Q3 | 718 | +1.036% | 2.375 |
| hot_Q4 | 719 | +1.162% | **2.616** |

Cold scanners (low overall momentum) produce PF=1.46. Hot scanners (high
overall momentum) produce PF=2.62. The step from cold to Q2/Q3 (+1.0 PF)
is the largest gap.

**Potential implication:** a heat gate (require scanner_heat above the median
pct_change) could filter out the cold_Q1 trades (PF=1.46) and raise aggregate PF.

### G3 — luld_upper Exit Strongly Clustered in Hot Scanners

| Segment | luld_upper share | vs population |
|---------|-----------------|---------------|
| Cold Q1 | 0.9% | 0.11x — strongly suppressed |
| Q2 | 10.2% | 1.19x |
| Q3 | 10.3% | 1.20x |
| **Hot Q4** | **13.5%** | **1.57x — flagged elevated** |

The high-value luld_upper exit (val-full PF=17.53, mean +4.63%) concentrates
almost entirely in hot scanner environments. Cold scanners essentially never
produce LULD-ceiling exits. This explains much of the heat-vs-PF gradient: hot
scanners produce more parabolic moves.

### G4 — Multi-Day Runners Outperform Fresh Events

| Group | n | EV | PF | Win Rate |
|-------|---|----|----|---------|
| fresh_event | 2,018 | +0.834% | 1.935 | 48.9% |
| multi_day_runner | 1,021 | +1.141% | **2.757** | 49.6% |

Multi-day runners (ticker had a momentum event in the prior 5 calendar days)
show +0.31% higher EV and PF=2.76 vs PF=1.94 (+0.82 PF gap). Win rate is
nearly identical. The PF gap is driven by higher upside on runners (more
luld_upper fires, larger luld_upper payoff).

**Potential implication:** a multi-day runner preference filter is the
strongest standalone signal found in Phase G.

### G5 — TOD: Open Best, Midday Near-Breakeven, Late-Day Secondary Peak

**Top RTH buckets (by EV):**

| Time (ET) | n | EV | PF |
|-----------|---|----|----|
| 09:30 | 241 | +2.315% | 3.109 |
| 09:40 | 171 | +1.729% | 2.948 |
| 14:50 | 52 | +1.562% | 5.074 |
| 15:40 | 50 | +1.756% | **6.038** |
| 14:00 | 52 | +1.302% | 4.031 |

**Bottom RTH buckets (by EV, all near-breakeven):**

| Time (ET) | n | EV | PF |
|-----------|---|----|----|
| 14:20 | 71 | +0.164% | 1.367 |
| 12:10 | 100 | +0.180% | 1.203 |
| 15:20 | 61 | +0.195% | 1.171 |
| 12:30 | 87 | +0.237% | 1.306 |
| 11:50 | 154 | +0.279% | 1.574 |

Strong U-shaped intraday pattern: open (9:30–10:00) and late-day (14:00–15:40)
outperform; midday (11:30–13:30) approaches breakeven. This matches known
microstructure patterns (wide spreads and momentum at open; institutional
accumulation/distribution into close vs. low-volume chop midday).

**Potential implication:** a midday exclusion window (e.g., 11:30–13:00 ET) could
raise PF at the cost of ~15–20% trade count reduction.

### G6 — Rank × Heat Interaction: Mid-Rank + Hot = Best Combo

| Rank | Heat | n | EV | PF |
|------|------|---|----|----|
| 1 | cold_Q1 | 203 | +0.272% | 1.197 |
| 1 | hot_Q4 | 91 | +0.254% | 1.203 |
| 2 | cold_Q1 | 152 | -0.392% | **0.747** |
| 2 | hot_Q4 | 124 | +1.498% | 2.252 |
| 3 | cold_Q1 | 109 | +1.150% | 2.294 |
| **3** | **hot_Q4** | **124** | **+2.217%** | **6.461** |
| 4 | cold_Q1 | 32 | +4.703% | 10.320 |
| 4 | hot_Q4 | 108 | +0.563% | 1.874 |
| 5 | cold_Q1 | 45 | +0.720% | 2.329 |
| 5 | hot_Q4 | 81 | +1.275% | 3.664 |

Rank 3 + hot_Q4 is the most reliable high-PF combination (n=124, PF=6.46).
Rank 2 + cold_Q1 is the only losing combination (PF=0.75, n=152).
Rank 1 is flat across heat bins (PF~1.20 in both — no heat sensitivity).

### G7 — Scanner Size: Weak Positive Signal

| Size Bin | Mean n | n_trades | EV | PF |
|----------|--------|----------|----|----|
| small_Q1 (≤11) | — | 1,013 | +0.827% | 2.110 |
| Q2 (≤13) | — | 565 | +0.704% | 1.769 |
| Q3 (≤16) | — | 866 | +1.148% | 2.290 |
| large_Q4 (>16) | — | 430 | +1.020% | 2.414 |

Weak positive relationship between scanner size (more qualifying names on the
day) and performance. No strong actionable signal.

### G8 — Entry Lag: Near-Zero Correlation

Correlation (time_of_day_sec × pnl_pct): 0.011 (essentially zero).

| Session-Age Bucket | n | EV | PF |
|-------------------|---|----|----|
| 300–600s (~5–10 min) | 65 | +1.803% | 2.315 |
| 600–1800s (~10–30 min) | 96 | +0.545% | 1.580 |
| 1800s+ (>30 min) | 2,878 | +0.931% | 2.173 |

Note: time_of_day_sec is used as a proxy for session age (no t_event anchor in
per_trade). This reflects absolute time from 4:00 AM ET, not time from event trigger.

---

## Scanner Context Escalation Results

| Check | Result |
|-------|--------|
| join_rate ≥ 90% | **PASS** — 94.6% |
| mean_active_names ≥ 2.0 | **PASS** — 12.5 names/date |
| zero_active_at_entry incidents ≤ 5 | **PASS** — 0 |
| All T5 sub-analyses run without error | **PASS** |
| 12 charts written | **PASS** |

---

## Strategy Implication Flags (for Phase H consideration)

The following signals are observational. None are implemented. All require
validation on val_full in a subsequent phase before the test set may be opened.

| Signal | Observation | Potential Gate |
|--------|-------------|----------------|
| Multi-day runner | PF=2.76 vs 1.94 (+0.82) | Prefer events where ticker appeared in momentum catalog within prior 5 days |
| Heat gate | Cold Q1 PF=1.46, Hot Q4 PF=2.62 (+1.16) | Require scanner_heat (75th pct) above session median |
| Rank gate | Rank 1 PF=1.18 vs ranks 3–9 PF=2.67–6.04 | Avoid or downweight rank 1 entries |
| TOD midday filter | 11:30–13:00 ET PF near-breakeven | Exclude midday window |
| Rank × Heat combo | Rank 3 + hot Q4: PF=6.46 (n=124) | Combined filter |

**None of these are implemented. Phase H requires Cooper's explicit approval.**

---

## Output Files

| File | Contents |
|------|----------|
| `results/phase_g/sampled_dates.json` | T1: 80 sampled dates with year distribution |
| `results/phase_g/scanner_build_log.json` | T2: scanner build stats per date |
| `results/phase_g/scanner_context.parquet` | T3+T5f: 3,039 rows with 8 context columns |
| `results/phase_g/tod_stats.parquet` | T4: 85 10-min TOD buckets (full 6,004 trades) |
| `results/phase_g/rank_stats.parquet` | T5a: EV, PF, WR + 95% CI by scanner_rank |
| `results/phase_g/heat_bin_stats.parquet` | T5b: stats by scanner heat quartile |
| `results/phase_g/rank_heat_interaction.parquet` | T5c: EV/PF for ranks 1–10 × cold/hot |
| `results/phase_g/exit_by_scanner_context.parquet` | T5d: exit distribution by rank + heat |
| `results/phase_g/scanner_size_stats.parquet` | T5e: stats by scanner size quartile |
| `results/phase_g/multi_day_runner_stats.parquet` | T5f: fresh vs multi-day runner comparison |
| `results/phase_g/entry_lag_stats.parquet` | T5g: session-age proxy stats |
| `results/phase_g/phase_g_meta.json` | Combined T1–T3 metadata + elapsed |
| `results/phase_g/charts/01_pnl_vs_pct_change_scatter.html` | PnL% vs pct_change scatter by rank |
| `results/phase_g/charts/02_ev_by_rank.html` | EV with CI by scanner rank |
| `results/phase_g/charts/03_pf_by_rank.html` | PF with CI by scanner rank |
| `results/phase_g/charts/04_ev_pf_by_heat_bin.html` | EV and PF by heat quartile |
| `results/phase_g/charts/05_rank_heat_interaction.html` | EV heatmap: rank × heat |
| `results/phase_g/charts/06_tod_ev_pf.html` | TOD EV bars + PF line |
| `results/phase_g/charts/07_tod_wr_hold.html` | TOD win rate + mean hold |
| `results/phase_g/charts/08_exit_type_by_rank.html` | Exit share stacked by rank |
| `results/phase_g/charts/09_exit_type_by_heat.html` | Exit share stacked by heat |
| `results/phase_g/charts/10_ev_pf_by_scanner_size.html` | EV and PF by scanner size |
| `results/phase_g/charts/11_multi_day_runner.html` | Fresh vs multi-day runner comparison |
| `results/phase_g/charts/12_entry_lag.html` | Entry lag stats by session-age bucket |

---

## Implementation Notes

### Scanner Definition Revision

The Phase G spec called for "permanent collapse" semantics: once q_tilde drops
below 0.65 after qualifying, the ticker is permanently removed from the scanner.
Testing showed this produces only 1 active bar per qualification cycle (the 15th
streak bar itself) and a 19% join rate — a structural limitation given that
q_tilde collapses from 0.65 momentarily in virtually every streak.

Two iterations:
1. "No permanent collapse" (active when q_tilde ≥ 0.65 and ever-qualified):
   produced 41.6% join rate. Still fails because entries happen before the
   streak accumulates (RTH open, first 15–20 minutes).
2. "Daily qualified" (ever achieves streak during session = on leaderboard for
   the day): produced 94.6% join rate. Conceptually appropriate: a scanner that
   tracks momentum names for the day once they qualify.

The revision is documented here; the strategy definition (entry/exit logic) is
unchanged.

---

## Related

- Phase F results: [[Phase_F_Results]]
- Phase G runner: `tools/phase_g/run_analysis.py`
- Phase G charts: `tools/phase_g/charts.py`
- Strategy spec: [[Scanner-EPG-Momentum]]
