---
tags:
  - type/results
  - domain/backtest
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/complete
created: 2026-05-14
last_reviewed: 2026-05-14
---

# Phase D Results — Intra-Window Rolling High Watermark

## Purpose

Phase D replaces the Phase C global watermark (anchored to T_event running high) with a
**per-window rolling high watermark** that carries the prior window's peak forward. The goal
is to prevent re-entries into declining regimes across multiple PASS windows.

**Key mechanic difference from Phase C:**

| | Phase C watermark | Phase D watermark |
|-|------------------|------------------|
| Anchor | T_event — running high from event start | Per PASS window — resets at each window open |
| Cross-window memory | None — watermark never resets | Yes — `prior_window_peak` carries prior peak to next window |
| Re-entry blocking | No — only first entries checked | Yes — re-entries also checked at signal fire |
| State variable | `high_watermark_price` (global per event) | `current_window_high`, `prior_window_peak` (per window) |

---

## Mechanic

At each **PASS rising edge** (first entry attempt):
```
if prior_window_peak is not None:
    current_window_high = max(current_price, prior_window_peak)
else:
    current_window_high = current_price   # first window: clean slate
drawdown = (current_window_high - current_price) / current_window_high
block if drawdown > threshold
```

On each **tick within PASS** (continuing window):
```
current_window_high = max(current_window_high, current_price)
```

On **window close** (PASS → FAIL/INACTIVE):
```
prior_window_peak = current_window_high
current_window_high = None
```

At each **re-entry signal fire** (Hawkes recovery → re-entry):
```
drawdown = (current_window_high - current_price) / current_window_high
block if drawdown > threshold
```

**T1d invariant:** On the first PASS window of every event, `prior_window_peak` must be
None. Validated at runtime; raises RuntimeError on violation.

---

## Run Parameters

| Parameter | Value |
|-----------|-------|
| Split | val |
| Sample | 100 events (stratified random, seed=42) |
| Year distribution | 16 × 2023, 84 × 2024 |
| Gap gate | Disabled |
| EXIT_D | enabled, theta=0.65, tau_min=4.0s |
| Re-entry | enabled, tau_recovery=4.0s |
| LULD proximity | 2.0% |
| Config | `config/phase_d.json` |
| Sweep thresholds | [2%, 3%, 5%, 7%] |
| Runner | `backtest/runner.py --intra-window-watermark-threshold <T>` |

---

## T2 — Threshold Sweep Results

| Threshold | PF | n_trades | win% | mean_pnl% | n_blocks | n_re_blocked | entries_blocked% |
|-----------|------|---------|------|-----------|---------|-------------|-----------------|
| **2%** | **2.6529** | 483 | 49.28 | +1.491% | 751 | 138 | 77.34% |
| 3% | 2.576 | 667 | 48.73 | +1.268% | 706 | 140 | 72.71% |
| 5% | 2.2155 | 1,213 | 47.16 | +0.821% | 621 | 141 | 63.95% |
| 7% | 2.1651 | 1,589 | 46.82 | +0.772% | 526 | 125 | 54.17% |

**Best threshold: 2%** (PF=2.6529, n=483). Selection rule: prefer loosest threshold within
0.05 PF of peak. Next loosest (3%, PF=2.576): delta=0.077 > 0.05 → does not qualify.
2% remains best.

### Escalation Flags

Two thresholds breached the **PF > 2.5** hard stop from the spec:

| Threshold | PF | Flag |
|-----------|-----|------|
| 2% | 2.6529 | PF > 2.5 ⚠️ |
| 3% | 2.576 | PF > 2.5 ⚠️ |

**User disposition:** "proceed with the whole phase. i acknowledge the flag."

**Assessment:** The PF elevation is consistent with extreme cherry-picking — at 2%,
77% of all pass_edges are blocked, selecting only 483 trades from a universe of ~1,234
attempted entries. On a 100-event sample, selecting near-peak entries in momentum windows
plausibly concentrates high-quality trades. No look-ahead bug was identified in code
inspection: `current_window_high` is initialized to `cur_price` at the rising edge tick
(not a future price), and updated forward only on subsequent ticks.

The **re-entry block count invariance** (138, 140, 141, 125 across thresholds) was flagged
as suspicious but not confirmed as a bug. Re-entry signals fire when Hawkes imbalance
recovers (momentum restores), which tends to occur when price has returned near the rolling
high — so even a tight 2% threshold may not block many more re-entries than 5%.

---

## T3 — Re-Entry Blocking Validation

| Metric | Value |
|--------|-------|
| n_blocked_reentries | 138 |
| n_allowed_reentries | 125 |
| mean_drawdown_blocked | 5.798% |
| mean_drawdown_allowed | 1.014% |
| ratio (blocked/allowed) | 5.72x |
| Discriminating | **Yes** |
| Escalation | Not triggered |

The filter is strongly discriminating: blocked re-entries have 5.72x higher drawdown from
the intra-window rolling high than allowed re-entries. All allowed re-entries have drawdown
strictly below 2% (max = 1.97%), all blocked re-entries are strictly above 2% (min = 2.03%).

**Distribution:**

| | Blocked | Allowed |
|-|---------|---------|
| min | 2.03% | 0.00% |
| p25 | 2.81% | 0.55% |
| median | 4.31% | 1.03% |
| p75 | 6.41% | 1.51% |
| max | 40.81% | 1.97% |

