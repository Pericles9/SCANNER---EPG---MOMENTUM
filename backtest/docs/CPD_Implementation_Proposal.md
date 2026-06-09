# Change Point Detection — Implementation Proposal
# EPG WJI Gate Redesign

**Project:** Scanner × EPG × Momentum  
**Date:** 2026-06-08  
**Status:** Proposal — awaiting Cooper approval before implementation  
**Supersedes:** Phase WJI-v2 decaying-peak EMA architecture (EPG-v2-Background-Normalized-Gate.md)

---

## 1. The Core Insight

Every threshold mechanism tried so far — static peak, decaying peak, slow EMA — has been a heuristic approximation of a statistical question: **has the distribution of WJI shifted from its rest state?**

Change Point Detection (CPD) answers that question directly. It is the formal statistical framework for detecting when a time series transitions between stationary distributions. The WJI signal, observed over an event window, is exactly this: a sequence of values drawn from a "rest" distribution that periodically shifts into a "regime" distribution and then returns.

The key advantage over every prior approach: **CPD never chases the signal.** The reference distribution is the Hawkes background — a generative model of the rest state that exists independently of WJI's observed values. The detector accumulates evidence that observations are inconsistent with that reference. Evidence resets when the regime ends. The threshold never inflates during a burst.

---

## 2. What We Have That Most CPD Applications Don't

Most CPD applications must estimate the reference distribution from the data. We already have it.

The Hawkes refit provides `mu_buy(t)` and `mu_sell(t)` — the background arrival rates — updated every 50 trades. From these, `WJI_background(t)` is computable at any tick:

```
WJI_background(t) = sqrt( g(1) × buy_term_background(t) )
```

This is the expected WJI value under pure background conditions. It is our **H0 distribution mean** — handed to us by the model itself, not estimated from observations.

This makes the detection problem unusually clean: we are not trying to learn what "normal" looks like from the data stream. We already know it analytically. The detector's only job is to accumulate evidence that the current observations are inconsistent with that known normal.

---

## 3. Signal Transformation Before Detection

WJI is strictly positive and right-skewed — the rest-state distribution has a long right tail driven by occasional large bursts. Feeding this raw into CUSUM creates two problems: the Gaussian assumption is violated, and `sigma_WJI` estimated from the warmup window will be dominated by any early-event spike, making the standardization unreliable for the rest of the event.

**The fix is a log-ratio transform applied before the CUSUM statistic is computed:**

```
WJI_log(t) = log( WJI(t) / WJI_background(t) )
```

**What this buys:**

- **Centers the rest state at zero automatically.** At background, `WJI(t) ≈ WJI_background(t)`, so `WJI_log ≈ 0`. No subtraction needed — H0 mean is zero by construction.
- **Compresses the right tail.** Log of a ratio on a multiplicative process is approximately Gaussian. The distribution CUSUM sees is far closer to the Gaussian assumption it requires.
- **Makes `sigma_WJI` consistent across events.** The log-ratio variance at rest is driven by the stochastic variation in the Hawkes intensity relative to background — not by the raw scale of the stock's activity. A single `sigma` estimated from warmup is now meaningful across thin names and thick names alike.
- **Scale-invariance.** A stock where WJI rests at 2.0 and spikes to 20.0 looks identical to one where it rests at 0.5 and spikes to 5.0 — both produce a log-ratio of ~2.3 at the spike. The detector's `k` and `h` parameters generalize across the universe without per-name tuning.

**Critical note on `WJI_background(t)`:** This must use the **dynamic** background from the online Hawkes refit (`mu_buy(t) + mu_sell(t)`, updated every 50 trades) — not the cold-start frozen value. The concern about background normalization being unreliable was specifically about static references going stale on long events. The dynamic refit-based background is the correct reference and is already part of the v2 spec.

**Updated CUSUM signal path:**

```
WJI_log(t)    = log( WJI(t) / WJI_background(t) )   ← transform applied first
sigma_log     = std( WJI_log ) over warmup window [T_event, T_event+300s)

deviation(t)  = WJI_log(t) / sigma_log               ← H0 mean is 0, no subtraction needed

S_up(t)       = max( 0,  S_up(t-1) + deviation(t) - k )
```

**Phase CPD-0 must verify** that `WJI_log` during rest periods is approximately symmetric. If it is, proceed. If it is still skewed, the WJI formula itself has a structural issue worth diagnosing before any sweep.

