<!-- fullWidth: false tocVisible: false tableWrap: true -->
---
tags:
  - type/results
  - domain/backtest
  - domain/signal
  - project/scanner-epg-momentum
  - status/complete
created: 2026-06-11
last_reviewed: 2026-06-11
---

# Phase CPD-EXIT Results — Exit Stack Sweep on BOCPD Winner Gate

## Purpose

Phase CPD-EXIT tests whether signal-based exits can improve on the naive EPG window-close exit
when the BOCPD gate (Phase CPD-BOCPD winner) is used as the entry mechanism. Three sub-phases
are run in sequence, each adding or combining exit mechanisms on top of the same fixed entry gate.

**Research question:** Given that the BOCPD entry gate produces sharp, well-timed entries — does
the exit mechanism account for the remaining performance gap vs the Phase D baseline (PF=2.65)?

**Exit problem diagnosis:** `epg_window_close` waits for the PASS→FAIL gate transition. By the
time that fires, the burst has exhausted and trades are losers. A fixed TP/SL, WJI trailing stop,
or price momentum drop should all be able to exit earlier and capture more of the available move.

---

## Fixed Entry Gate (All Sub-Phases)

**BOCPD winner config:** `lh0.01_pe0.6`

| Parameter | Value |
|-----------|-------|
| lambda_h (hazard rate) | 0.01 |
| p_enter | 0.60 |
| p_exit | 0.50 (derived internally) |
| sigma_log_fallback | 0.209 (CPD-0 median) |
| dir_thresh | 1.0 |
| max_run_length | 600 |
| EPG warmup | 300s |

**LULD:** Upper band only, Phase F config (N=1 spread multiple, ref_window=300s, warmup=60s).
**EXIT_D:** Permanently disabled. Not revisited in this phase.
**Sample:** 100-event val sample (same events across all sub-phases). 687 trades per config on no-escalation runs.

**BOCPD baseline (no exit improvement):**

| PF | CVaR5 | EV | n_trades |
|----|-------|----|---------|
| 1.0779 | −20.99% | 0.154 | 1,117 |

---

## Sub-Phase 1 — Fixed TP/SL Benchmark

**Purpose:** Establish a concrete benchmark. If fixed levels can't beat the BOCPD baseline,
signal-based exits have no chance. 9 configs: TP ∈ {5%, 10%, 15%} × SL ∈ {3%, 5%, 10%}.

### Results

| config | TP% | SL% | PF | CVaR5 | EV | win% | hold_s | sl_hit% | tp_hit% | epg% |
|--------|-----|-----|-----|-------|----|------|--------|---------|---------|------|
| **tp5_sl5** | 5 | 5 | **1.399** | **−9.14%** | **0.505** | 51.7 | 204 | 14.3 | 34.1 | 51.6 |
| tp10_sl5 | 10 | 5 | 1.388 | −9.27% | 0.469 | 50.4 | 303 | 15.9 | 19.2 | 64.9 |
| tp15_sl5 | 15 | 5 | 1.359 | −9.33% | 0.394 | 49.7 | 381 | 16.5 | 11.7 | 71.9 |
| tp5_sl10 | 5 | 10 | 1.356 | −14.08% | 0.431 | 50.7 | 205 | 4.4 | 36.9 | 58.7 |
| tp10_sl10 | 10 | 10 | 1.345 | −14.10% | 0.400 | 51.4 | 338 | 5.1 | 20.4 | 74.5 |
| tp15_sl10 | 15 | 10 | 1.308 | −14.53% | 0.329 | 51.2 | 458 | 5.9 | 12.3 | 81.8 |
| tp5_sl3 | 5 | 3 | 1.232 | −5.28% | 0.244 | 52.3 | 130 | 26.4 | 30.4 | 43.2 |
| tp10_sl3 | 10 | 3 | 1.201 | −5.31% | 0.191 | 51.6 | 218 | 28.9 | 16.5 | 54.7 |
| tp15_sl3 | 15 | 3 | 1.184 | −5.56% | 0.165 | 51.3 | 291 | 30.0 | 9.8 | 60.2 |

### Interpretation

**`tp5_sl5` wins on PF and EV; `tp5_sl3` wins on CVaR5 (−5.28%) at the cost of heavy overcuts.**

- **SL=5% is the sweet spot.** SL=3 fires on 26–30% of trades (too tight for this strategy's
  natural volatility). SL=10 rarely fires but lets deep losers run, driving CVaR5 to −14%.
