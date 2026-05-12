---
tags:
  - type/results
  - domain/backtest
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/complete
created: 2026-05-07
last_reviewed: 2026-05-07
---

# Phase A Results — EPG + LULD Baseline (No EXIT_D)

## Purpose

Establish the EPG + LULD baseline for the Scanner × EPG × LULD momentum strategy with
EXIT_D disabled. Phase A answers: **what does the strategy earn when the only active
exits are LULD proximity and EPG window close?** This isolates the cost of LULD fires
and provides a clean reference PF before EXIT_D is reintroduced.

EXIT_D config: `"enabled": false` in `config/strategy.json`. Hawkes replay continues to
run on every event (online MLE refitting active); only the EXIT_D decision block is gated.

---

## Run Parameters

| Parameter | Value |
|-----------|-------|
| Split | val |
| Sample | 100 events (stratified random, seed=42) |
| Year distribution | 16 × 2023, 84 × 2024 |
| EXIT_D enabled | **false** |
| EXIT_D theta | 0.65 (unused) |
| EXIT_D tau_min | 4.0s (unused) |
| LULD proximity threshold | 2.0% |
| LULD ref_window_sec | 300s |
| LULD warmup_sec | 60s |
| Gap gate threshold | 30% intraday |
| EPG k | 5 |
| EPG τ | 300s |
| EPG p | 0.65 |
| EPG warmup | 300s |
| Hawkes β | 0.1 (fixed) |
| Hawkes ρ | 0.99 |
| Refit interval | 50 trades |
| Cold-start size | 1,000 trades |
| Runner | `backtest/runner.py` |
| Config | `config/strategy.json` |
| Elapsed | 1,791s (~29.9 min) |

---

## Results Summary

| Metric | Phase A |
|--------|---------|
| Events input | 100 |
| Events with trades (true) | **51** |
| Events with trades (runner) | 81 ¹ |
| Events skipped | 18 |
| Events errored | 1 |
| **Trades** | **385** |
| **Profit factor** | **1.0387** |
| Win rate | 42.6% |
| Mean PnL | +0.0657% |
| Median PnL | 0.0% |
| Total PnL (summed) | +25.31% |
| Max win | +65.32% |
| Max loss | −24.14% |
| Mean hold | 322.5s |
| Median hold | 34.7s |

¹ Runner overcounting: `n_events_with_trades=81` counts all events that returned a result
including 0-trade events (EPG PASS windows fully blocked by gap gate). True chartable
events = 51 unique (ticker, date) pairs in `per_trade.parquet`.

---

## Exit Reason Breakdown

| Exit Reason | Trades | % of Total | Profit Factor | Mean PnL |
|-------------|--------|-----------|---------------|----------|
| EPG window close | 351 | 91.2% | **1.4751** | +0.596% |
| LULD proximity | 34 | 8.8% | **0.1413** | −5.409% |

**LULD exits are highly destructive.** 34 fires at PF=0.14 and mean −5.4% per trade
drags the overall PF from ~1.47 (EPG-only) down to 1.04. This is the dominant
finding of Phase A.

---

## Session Breakdown

| Session | Trades | Profit Factor | Mean PnL | Win Rate |
|---------|--------|--------------|----------|----------|
| Regular hours (RTH) | 280 | 1.0753 | +0.116% | 45.71% |
| Pre-market | 104 | 1.0088 | +0.018% | 34.62% |
| Post-market | 1 | 0.000 | −9.091% | 0.00% |

Pre-market underperforms RTH even without EXIT_D active, suggesting lower win rates
are structural (thinner order flow, wider spreads, greater adverse excursions).
RTH PF=1.075 is acceptable as a baseline but still dragged by LULD fires.

---

## Gap Gate Analysis

| Metric | Value |
|--------|-------|
| Total PASS edges evaluated | 971 |
| Blocked by gap < 30% | 586 (60.4%) |
| Entries taken immediately | 345 |
| Entries queued (gap reached later) | 40 (10.4% of taken entries) |
| Intraday % at entry — mean | 172.1% |
| Intraday % at entry — median | 53.6% |
| Intraday % at entry — p25 | 39.2% |
| Intraday % at entry — p75 | 105.9% |
| Intraday % at entry — min | 30.0% |
| Intraday % at entry — max | 2,093.6% |

60% of EPG PASS edges are blocked by the gap gate — only the extreme-move events
convert to trades. The high mean intraday % (172%) reflects the long tail of
multi-bagger events in the sample; median 53.6% is more representative.

Queued entry quality (40 entries that waited for gap to reach 30%) not yet compared
against immediate entries.

---

## Skip and Error Analysis

**18 skipped, 1 errored** out of 100 events.

| Reason | Count | Examples |
|--------|-------|---------|
| `setup_filter_fail` | 9 | RVPHW, BBLGW, PYPD, BFRGW, ENGNW, ZCMD, FBYDW, IDAI, FGI |
| `missing_prev_close` | 8 | CCCC, ELAB, SOGP, APGE, BCG, GCTS, GDHG, CDT |
| `insufficient_trades` | 1 | NTRBW |
| `error` | 1 | ATLN 2024-06-26 (missing `quotes.parquet`) |

ATLN error is a data gap — `filtered/ATLN_2024-06-26_53.50/quotes.parquet` does not
exist on disk. Not a code crash.

---

## Comparison to Parent Phases