---

## 4. The Three Candidate Mechanisms

### 3.1 CUSUM (Cumulative Sum Control Chart)

**What it is:** At each tick, compute a standardized deviation of WJI from the background reference. Accumulate positive deviations in a running sum `S(t)`. Declare a regime (PASS) when `S(t) > h`. Reset `S(t)` toward zero when the deviation falls below a slack parameter `k`.

**Formal definition:**

```
deviation(t) = ( WJI(t) - WJI_background(t) ) / sigma_WJI

S(t) = max( 0,  S(t-1) + deviation(t) - k )

PASS  when  S(t) > h
FAIL  when  S(t) == 0  (or < reset_threshold for hysteresis)
```

`sigma_WJI` is the expected standard deviation of WJI at rest. Can be estimated empirically from the warmup window (the 300s post-T_event before the gate activates), or set as a fixed fraction of `WJI_background`.

**Parameters:** `k` (slack — controls sensitivity to small deviations, typical range 0.5–2.0), `h` (detection threshold — controls how much accumulated evidence triggers PASS, typical range 2–8).

**Breakout strength:** The rate of rise of `S(t)` at the PASS transition is a direct measure of how fast evidence is accumulating. A slope filter `dS/dt > R` at the crossing is a clean, interpretable strength requirement.

**Why this fits:** CUSUM was designed for exactly this use case — detecting a shift in mean from a known reference. It is causal, tick-by-tick, computationally trivial (two arithmetic operations per tick), and has well-understood calibration behavior. The slack parameter `k` directly maps to "how elevated above background before I care."

**Reset behavior:** `S` resets to zero naturally when WJI returns to background. This is the exit condition — not a threshold the signal has to drop below, but a natural draining of accumulated evidence. This is the "WJI returning to range" exit you described.

---

### 3.2 Bayesian Online Changepoint Detection (BOCD)

**What it is:** Maintains a probability distribution over "run length" — how many ticks has the current stationary segment been running? At each tick, updates the run-length posterior using Bayes' rule. A spike in probability mass at run length = 0 signals a changepoint.

**What it gives you that CUSUM doesn't:** A posterior probability `P(regime at t)` rather than a binary threshold crossing. You can set the PASS threshold at any confidence level, e.g., PASS when `P(in-regime) > 0.80`.

**Parameters:** Hazard rate `λ` (prior probability of a changepoint per tick), and the parameters of the predictive distribution (Gaussian with known or estimated variance).

**Why it's attractive:** The run-length posterior naturally answers "how long has this regime been running?" — which could be useful for position management, not just entry/exit. It also degrades gracefully: if the regime is weak, the posterior is spread out rather than giving a false binary signal.

**Why it's heavier:** BOCD maintains a vector of probabilities over all possible run lengths, growing each tick until a reset. Computational cost is O(t) per tick in the naive implementation, though truncation at a maximum run-length cap (e.g., 600s) keeps it bounded. Not trivial to implement correctly in a single-threaded asyncio loop without careful pre-allocation.

**Recommendation:** Better suited as a diagnostic tool on historical data than as a live gate in the first implementation. If CUSUM proves insufficient, BOCD is the natural upgrade path.

---

### 3.3 Offline Segmentation via PELT (for calibration only)

**What it is:** Pruned Exact Linear Time algorithm. Finds the globally optimal set of changepoints in a complete time series. Not causal — requires the full trace. Available in the `ruptures` Python library.

**Role here:** Not a live gate. Used offline on historical WJI traces to find ground-truth regime boundaries. These ground-truth labels then calibrate the CUSUM parameters `k` and `h` — you run PELT on the 100-event val sample, see where the true changepoints were, then tune the online detector to match.

**Why this matters:** Without a principled calibration procedure, `k` and `h` are just two more free parameters swept blindly. PELT gives you a reference answer to compare against.

---

## 5. Recommended Architecture: CUSUM Gate

### 5.1 Signal path

Full pipeline from raw inputs to gate state:

```
WJI(t)             — existing composite signal, unchanged
WJI_background(t)  — dynamic, from Hawkes refit every 50 trades

WJI_log(t)         = log( WJI(t) / WJI_background(t) )   ← see §3
sigma_log          — std of WJI_log over warmup window [T_event, T_event+300s)

deviation(t)       = WJI_log(t) / sigma_log

S_up(t)   = max( 0,  S_up(t-1)   + deviation(t) - k )   ← regime entry accumulator
S_down(t) = max( 0,  S_down(t-1) - deviation(t) - k )   ← regime exit accumulator (optional)
```