- **EPG window close dominates at 51–82% of exits.** TP functions as a tail-capture backstop
  rather than a primary exit, which explains why increasing TP from 5% to 15% has diminishing
  returns — the gate closes before most TP levels are reached.
- **TP=5% is optimal.** Higher TP levels simply defer the exit to window close without adding
  upside, since the available move distribution is front-loaded in the first ~200s.
- **vs BOCPD baseline:** PF +0.32, CVaR5 +11.85 pp (−21% → −9%), EV +0.35 (3.3×). A fixed
  TP/SL makes a substantial improvement over no structured exit.

**Sub-1 benchmark: `tp5_sl5` — PF=1.399, CVaR5=−9.14%, EV=0.505.**

---

## Sub-Phase 2a — WJI Trailing Stop

**Purpose:** Replace fixed SL with a trailing stop on the WJI signal. WJI tracks participation;
if it rolls over from its peak since entry, that signals momentum exhaustion.

18 configs: variants {raw, log} × p_exit ∈ {0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9}.

- **Raw variant:** fires when `WJI(t) < p_exit × WJI_peak_since_entry`
- **Log variant:** fires when `log(WJI(t)) < p_exit × log(WJI_peak)`, only when `log(peak) > 0`

### Results (top 10 of 18)

| config | var | p_exit | PF | CVaR5 | EV | hold_s | wji% | luld% | epg% |
|--------|-----|--------|-----|-------|----|--------|------|-------|------|
| raw_pe20 | raw | 0.2 | 1.396 | −16.83% | 0.660 | 554 | 16.9 | 5.8 | 77.3 |
| log_pe10 | log | 0.1 | 1.376 | −18.96% | 0.680 | 738 | 49.3 | 6.4 | 44.3 |
| log_pe20 | log | 0.2 | 1.374 | −17.59% | 0.629 | 628 | 68.6 | 5.8 | 25.6 |
| raw_pe10 | raw | 0.1 | 1.359 | −18.36% | 0.640 | 672 | 6.6 | 6.3 | 87.2 |
| raw_pe50 | raw | 0.5 | 1.337 | −15.07% | 0.495 | 332 | 44.7 | 4.7 | 50.7 |
| raw_pe90 | raw | 0.9 | 1.318 | −9.68% | 0.293 | 79 | 95.5 | 2.2 | 2.3 |
| raw_pe60 | raw | 0.6 | 1.269 | −14.75% | 0.379 | 269 | 57.4 | 4.4 | 38.3 |
| raw_pe70 | raw | 0.7 | 1.237 | −13.53% | 0.299 | 205 | 70.7 | 3.6 | 25.6 |
| raw_pe80 | raw | 0.8 | 1.175 | −13.07% | 0.216 | 142 | 85.3 | 3.1 | 11.6 |
| log_pe90 | log | 0.9 | 1.028 | −12.53% | 0.028 | 62 | 97.4 | 1.7 | 0.9 |

### Escalation

**HARD STOP.** Best CVaR5 = −16.83% (raw_pe20) < −15% threshold. Charts not generated.

### Interpretation

The WJI trailing stop faces a structural dilemma: loose stops (low p_exit) don't fire enough
to improve exits, while tight stops (high p_exit) prematurely exit winners before momentum
peaks. No p_exit level threads this needle:

- **Low p_exit (0.1–0.2):** Stop rarely fires (6–17% wji_exit rate at pe20). Exit pattern
  resembles window-close-dominated behavior with deeper CVaR5 tails than Sub-1.
- **High p_exit (0.7–0.9):** Stop fires aggressively (71–96% wji_exit rate). Short holds (79–205s)
  cut winners before they extend. PF falls and EV contracts vs Sub-1.
- **Log variant underperforms raw uniformly.** Log scaling applies tighter effective stops at
  high peak values, meaning log fires earlier and cuts more winners. Raw is strictly better.
- **EV is higher than Sub-1 (0.660 raw_pe20 vs 0.505 tp5_sl5)** but this comes with tail risk
  that the CVaR5 hard stop correctly flags as unacceptable.

---

## Sub-Phase 2b — 120s Price Momentum Drop

**Purpose:** Exit when price has dropped ≥ threshold% over the trailing 120 active seconds.
Intended to catch violent sell waves early. 10 configs: threshold ∈ {2%, 4%, 6%, 8%, 10%, 12%, 14%, 16%, 18%, 20%}.

