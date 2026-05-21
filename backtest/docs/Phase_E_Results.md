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

# Phase E Results — Symmetric Spread-Multiple LULD Proximity Exit

## Purpose

Phase E replaces the Phase D fixed-proximity LULD exit (lower band only, 2% threshold)
with a **symmetric spread-multiple buffer** applied to both upper and lower bands.

**Key changes from Phase D LULD:**

| | Phase D LULD | Phase E LULD |
|-|--------------|--------------|
| Band coverage | Lower band only | Both lower AND upper bands |
| Trigger logic | price < lower_band * (1 - proximity_pct) | price < lower_band + N*spread OR price > upper_band - N*spread |
| Buffer | Fixed 2% of price | N × prevailing bid-ask spread |
| Spread fallback | N/A | If bid/ask invalid, buffer=0 (fires only at band) |
| Exit reason | `luld_proximity` | `luld_lower` or `luld_upper` (tracked separately) |
| RTH only | Yes | Yes |

**Sweep parameter:** N ∈ {1, 2, 3} spread multiples.
**Primary success metric:** Best N variant PF ≥ Phase D baseline (2.6529).

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
| Intra-window watermark | 2% (Phase D best) |
| LULD ref_window_sec | 300s |
| LULD warmup_sec | 60s |
| LULD RTH only | true |
| Config | `config/phase_e.json` |
| Sweep multiples | N = 1, 2, 3 |
| Runner | `backtest/runner.py --luld-n-spread-multiple N` |

---

## Sweep Results

### Sweep Comparison Table

| N | PF | n_trades | win% | mean_pnl% | luld_lo | luld_hi | luld_lo_pf | luld_hi_pf | exit_d | exit_d_pf | fallback_ticks |
|---|-----|---------|------|-----------|---------|---------|-----------|-----------|--------|-----------|----------------|
| Phase D (2% prox, lower only) | **2.6529** | 483 | 49.28 | +1.491% | 13* | 0 | 0.71* | — | 270 | 3.074 | — |
| 1 | **1.9271** | 476 | 48.32 | +0.838% | 20 | 47 | 0.0589 | 12.5712 | 230 | 2.0991 | 266,955 |
| 2 | 1.7509 | 467 | 47.11 | +0.638% | 33 | 71 | 0.2740 | 4.7440 | 203 | 1.9267 | 266,955 |
| 3 | 1.7901 | 449 | 46.77 | +0.636% | 48 | 81 | 0.4302 | 8.0894 | 173 | 1.8426 | 266,955 |

\* Phase D reported these as `luld_proximity` (lower band only, fixed threshold).

### Best N Selection

Selection rule: maximum PF; prefer loosest N within 0.05 PF of best.

- N=1: PF=1.9271 (best)
- N=2: PF=1.7509 (delta=0.1762 > 0.05 — does not qualify)
- N=3: PF=1.7901 (delta=0.1370 > 0.05 — does not qualify)

**Winner: N=1** (PF=1.9271).

### Escalation Status

| Condition | Threshold | Status |
|-----------|-----------|--------|
| Any variant PF > 3.0 | > 3.0 | Not triggered |
| Best variant n_trades < 50 | < 50 | 476 — not triggered |
| Best variant PF < 2.20 | < 2.20 | **TRIGGERED** — N=1 best PF=1.9271 < 2.20 |
| pytest failures | Any | Not triggered (78/78 pass) |
| Per-event fallback rate > 10% | > 10% | LVTX 2024-01-25 = 16.4% (log warning; not a sweep hard stop) |

---

## Per-Variant Details

### N=1 (1 spread wide)

| Metric | Value |
|--------|-------|
| PF | 1.9271 |
| n_trades | 476 |
| win_rate | 48.32% |
| mean_pnl_pct | +0.838% |
| luld_fallback_ticks | 266,955 |

**Exit reason breakdown:**