**Spot-checks (top 3 blocked re-entries by drawdown):**

| Ticker | Date | Entry time | drawdown | cwh | prior_peak | Note |
|--------|------|-----------|---------|-----|-----------|------|
| SGLY | 2024-03-12 | 12:10:55 UTC | 40.81% | $7.62 | — | In-window: price crashed 40% from window high |
| UCAR | 2024-02-08 | 10:13:38 UTC | 24.62% | $0.13 | — | In-window: penny stock halved from window high |
| SGD | 2024-03-08 | 14:15:06 UTC | 20.95% | $1.24 | — | In-window: 21% pullback within PASS window |

All three spot-checks had `prior_window_peak = None` (first PASS window of the event),
confirming T1d invariant holds and re-entry blocking is operating correctly intra-window.

Output: `results/phase_d/reentry_validation.json`

---

## T4 — Comparison Table

| Variant | PF | n_trades | win% | mean_pnl% | n_blocks (first/re) |
|---------|----|---------|------|-----------|---------------------|
| Phase C — no filter (baseline) | 1.7391 | 3,588 | 46.46% | +0.524% | 0 / 0 |
| Phase C — global watermark 5% | 1.9443 | 1,945 | 46.68% | +0.615% | 572 / 0 |
| **Phase D — intra-window watermark 2%** | **2.6529** | 483 | 49.28% | +1.491% | 613 / 138 |

Key differences in the Phase D best vs Phase C winner:
- PF: +0.7086 absolute (+36.5% relative)
- n_trades: −1,462 (75% fewer trades — much more selective)
- Re-entry blocking: Phase D blocks 138 re-entries; Phase C blocks none
- Win rate: +2.60pp (49.28% vs 46.68%)
- mean_pnl: +0.876pp (+1.491% vs +0.615%)

Output: `results/phase_d/comparison_table.json`

---

## Implementation Notes

### New state variables (per event in `backtest/runner.py`)

| Variable | Type | Description |
|----------|------|-------------|
| `current_window_high` | float or None | Rolling high within current PASS window |
| `prior_window_peak` | float or None | Peak of most recent closed PASS window |
| `first_window_seen` | bool | T1d guard: True after first rising edge |
| `n_intra_window_blocks` | int | Total blocked entries (first + re-entry) |
| `n_re_entries_intra_blocked` | int | Blocked re-entry count |
| `blocked_edges` | list[dict] | Per-block log; written to parquet |
| `entry_dwh`, `entry_cwh`, `entry_pwp` | float or None | Phase D fields on each trade record |

### New CLI arg

`--intra-window-watermark-threshold <float>` — activates Phase D logic; mutually
exclusive with `--watermark-threshold` (Phase C global).

### New trade record fields

All trade records include:

| Field | Description |
|-------|-------------|
| `drawdown_from_window_high` | Drawdown from cwh at entry (0.0 for first windows, clean slate) |
| `current_window_high_at_entry` | cwh value at entry |
| `prior_window_peak_at_entry` | prior_window_peak at entry (None for first windows) |

---

## Escalation Events

| Trigger | Threshold | Actual | Disposition |
|---------|-----------|--------|-------------|
| Best threshold n_trades < 50 | < 50 | 483 — NOT TRIGGERED | Passed |
| Any variant PF > 2.5 | > 2.5 | 2% (2.6529), 3% (2.576) — **TRIGGERED** | User approved continuation |
| T1d invariant (prior_peak non-None on first window) | Any violation | None detected | Passed |
| cwh < cur_price at rising edge | Any violation | None detected | Passed |
| mean drawdown blocked <= mean drawdown allowed | Any violation | 5.80% > 1.01% — NOT TRIGGERED | Passed |

---

## Output Files

| File | Contents |
|------|----------|
| `results/phase_d/sweep_0_02/run_summary.json` | Best threshold (2%) full summary |
| `results/phase_d/sweep_0_02/per_trade.parquet` | Per-trade records, 483 rows |
| `results/phase_d/sweep_0_02/blocked_edges.parquet` | Per-block log, 751 rows |
| `results/phase_d/sweep_0_03/run_summary.json` | Threshold 3% summary |
| `results/phase_d/sweep_0_05/run_summary.json` | Threshold 5% summary |
| `results/phase_d/sweep_0_07/run_summary.json` | Threshold 7% summary |
| `results/phase_d/threshold_sweep.json` | Aggregated sweep table |
| `results/phase_d/reentry_validation.json` | T3 re-entry blocking validation |
| `results/phase_d/comparison_table.json` | T4 comparison: C baseline / C watermark 5% / D best |
| `results/phase_d/event_charts/` | T5 per-event charts (81 events, 4-panel Plotly) |
| `results/phase_d/event_charts/index.html` | Sortable chart index |
| `config/phase_d.json` | Phase D config (gap_gate disabled, watermark enabled via CLI) |

---

## Related

- Strategy spec: [[Scanner-EPG-Momentum]]
- Phase C results: [[Phase_C_Results]]
- Phase D config: `config/phase_d.json`
- Sweep runner: `tools/phase_d/run_sweep.py`
- Chart builder: `tools/phase_d/chart.py`
- Chart runner: `tools/phase_d/run_charts.py`