One-sided CUSUM (`S_up` only) detects upward shifts — WJI elevated above background. This is the entry signal.

A two-sided version adds `S_down` to detect when WJI falls significantly *below* background, which could optionally gate against entering into a sell-dominated environment. Keep disabled until one-sided is validated.

### 5.2 State logic

```
WARMUP   — first 300s after T_event (unchanged; sigma_log estimated here)

PASS     — S_up(t) > h  AND  (optional) dS_up/dt > R at crossing
FAIL     — S_up(t) == 0  (evidence fully drained)
```

Re-entry works naturally: after a FAIL, `S_up` starts accumulating again from zero on the next burst. No explicit re-entry logic needed.

### 5.3 Parameters (3 total)

| Parameter | Meaning | Calibration method | Typical range |
|---|---|---|---|
| `k` | Slack — minimum excess deviation before evidence accumulates | PELT offline calibration, then sweep | 0.5 – 2.0 |
| `h` | Detection threshold — accumulated evidence required to trigger PASS | PELT offline calibration, then sweep | 2.0 – 8.0 |
| `R` | Optional breakout slope filter — minimum dS/dt at PASS crossing | Diagnostic charts, optional | disable first |

`sigma_log` is estimated, not a free parameter — computed from the 300s warmup window per event. If `n_warmup_observations < 20`, fall back to a fixed empirical constant derived from Phase CPD-0 analysis.

### 5.4 Why the transform and sigma work together

The log-ratio centers the rest-state at zero and compresses the right tail. `sigma_log` then standardizes the scale. Together they make `k` and `h` dimensionless constants that mean the same thing across every event in the universe — "how many standard deviations of rest-state noise, accumulated over how many ticks." Without both, the parameters would need per-name or per-regime tuning.

---

## 6. Relationship to Prior Work

| Aspect | WJI-OPT (current best) | CUSUM Gate (proposed) |
|---|---|---|
| Reference for "normal" | Event's own running peak | `WJI_background(t)` from Hawkes refit |
| Reference adapts during regime? | Yes — monotonically ratchets up | No — background is independent of WJI observations |
| Exit mechanism | WJI drops below `p × peak` | Accumulated evidence drains to zero |
| Re-entry mechanism | Implicit — new burst re-enters immediately | Natural — `S_up` re-accumulates from zero |
| Parameters | 1 (`p`) | 2 (`k`, `h`) + optional `R` |
| Statistical grounding | None — heuristic threshold | Optimal detection under Gaussian shift assumption |
| Sensitivity to opening burst | High — peak anchors to first spike | None — `S_up` resets on exit, opening burst is forgotten |

---

## 7. Implementation Sequence

### Phase CPD-0 — Offline calibration (no backtest)

**Objective:** Verify the log-ratio transform, establish ground-truth regime boundaries using PELT, and set the prior range for `k` and `h` before any sweep.

Tasks:
1. Compute WJI traces and `WJI_background` traces for all 100 val events
2. **Compute `WJI_log = log(WJI / WJI_background)` for each event. Plot rest-period distribution. Verify approximate symmetry. If still skewed, hard stop — escalate before proceeding.**
3. Estimate `sigma_log` from warmup windows. Record distribution across events — flag if variance is high (unstable warmup estimation).
4. Run PELT (`ruptures`, cost function `"rbf"`) on each `WJI_log` trace
5. For each event, record: changepoint timestamps, within-segment mean and variance, inter-segment gaps
6. Aggregate: distribution of segment lengths, typical elevation at changepoints, typical return-to-zero time
7. Use results to set `k` and `h` prior ranges for the CPD-1 sweep

**Output:** `results/phase_cpd_0/pelt_segments.json`, `results/phase_cpd_0/calibration_summary.json`, `results/phase_cpd_0/log_ratio_distributions.html`

**No approval gate needed** — analysis only, no backtest changes.

---

### Phase CPD-1 — CUSUM gate implementation and 100-event sweep

**Objective:** Implement the CUSUM gate as `gate_mode="cusum"` in `ParticipationGate`. Sweep `k × h` on the 100-event val sample. Validate against WJI-OPT baseline (PF=1.1881, CVaR5=−9.16%).