| Exit | Count | % | PF | mean_pnl% |
|------|-------|---|----|-----------|
| exit_d | 230 | 48.32 | 2.0991 | +1.100% |
| epg_window_close | 179 | 37.61 | 1.4388 | +0.248% |
| luld_upper | 47 | 9.87 | 12.5712 | +3.835% |
| luld_lower | 20 | 4.20 | 0.0589 | -3.930% |

**Key observation — asymmetric LULD PF:**
- `luld_upper`: PF=12.57 — stocks running into upper LULD territory are massive momentum winners
- `luld_lower`: PF=0.06 — stocks hitting lower trigger are almost universally losses

**Hypothesis:** The 20 lower-band exits pre-empt EXIT_D, which in Phase D handled similar declining trades at PF=3.07. When lower-band triggers fire first (before EXIT_D), they take trades at a worse exit point. The lower band exit is not adding value for the lower side.

---

### N=2 (2 spreads wide)

| Metric | Value |
|--------|-------|
| PF | 1.7509 |
| n_trades | 467 |
| win_rate | 47.11% |
| mean_pnl_pct | +0.638% |
| luld_fallback_ticks | 266,955 |

**Exit reason breakdown:**

| Exit | Count | % | PF | mean_pnl% |
|------|-------|---|----|-----------|
| exit_d | 203 | 43.47 | 1.9267 | +1.000% |
| epg_window_close | 160 | 34.26 | 1.4042 | +0.223% |
| luld_upper | 71 | 15.20 | 4.7440 | +1.465% |
| luld_lower | 33 | 7.07 | 0.2740 | -1.357% |

**Key observation:** Wider buffer fires upper trigger more frequently (71 vs 47 at N=1) but at
lower PF (4.74 vs 12.57). Lower trigger fires also increase (33 vs 20) with improved but still
sub-1.0 PF (0.274 vs 0.059). The lower band continues to destroy value vs EXIT_D.

---

### N=3 (3 spreads wide)

| Metric | Value |
|--------|-------|
| PF | 1.7901 |
| n_trades | 449 |
| win_rate | 46.77% |
| mean_pnl_pct | +0.636% |
| luld_fallback_ticks | 266,955 |

**Exit reason breakdown:**

| Exit | Count | % | PF | mean_pnl% |
|------|-------|---|----|-----------|
| exit_d | 173 | 38.53 | 1.8426 | +0.990% |
| epg_window_close | 147 | 32.74 | 1.4078 | +0.231% |
| luld_upper | 81 | 18.04 | 8.0894 | +1.402% |
| luld_lower | 48 | 10.69 | 0.4302 | -0.696% |

**Key observation:** N=3 is non-monotone vs N=2: PF=1.79 vs 1.75. luld_upper PF recovers
to 8.09 (vs 4.74 at N=2) with more fires (81 vs 71) — the N=3 buffer selects a different
mix of upper-band events than N=2. luld_lower PF continues improving (0.430 vs 0.274) but
remains net-negative. EXIT_D PF degrades monotonically as N increases (2.099 → 1.927 → 1.843),
consistent with lower-band exits intercepting progressively more EXIT_D candidates.

---

## Key Findings

### F1 — LULD Upper Band Exit Is Highly Valuable

PF=12.57 on 47 upper-band exits (9.87% of trades). These represent stocks that rallied
sharply to within N spreads of the LULD upper band — a regime where EXIT_D (Hawkes sell
imbalance) may not fire (stock still buy-dominated), and EPG window may stay open (high
participation). The upper-band exit catches these parabolic moves efficiently.

Phase D had zero upper-band exits (upper band was never checked). This is a genuine Phase E
contribution.

### F2 — LULD Lower Band Exit Destroys Value

PF=0.06 on 20 lower-band exits (4.20% of trades). These exits pre-empt EXIT_D, which in
Phase D delivered PF=3.07 on 270 trades. The lower-band trigger fires before sell imbalance
has time to manifest as a sustained EXIT_D signal. The stock has declined sharply enough to
trigger lower proximity, but EXIT_D would have fired more profitably (or not at all, with
the trade recovering).