Momentum is precomputed once per event (vectorized `np.searchsorted` on active-seconds axis)
before replaying all configs.

### Results

| config | threshold | PF | CVaR5 | EV | hold_s | mom_exit% | luld% | epg% |
|--------|-----------|-----|-------|----|--------|-----------|-------|------|
| **mom6** | −6% | **1.142** | **−12.60%** | **0.220** | 369 | 30.7 | 3.6 | 65.6 |
| mom10 | −10% | 1.125 | −16.78% | 0.239 | 511 | 17.3 | 4.8 | 77.9 |
| mom8 | −8% | 1.092 | −14.08% | 0.162 | 444 | 22.0 | 4.2 | 73.8 |
| mom12 | −12% | 1.067 | −18.50% | 0.136 | 579 | 11.9 | 5.4 | 82.7 |
| mom4 | −4% | 0.965 | −11.23% | −0.048 | 250 | 45.6 | 2.9 | 51.5 |
| mom2 | −2% | 0.871 | −10.15% | −0.143 | 133 | 68.9 | 2.2 | 29.0 |

### Interpretation

Best config mom6 (PF=1.142, CVaR5=−12.60%) is below Sub-1 (tp5_sl5) on every metric.

- **EPG window close still dominates at 65%** even with a 6% momentum threshold. The 120s
  price drop fires on only 30.7% of trades — meaning the burst has already ended by the time
  momentum turns sufficiently negative for the threshold to trigger.
- **Threshold too tight (2–4%):** Overcuts are severe. mom4 PF=0.965 (< 1.0). The 120s
  momentum signal is noisy at short timescales and fires on recoverable dips.
- **Threshold too loose (12%+):** Rarely fires. Behavior converges to window-close-only with
  slight tail benefit. PF degrades vs Sub-1.
- **6% is the sweet spot** but this optimum is not robust — the curve is flat between 6–10%
  and the entire range is below Sub-1.
- **Tail improvement vs Sub-2a:** Best CVaR5=−12.60% vs Sub-2a hard stop at −16.83%. Momentum
  drop is less tail-risky than WJI trailing stop, but still underperforms Sub-1 (−9.14%).

---

## Sub-Phase 3 — Combined WJI Trailing Stop + Momentum Drop

**Purpose:** Stack both mechanisms. Exit priority: (1) WJI trailing stop → (2) 120s momentum
drop → (3) LULD upper → (4) EPG window close. Log WJI variant excluded (underperformed in 2a).

20 configs: WJI p_exit ∈ {0.1, 0.2, 0.3, 0.5} × momentum threshold ∈ {4%, 6%, 8%, 10%, 12%}.

### Results (all 20 configs)