Tasks:
1. Implement `gate_mode="cusum"` branch — log-ratio transform, `sigma_log` from warmup, tick-by-tick accumulation, halt-adjusted dt
2. Unit tests: synthetic traces verifying log-ratio computation, accumulation, reset, and PASS/FAIL transitions
3. 100-event sweep over `k ∈ {0.5, 1.0, 1.5, 2.0}` × `h ∈ {2, 4, 6, 8}` (16 configs)
4. Select winner: highest Borda rank on (capture_fraction, EV/trade, CVaR5) — same ranking protocol as prior phases
5. Per-event charts for winner config (4-panel standard + `S_up` score panel replacing Panel 3)

**Escalation criteria:**
- No config passes CVaR5 ≥ −10%: hard stop, post full sweep table, await instruction
- Winner PF < 1.10: hard stop, post results, await instruction

**Cooper approval gate** before proceeding to full val.

---

### Phase CPD-2 — Full val confirmation

**Objective:** Run winner config on full val split. Compare to WJI-OPT and Phase F baseline.

Standard val confirmation protocol — no new decisions.

---

### Phase CPD-3 (Optional) — BOCD upgrade

Only if CUSUM passes full val but shows pathology on weak/noisy events (false positives on slow builds, missed sharp bursts). BOCD provides the run-length posterior as an additional gate input. Evaluate only if the simpler solution proves insufficient.

---

## 8. Live Implementation Notes

**`sigma_log` per event:** Estimated from the 300s WARMUP window on the log-ratio values. Store as a scalar on the gate object. If `n_warmup_observations < 20`, fall back to a fixed empirical constant from Phase CPD-0 analysis. Log the fallback.

**Hawkes refit interaction:** `WJI_background(t)` updates on every Hawkes refit (every 50 trades), which immediately flows through to `WJI_log(t)`. `sigma_log` does not update after warmup — it is a fixed per-event constant. This keeps the standardization stable and prevents `sigma` from shrinking during a regime (which would inflate `deviation` and cause false persistence).

**Halt adjustment:** `dt` in the CUSUM accumulation uses halt-adjusted active seconds, same as all other EMA kernels. During a halt, `S_up` holds its value. On resume, the first post-halt tick uses active-seconds dt.

**Reset on re-entry:** `S_up` resets to zero on every event start. Between PASS/FAIL cycles within an event, `S_up` drains to zero naturally — it is not explicitly reset. A partial drain means the gate re-opens more quickly on the next burst, which is correct behavior for continuation regimes.

**Parameter count:** Two free parameters (`k`, `h`). `sigma_log` is estimated, not tuned. `R` is disabled by default.

---

## 9. Open Questions (to be resolved at Phase CPD-0)

1. **`sigma_log` fallback value** — if warmup is too short, what fixed constant to use? Phase CPD-0 will produce the empirical distribution of `sigma_log` across 100 events, giving a principled default.

2. **One-sided vs two-sided CUSUM** — preliminary hypothesis: one-sided is sufficient because `WJI_background` already encodes the buy/sell balance. Keep one-sided for CPD-1.

3. **`g(.)` and `buy_term` form** — the four open decisions from EPG-v2 still apply to the WJI formula itself. CUSUM consumes WJI as-is; these decisions affect what WJI looks like, not how the detector works. Resolve before CPD-1 or hold fixed at current defaults.

4. **PELT cost function** — `"rbf"` preferred over `"l2"` because WJI has both level and variance shifts during a regime. Confirm in CPD-0.

---

## 10. Summary

The WJI signal is correct. Every prior gate failure has been a threshold mechanism failure, not a signal failure. CUSUM applies the right statistical tool to the problem: it treats the Hawkes background as a known reference distribution and accumulates evidence of departure from it.

The two-step implementation (offline PELT calibration → online CUSUM sweep) avoids the blind parameter sweep problem that has characterized prior phases. It also gives the gate a principled interpretation: `k` is "how much above background before I care," and `h` is "how much sustained evidence of regime before I enter." Both are inspectable and debuggable against the per-event charts.

BOCD is available as an upgrade path if the binary CUSUM signal proves too coarse. But the simpler tool should be validated first.

---

*Approval gate: Do not begin Phase CPD-0 or any implementation work until Cooper has reviewed this proposal and given explicit approval.*