| Phase | EXIT_D | LULD | n_trades | PF | Win% | Mean PnL | RTH PF | PM PF |
|-------|--------|------|----------|----|------|----------|--------|-------|
| Phase S (baseline) | ✗ | ✗ | 345 | **1.2709** | 43.5% | +0.38% | 1.16 | 1.73 |
| Phase U (default) | ✓ θ=0.75 τ=8s | ✓ | 385 | 1.0962 | 43.1% | +0.133% | — | 0.90 |
| T10 best combo | ✓ θ=0.65 τ=4s | ✓ | — | **1.3848** | — | — | — | — |
| **Phase A (this run)** | ✗ | ✓ | **385** | **1.0387** | **42.6%** | **+0.066%** | **1.075** | **1.009** |

Key comparisons:
- **Phase A vs Phase S**: Adding LULD exit drops PF 1.2709→1.0387 and n_trades stays
  near-identical (LULD replaces EPG close exits, not adds new trades). LULD alone costs
  ~23 PF points on this sample.
- **Phase A vs Phase U default**: Phase A has no EXIT_D but same LULD. PF nearly
  identical (1.04 vs 1.10), suggesting EXIT_D (theta=0.75) adds marginal value over
  EPG-only when LULD drag is present.
- **Phase A vs T10**: T10 includes both EXIT_D (theta=0.65) and LULD. Its higher PF
  (1.3848) implies EXIT_D (theta=0.65) successfully avoids the worst LULD fire scenarios
  by exiting earlier, before price reaches LULD proximity.

---

## Key Findings

### F1 — LULD Exits Are Highly Destructive

34 LULD fires at PF=0.14 and mean −5.4% represent a material drag. Without LULD,
the EPG window close alone would produce PF≈1.47. LULD proximity detection is
triggering exits at price extremes that often recover rather than halt.

**Hypothesis A:** The 5-minute rolling reference price (ref_window_sec=300s) drifts
upward during strong momentum runs, causing the LULD band to track price up. Proximity
fires when price pulls back toward the band — precisely the wrong signal.

**Hypothesis B:** LULD fires predominantly in RTH, but proximity to lower band during a
30%+ gap-up event is rare unless price has already reversed significantly. These may be
late-momentum entries that are already failing.

### F2 — EPG Window Close Is the Right Exit Mechanism

351 trades at PF=1.475, mean +0.596% — EPG window close is the value generator.
The gate correctly identifies when participation has dropped and exits cleanly.

### F3 — Pre-Market PF Below RTH Even Without EXIT_D

Pre-market PF=1.009 vs RTH PF=1.075. Phase S pre-market was PF=1.73. The regression
persists even without EXIT_D, which means either: (a) LULD fires are concentrated in
pre-market, or (b) the 2024 pre-market events in this sample are structurally weaker.
This contradicts the earlier hypothesis that EXIT_D was solely responsible for pre-market
regression.

### F4 — Median Hold 34.7s vs Mean 322.5s

The distribution is extremely right-skewed. Most trades exit quickly via LULD proximity
(fast bad exits) or short EPG windows. A small number of long-duration EPG windows
(up to ~1,800s) pull the mean up significantly.

---

## Output Files

| File | Contents |
|------|----------|
| `results/phase_a/summary.json` | Phase-level summary (PF, n_trades, exit breakdown) |
| `results/phase_a/run_summary.json` | Full run summary with gap gate and session breakdown |
| `results/phase_a/trade_log.json` | 385 trades: ticker, date, entry_ts, exit_ts, exit_reason, pnl_pct, hold_sec |
| `results/phase_a/per_trade.parquet` | 385 rows × 24 columns (full trade-level data) |
| `results/phase_a/per_event_summary.json` | 81 events: PASS windows, gap gate blocks, n_trades per event |
| `results/phase_a/skipped_events.json` | 19 skipped/errored events with reasons |
| `results/phase_a/charts/` | 51 interactive HTML charts (one per event with trades) |

---

## Open Questions

1. **Why are LULD fires destructive?** Are they concentrated in pre-market (no RTH
   filter overlap), or in specific price ranges? Needs exit-level analysis by session,
   intraday%, and time-since-entry.

2. **Why did pre-market regress from Phase S PF=1.73 to Phase A PF=1.009?** If EXIT_D
   is off, what changed? Possible: LULD fires are pre-market concentrated. Or the 2024
   vintage pre-market events (84 of 100) are weaker than the 2023 events Phase S saw.

3. **What is the quality of queued gap-gate entries vs immediate entries?** 40 queued
   entries (10.4%) — are they worse (chasing a move that already peaked) or better
   (capturing continuation after confirming gap)?

4. **Can proximity_pct_threshold be raised to reduce destructive fires without missing
   real halt-proximity situations?** 2% threshold may be too tight for volatile
   momentum stocks with 30%+ gaps.

---

## Next Steps

| Priority | Task |
|----------|------|
| High | Analyze 34 LULD fires: session, intraday%, time-since-entry, whether price recovered post-exit |
| High | Re-run Phase U equivalent (EXIT_D theta=0.65 tau=4s) on same 100-event val to confirm T10 result holds vs Phase A baseline |
| Medium | Analyze pre-market LULD fire concentration (are PM fires driving most of the drag?) |
| Medium | Test raising LULD proximity_pct_threshold from 2% → 3% or 4% |
| Low | Analyze queued gap-gate entry quality vs immediate entries |

---

## Related

- Strategy spec: [[Scanner-EPG-Momentum]]
- Project directory: [[scanner-epg-momentum/docs/Project_Directory|Project Directory]]
- Parent Phase S: [[Phase_S_Results]]
- Parent Phase U: [[Phase_U_Results]]
- T10 sweep results: `hawkes-ofi-impact/results/phase_t/` (theta/tau grid)
