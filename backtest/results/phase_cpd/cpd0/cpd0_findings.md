# Phase CPD-0 — Findings & Handoff to CPD-1

**Date:** 2026-06-09
**Sample:** 100 val events, seed=42 (stratified). Full val reserved; test locked.
**Baseline to beat (CPD-1+):** WJI-OPT `p065_single` — PF=1.1881, CVaR5=−9.16%, EV/trade=+0.157%, n=2,134.
**Status:** CPD-0 complete, all escalation gates PASSED. **Awaiting Cooper approval (Gate 1) before CUSUM implementation (T5).**

---

## 0. Foundational decisions resolved with Cooper before T1

These two plan clauses conflicted with the actual code and were resolved before any extraction:

1. **`WJI_background(t) ≡ 1.0` (Cooper Option A).** The implemented WJI (`WJIGate` / `compute_wji_signal`) is already normalised against *static* references (λ_V_ref = pre-event mean λ_V; μ_buy = cold-start Hawkes), so it rests at ≈ 1.0 by construction. There is no separable dynamic background, and the per-tick refit μ is not persisted. The log-ratio therefore reduces to **`WJI_log(t) = log(WJI(t))`**. The WJI signal is unchanged/locked; the plan's "dynamic background mandatory" clause is void for this path.

2. **Timestamp axis = halt-adjusted active seconds (Cooper "wire halts now").** Halt-adjustment was *not* in the WJI path (it ran on raw `t_sec`). It was wired via `prepare_active_trades(include_extended=True)` on an ET-naive index (keeps 04:00–20:00 pre/post-market). The WJI EMAs now decay on active-seconds dt. **Consequence (accepted):** CPD-0 traces are not bit-identical to the raw-seconds WJI-OPT baseline.

---

## 1. T1 — Trace extraction

- **100/100 events** extracted, 0 skipped, 0 errored (81s, 8 workers).
- **Halts:** 6 events halted, 2,101s total compressed out of the active axis (all detected halts are full 5-min LULD halts; detection is conservative).
- Output: `wji_traces.pkl` (per-event WJI, WJI_background≡1, active-seconds axis), `t1_summary.json`.

---

## 2. T2 — Log-ratio transform & symmetry verification  ✅ PASS

**Zero-background / undefined-log guard:** 0 bad ticks across all 100 events (0.0%) — background≡1.0 is always >0 and WJI is ε-floored. No hard stop. (`zero_background_report.json`)

**H0 / rest-state window.** Per proposal §8 and plan T2, the H0 sample is the **warmup window** = first 300s of *active* time after T_event. ⚠️ **Finding:** for these momentum events T_event is the ignition onset, so this window is the early-surge state, not true rest — pooled log(WJI) is centred at **+1.25**, not 0. It is nonetheless the window the *live* gate uses for σ_log, and it is the most symmetric of the candidates:

| Window | n | mean | std | **skew** | excess-kurt |
|---|---|---|---|---|---|
| **A: warmup [0,+300s)** (used) | 222,215 | +1.251 | 0.870 | **+0.227** | −0.49 |
| B: all pre-event (true rest) | 1,237,425 | −0.566 | 1.163 | −1.374 | **+13.65** |
| B2: pre-event last 300s | 103,476 | +0.084 | 0.872 | −0.922 | +10.31 |

- **Escalation (skew within ±1.5): PASS** (Window A skew +0.227). Proceeded to T3.
- **Structural finding (for CPD-1 watch-list):** true-rest (pre-event) log(WJI) is heavily left-tailed (excess-kurt 10–14). Cause: during quiet pre-event periods the buy-side kernel decays toward 0, flooring WJI at ε → extreme negative log values. This is the WJI structural quirk the proposal warned about. It does not block (σ_log comes from the warmup window), but revisit if CUSUM mis-triggers. (`log_ratio_distributions.html`)

**σ_log distribution (Window A, per-event std of log(WJI)):**

| mean | median | p10 | p90 |
|---|---|---|---|
| 0.245 | 0.209 | 0.112 | 0.399 |

- **Recommended σ_log fallback constant = 0.209** (median). All 100 events have ≥20 warmup ticks, so the fallback is never triggered on this sample — but it is the principled default for warmup-deficient live events. (`sigma_log_summary.json`)

---

## 3. T3 — PELT offline segmentation

- ruptures v1.1.10, `model="rbf"`, `pen = 2·ln(n_bins)`. Sanity check passed (synthetic 3-segment signal recovered exactly).
- PELT run on the **post-T_event** WJI_log, resampled to a uniform active-time grid (~3000 bins/event, adaptive bin width ≥2s, mean log per bin). Post-event scoping avoids the pre-event dead-tick flooring. REST/REGIME label threshold: mean log(WJI) = 0.5 (analysis label only). 100/100 segmented. (`pelt_segments.json`, `pelt_diagnostic_sample.html`)