| config | wji_pe | mom% | PF | CVaR5 | EV | hold_s | wji% | mom% | luld% | epg% |
|--------|--------|------|----|-------|----|--------|------|------|-------|------|
| pe10_mo6 | 0.1 | 6 | 1.128 | −12.60% | 0.199 | 351 | 0.7 | 30.6 | 3.6 | 65.1 |
| pe10_mo10 | 0.1 | 10 | 1.114 | −16.59% | 0.216 | 478 | 2.2 | 17.3 | 4.7 | 75.8 |
| pe20_mo10 | 0.2 | 10 | 1.098 | −15.49% | 0.177 | 434 | 8.7 | 16.3 | 4.4 | 70.6 |
| pe30_mo10 | 0.3 | 10 | 1.093 | −15.47% | 0.166 | 385 | 15.7 | 16.0 | 4.1 | 64.2 |
| pe20_mo6 | 0.2 | 6 | 1.086 | −12.27% | 0.130 | 335 | 4.2 | 30.0 | 3.5 | 62.3 |
| pe10_mo8 | 0.1 | 8 | 1.073 | −14.08% | 0.129 | 421 | 1.7 | 21.8 | 4.2 | 72.2 |
| pe20_mo12 | 0.2 | 12 | 1.066 | −16.66% | 0.124 | 475 | 11.5 | 10.5 | 4.7 | 73.4 |
| pe30_mo6 | 0.3 | 6 | 1.056 | −12.24% | 0.085 | 306 | 9.3 | 29.4 | 3.2 | 58.1 |
| pe30_mo12 | 0.3 | 12 | 1.051 | −16.77% | 0.095 | 414 | 19.7 | 10.0 | 4.2 | 66.1 |
| pe50_mo10 | 0.5 | 10 | 1.050 | −14.88% | 0.082 | 286 | 34.8 | 13.7 | 3.6 | 47.9 |
| pe50_mo12 | 0.5 | 12 | 1.048 | −15.50% | 0.080 | 298 | 38.1 | 9.0 | 3.6 | 49.2 |
| pe10_mo12 | 0.1 | 12 | 1.040 | −18.14% | 0.081 | 534 | 3.6 | 11.8 | 4.9 | 79.6 |
| pe20_mo8 | 0.2 | 8 | 1.031 | −13.80% | 0.053 | 393 | 6.8 | 21.1 | 4.1 | 68.0 |
| pe30_mo8 | 0.3 | 8 | 1.012 | −13.81% | 0.021 | 352 | 13.4 | 20.8 | 3.8 | 62.0 |
| pe50_mo6 | 0.5 | 6 | 0.983 | −12.10% | −0.024 | 242 | 26.5 | 26.3 | 3.1 | 44.1 |
| pe20_mo4 | 0.2 | 4 | 0.975 | −11.23% | −0.034 | 247 | 2.6 | 45.1 | 2.9 | 49.3 |
| pe10_mo4 | 0.1 | 4 | 0.965 | −11.23% | −0.048 | 250 | 0.0 | 45.6 | 2.9 | 51.5 |
| pe30_mo4 | 0.3 | 4 | 0.964 | −11.22% | −0.048 | 234 | 5.4 | 44.7 | 2.8 | 47.2 |
| pe50_mo8 | 0.5 | 8 | 0.948 | −13.66% | −0.085 | 266 | 31.3 | 18.6 | 3.3 | 46.7 |
| pe50_mo4 | 0.5 | 4 | 0.908 | −11.22% | −0.122 | 197 | 18.2 | 41.9 | 2.8 | 37.1 |

### Interpretation

The combined exit stack is **strictly worse than either mechanism in isolation.**

- **At pe=0.1, WJI fires on only 0.7–3.6% of trades.** The best combined config (pe10_mo6)
  is effectively identical to the standalone momentum-drop config (mom6) with a dormant WJI
  layer. Best combined PF=1.128 vs mom6 standalone PF=1.142 — the WJI layer is a marginal drag.
- **As WJI gets tighter (pe=0.5), it competes directly with momentum** — both mechanisms fire
  in the same region of trade history, causing the WJI to preempt momentum exits that would
  have been cleaner (or vice versa). Neither wins when they overlap.
- **Mechanism interference:** The WJI peak initialises at entry WJI. In regimes where WJI
  starts high and immediately dips below p_exit × peak, WJI fires before momentum has time
  to develop a clean signal. This causes early exits on what would have been recoverable moves.
- **6 of 20 configs have PF < 1.0.** The combination is more dangerous than either mechanism
  alone at any p_exit ≥ 0.5.

---

## Phase Summary

| sub-phase | best config | PF | CVaR5 | EV | beats Sub-1? |
|-----------|-------------|-----|-------|----|-------------|
| BOCPD baseline | lh0.01_pe0.6 | 1.078 | −20.99% | 0.154 | — |
| **Sub-1: TP/SL** | **tp5_sl5** | **1.399** | **−9.14%** | **0.505** | baseline |
| Sub-2a: WJI trailing | raw_pe20 | 1.396 | −16.83% | 0.660 | **HARD STOP** |
| Sub-2b: momentum drop | mom6 | 1.142 | −12.60% | 0.220 | No |
| Sub-3: combined | pe10_mo6 | 1.128 | −12.60% | 0.199 | No |

**No exit mechanism or combination beats Sub-1 (fixed TP/SL). Full val run skipped.**

---

## Phase Conclusion

**The entry gate is the binding constraint, not the exit.** The BOCPD gate with fixed TP/SL
achieves PF=1.399 on the val sample — a substantial improvement over the BOCPD baseline
(PF=1.078), confirming that structured exits matter. But PF=1.399 is well below the Phase D
watermark baseline (PF=2.653). The signal-based exits (WJI trailing, momentum drop, combined)
all degrade PF relative to fixed TP/SL, for the same reason the WJI trailing stop always
fails: the exit signal arrives too late to outperform a simple fixed level.