Alternatively: some lower-band fires may be in thin pre-market-like RTH conditions where
the spread is wide and the trigger fires prematurely.

### F3 — Overall PF Regression vs Phase D

N=1 PF=1.9271 vs Phase D PF=2.6529 — a drop of 0.7258 (27% relative decline).
The exit_d PF itself drops from 3.074 (Phase D) to 2.099 (Phase E N=1), suggesting
EXIT_D is being pre-empted by LULD lower on its best trades. The net effect of adding the
symmetric LULD exit in N=1 configuration is negative.

### F4 — luld_upper PF Is Non-Monotone in N

| N | luld_upper count | luld_upper PF |
|---|------------------|---------------|
| 1 | 47 | 12.5712 |
| 2 | 71 | 4.7440 |
| 3 | 81 | 8.0894 |

Wider N fires earlier (upper_trigger = upper_band - N*spread, so larger N = lower threshold =
earlier fire). N=1 catches only the strongest parabolic runs (all the way near the band), giving
the highest PF. N=2 casts a wider net and includes reversing stocks, collapsing PF. N=3's
recovery to 8.09 is non-monotone — likely a sampling artifact in the 100-event window.
The pure upper-band signal is highest-quality at N=1.

### F5 — luld_lower PF Improves With N but Remains Net-Negative

| N | luld_lower count | luld_lower PF | luld_lower mean_pnl% |
|---|------------------|---------------|----------------------|
| 1 | 20 | 0.0589 | -3.930% |
| 2 | 33 | 0.2740 | -1.357% |
| 3 | 48 | 0.4302 | -0.696% |

Wider N raises lower_trigger (lower_band + N*spread), so fire occurs earlier before the trade
has declined as far. At N=3, trades exit at shallower losses (-0.70% mean) — still below
breakeven (PF=0.43) but less catastrophic than N=1 (-3.93% mean). Even so, all N variants
are net-negative for the lower band; none clears PF=1.0. The lower-band exit should not be
included in any follow-on phase.

---

## Output Files

| File | Contents |
|------|----------|
| `results/phase_e/sweep_N1/run_summary.json` | N=1 full summary |
| `results/phase_e/sweep_N1/per_trade.parquet` | N=1 per-trade records |
| `results/phase_e/sweep_N1/blocked_edges.parquet` | N=1 blocked entries |
| `results/phase_e/sweep_N2/run_summary.json` | N=2 full summary |
| `results/phase_e/sweep_N2/per_trade.parquet` | N=2 per-trade records |
| `results/phase_e/sweep_N2/blocked_edges.parquet` | N=2 blocked entries |
| `results/phase_e/sweep_N3/run_summary.json` | N=3 full summary |
| `results/phase_e/sweep_N3/per_trade.parquet` | N=3 per-trade records |
| `results/phase_e/sweep_N3/blocked_edges.parquet` | N=3 blocked entries |
| `results/phase_e/sweep_summary.json` | Aggregated sweep table |
| `results/phase_e/event_charts_N1/` | Per-event 4-panel charts for best N (pending) |
| `config/phase_e.json` | Phase E config |
| `tools/phase_e/run_sweep.py` | Sweep runner |
| `tools/phase_e/chart.py` | Phase E chart builder (LULD bands + symmetric exit markers) |
| `tools/phase_e/run_charts.py` | Chart runner for best N |

---

## Related

- Strategy spec: [[Scanner-EPG-Momentum]]
- Phase D results: [[Phase_D_Results]]
- Phase E config: `config/phase_e.json`
- Sweep runner: `tools/phase_e/run_sweep.py`
- Chart builder: `tools/phase_e/chart.py`
- Chart runner: `tools/phase_e/run_charts.py`