**Segment statistics (`calibration_summary.json`):**

| Metric | Value |
|---|---|
| Segments per event | mean 14.3, p10/50/90 = 10/14/19 |
| REST segment duration (s) | median 2,490, p10/90 = 695/6,598 (n=855) |
| REGIME segment duration (s) | median 2,340, p10/90 = 940/5,926 (n=576) |
| REGIME duration (trade ticks) | p25/75 = 4,979 / 38,984 |
| REGIME log elevation | mean 1.87, p25/75 = 1.13/2.56 |
| REST→REGIME transition elevation (log) | p25/50/75 = 1.00/1.48/1.99 (n=137) |
| REGIME→REST transition elevation (log) | p25/50/75 = 0.77/1.14/1.44 (n=190) |

---

## 4. Recommended k / h ranges for CPD-1 (T6 grid)

| Param | Recommendation | Notes |
|---|---|---|
| **k (raw-log units)** | [0.5, 3.0] | plan T3c formula: [p25·0.5, p75·1.5] of REST→REGIME log elevation |
| **k (standardised units)** | [2.4, 14.4] | raw-log range ÷ σ_log_median (0.209) — the apples-to-apples comparison to the proposal default k∈{0.5,1.0,1.5,2.0} |
| **h** | [2, 12] | plan T3c: [2, ceil(p75 regime ticks/10)] capped at 12 (saturates — regimes are tick-dense) |

⚠️ **k-units / saturation finding (most important handoff item).** In the CUSUM accumulator, `deviation = log(WJI)/σ_log` and `k` is in the same standardised units. Because σ_log (0.209) is the *within-surge* variance (small), an established regime at log(WJI)≈1.5 sits **~7σ above zero**, i.e. `deviation ≈ 7 per tick`. Consequences:
- The proposal-default grid k∈{0.5,1.0,1.5,2.0} all sit far below per-tick regime deviation → S_up rockets past any h∈[2,12] within ~1–2 ticks → **CUSUM PASS fires almost immediately on elevation** (behaves like a fast level detector with accumulation-hysteresis on exit).
- The CPD-0 standardised k recommendation (up to ~14) suggests the **T6 sweep should explore k well above the proposal defaults** to require genuinely stronger per-tick evidence, otherwise k/h will be nearly inert.
- **Recommendation for T6a:** sweep k across both regimes — low (proposal defaults {0.5,1.0,1.5,2.0}, fast/saturating) and high ({3,5,8,12}, evidence-demanding) — and h∈{2,4,8,12}, then let the Borda selection decide. This is a deviation from the proposal's fixed grid, justified by the σ_log scale.

---

## 5. Anomalies / watch-list

1. **H0 window is early-surge, not rest** (§2). σ_log measures surge-within-window variance, not rest noise → drives the k saturation above.
2. **Pre-event WJI heavy left tail** (excess-kurt 10–14) from buy-side dead-tick ε-flooring (§2). Structural WJI property; revisit if CUSUM mis-triggers on slow builds.
3. **k standardised range is large** (§4) — proposal-default k may be inert; widen the T6 grid.
4. Zero halts-with-in-window-trades — all 6 halts are clean 5-min gaps; active-seconds compression verified exact.

---

## 6. Output files (CPD-0)

| File | Status |
|---|---|
| `wji_traces.pkl` | ✅ |
| `zero_background_report.json` | ✅ (0 bad ticks) |
| `log_ratio_distributions.html` | ✅ |
| `sigma_log_summary.json` | ✅ |
| `pelt_segments.json` | ✅ |
| `calibration_summary.json` | ✅ |
| `pelt_diagnostic_sample.html` | ✅ |
| `cpd0_findings.md` | ✅ (this file) |

---

## 7. Decision request (Gate 1)

CPD-0 passed all gates. Before implementing the CUSUM gate (T5), I need Cooper's call on:

- **A.** Accept the σ_log = warmup-window definition as-is (live-consistent, passes symmetry) and proceed. *(my recommendation — it's what the live gate can use, and the saturation is just a k-range question handled in T6)*
- **B.** The T6 k-grid: approve widening beyond the proposal defaults to {0.5,1.0,1.5,2.0,3,5,8,12} (×16+ configs with h) given the standardised-k finding, vs. holding the proposal's fixed k∈{0.5,1.0,1.5,2.0}.
- **C.** Whether the pre-event heavy-tail / surge-vs-rest σ_log finding warrants any WJI-formula diagnostic now, or is deferred to a later pass (proposal §3 left this open).