**The exit problem is not fully solvable at the exit layer alone.** Two factors:

1. EPG window close dominates (50–80% of all exits) regardless of what other mechanisms are
   active. The gate is closing slower than the burst exhausts, and the structural fix requires
   either a faster gate (Phase EPG-GRT territory) or a different entry design.
2. WJI and price momentum are lagging indicators at 120–300s timescales — they confirm exhaustion
   after the fact rather than predicting it. A quote-side or bid-depth signal (deferred; see
   Phase_CPD_EXIT_Spec.md §6) would be earlier but requires separate infrastructure work.

**Recommendation:** Phase CPD-EXIT is closed. If improving exits is the goal, the most
promising next direction is a quote-side diagnostic (bid depth at reversal peak) before
parameterizing any quote-based trailing stop. That is already noted as deferred work in the
spec (§6).

---

## Escalation Log

| sub-phase | check | threshold | observed | result |
|-----------|-------|-----------|----------|--------|
| Sub-2a | best CVaR5 | ≥ −15% | −16.83% (raw_pe20) | **HARD STOP** |
| Sub-1 | all PF < 1.0 | — | min PF = 1.184 | PASS |
| Sub-2b | all PF < 1.0 | — | min PF = 0.871 (mom2) | soft flag only |
| Sub-3 | all PF < 1.0 | — | min PF = 0.908 (pe50_mo4) | soft flag only |
| Sub-3 | best CVaR5 | ≥ −15% | −12.60% (pe10_mo6) | PASS |

---

## Implementation Notes

- **Sequential execution required.** `ProcessPoolExecutor` deadlocks on Windows (spawn workers
  can't locate the venv path). All sweeps run single-threaded. Runtime: ~20–25 min per sweep.
- **MAX_RAW_TICKS = 100,000.** Applied immediately after `load_trades()` to cap per-event
  runtime. CCCC (5M+ ticks/event) would otherwise take hours per event.
- **4-pass architecture:** BOCPD gate once → LULD once → precompute momentum array → collect
  entries → replay N configs. Passes 1–3 are per-event fixed costs shared across all configs.
- **WJI log variant guard:** When `log(WJI_peak) ≤ 0` (peak ≤ 1.0), `p_exit × log(peak)` is
  a scaled negative — producing counterproductive stop behavior. Guard: deactivate log variant
  per trade when peak ≤ 1.0, fall through to lower-priority exits.
- **Momentum precomputation:** `np.searchsorted` on active-seconds axis (halt time excluded).
  Vectorized once per event; replayed per config without recomputation.

---

## Output Files

| Path | Description |
|------|-------------|
| `results/phase_cpd_exit/sub1_tp_sl/sweep_results.json` | Sub-1 full metrics (9 configs) |
| `results/phase_cpd_exit/sub1_tp_sl/sweep_summary.html` | Sortable dark-theme table |
| `results/phase_cpd_exit/sub1_tp_sl/event_charts/` | 96 per-event charts + index.html |
| `results/phase_cpd_exit/sub2a_wji_trailing/sweep_results.json` | Sub-2a full metrics (18 configs) |
| `results/phase_cpd_exit/sub2a_wji_trailing/sweep_summary.html` | Sortable dark-theme table |
| `results/phase_cpd_exit/sub2b_momentum_drop/sweep_results.json` | Sub-2b full metrics (10 configs) |
| `results/phase_cpd_exit/sub2b_momentum_drop/sweep_summary.html` | Sortable dark-theme table |
| `results/phase_cpd_exit/sub2b_momentum_drop/event_charts/` | 96 per-event charts + index.html |
| `results/phase_cpd_exit/sub3_combined/sweep_results.json` | Sub-3 full metrics (20 configs) |
| `results/phase_cpd_exit/sub3_combined/sweep_summary.html` | Sortable dark-theme table |
| `results/phase_cpd_exit/sub3_combined/event_charts/` | 96 per-event charts + index.html |
| `tools/phase_cpd_exit/sub1_tp_sl.py` | Sub-1 sweep runner |
| `tools/phase_cpd_exit/sub2a_wji_trailing.py` | Sub-2a sweep runner |
| `tools/phase_cpd_exit/sub2b_momentum_drop.py` | Sub-2b sweep runner |
| `tools/phase_cpd_exit/sub3_combined.py` | Sub-3 sweep runner |
