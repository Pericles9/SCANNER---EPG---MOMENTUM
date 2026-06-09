<!-- fullWidth: false tocVisible: false tableWrap: true -->
---
tags:
  - type/research
  - domain/signal
  - project/scanner-epg-momentum
  - status/needs-review
created: 2026-06-08
last_reviewed: 2026-06-08
---

# WJI Architecture Timeline

Read-only reconstruction of the Weighted Joint Intensity (WJI) formula at every\
implementation iteration, from first commit through WJI-v2. Produced before any\
further gate-code changes so Cooper can confirm the formula lineage is correct.

---

## Overview

Three distinct WJI formulations exist across two commits. Iterations 1–2 live in\
`backtest/core/epg/gate_variants.py`; Iteration 3 lives in `backtest/core/epg/gate.py`.\
No WJI code exists in the parent `hawkes-ofi-impact` repo.

| Iteration | Phase   | Class                                     | Commit      | Date        |
| --------- | ------- | ----------------------------------------- | ----------- | ----------- |
| 1         | WJI-POC | `WJIGate`                                 | `ef24d7c`   | 2026-06-05  |
| 2         | WJI-OPT | `RunningMaxGate` + `compute_wji_signal()` | `536be74`   | 2026-06-07  |
| 3         | WJI-v2  | `ParticipationGate(gate_mode="background")` | in-progress | 2026-06-07+ |

---

## Iteration 1 — WJI-POC (`WJIGate`, commit `ef24d7c`)

**First introduced:** 2026-06-05 ("build and tested wji gate.")\
**Source:** `backtest/core/epg/gate_variants.py`, class `WJIGate`

### Signal formula

```
norm_λ_V(t)   = λ_V(t) / λ_V_ref
norm_λ_buy(t) = λ_buy_slow(t) / μ_buy

WJI(t) = norm_λ_V(t)^α × norm_λ_buy(t)^(1−α)
```

Default α = 0.5 → equal-weight geometric mean of the two normalised components.

### Kernels

**λ\_V (dollar-volume EMA):** Standard exponential kernel, half-life τ\_V = 180 s.

```
λ_V(t) = λ_V(t−) × exp(−(ln2/τ_V) × dt) + dv × (ln2/τ_V)
```

**λ\_buy_slow (standalone EMA — NOT a Hawkes kernel):** Accumulates β\_slow on each buy\
trade; decays continuously at rate β\_slow. Entirely independent of the Hawkes process.

```
λ_buy_slow(t) = λ_buy_slow(t−) × exp(−β_slow × dt)
if side == buy: λ_buy_slow += β_slow
```

Default β\_slow = 0.01 → half-life ≈ 69 s.

### Reference levels

- **λ\_V_ref:** Pre-event mean of λ\_V EMA computed over `[session_start, T_event)`.\
  Computed by `_compute_lambda_v_ref()` in `tools/phase_wji_poc/common.py`.\
  Static — fixed at event start, not refreshed during the window.
- **μ\_buy:** Background buy-arrival rate from a **cold-start** Hawkes MLE fit at T_event\
  (not online refit). Sourced from `wji_poc_worker()` in the same file.

### Peak update (slope-driven adaptive decay)

Peak initialised at **1.0**.

```
if in_pass or slope_WJI >= 0:
    peak = max(peak, WJI)          # ratchet up
else:  # FAIL and slope < 0
    peak *= exp(−(ln2/τ_decay) × dt)   # decay with half-life 120 s
```

τ\_decay = 120 s. Slope defined as `WJI(t) − WJI(t − L_sec)` where L_sec = 60 s.\
Decay activates **only** when the gate is in FAIL and WJI is decelerating.

### Hysteresis (asymmetric)

```
FAIL → PASS: WJI ≥ p_open  × peak     (p_open  = 0.65)
PASS → FAIL: WJI <  p_close × peak    (p_close = 0.30)
```

### Results (val, 100-event sample, seed=42)

| Metric                         | Value              |
| ------------------------------ | ------------------ |
| PF                             | 1.1720             |
| n_trades                       | 1,546              |
| Events with trades             | 97 / 100           |
| Component balance at gate open | 27–31% buy channel |
| SF blocking rate               | 54%                |

---

## Iteration 2 — WJI-OPT (`RunningMaxGate`, commit `536be74`)

**Introduced:** 2026-06-07 ("wji opt and wji decay (decay not complete)")\
**Source:** `backtest/core/epg/gate_variants.py`, class `RunningMaxGate`;\
signal computation in `tools/phase_wji_opt/common.py`, function `compute_wji_signal()`

The OPT phase extracted signal computation from the gate class, enabling one signal array\
to be scored against multiple gate configurations in a single pass.

### Signal formula

Unchanged from POC.

```
norm_v = λ_V(t) / λ_V_ref
norm_b = λ_buy_slow(t) / μ_buy

WJI(t) = max(norm_v, EPS)^α × max(norm_b, EPS)^(1−α)
```

Same α = 0.5. T_event and μ\_buy are **cached from the POC run** — OPT does not rerun\
Hawkes, it replays pre-computed signal arrays through different gate configurations.

### Gate class: `RunningMaxGate`

Signal-agnostic: the caller passes a single pre-normalised float. The gate applies a\
causal running-max peak with **no decay** and **no slope term**.

```
peak = max(peak, signal)   # every tick, monotonically non-decreasing
peak initialised at 1.0
```

Hysteresis modes:

- `single`: symmetric open and close at fraction p
- `asym`: open at p, close at p_close = 0.30

### Sweep

P grid: {0.55, 0.65, 0.70, 0.75, 0.80} × {single, asym}.

All asym configs failed CVaR5 (−17% to −18% vs floor −8%).\
**Winner: `p065_single`** — p = 0.65, symmetric (p_close = p_open = 0.65).

### Results (val, 100-event sample, seed=42)

| Metric          | `p065_single`            |
| --------------- | ------------------------ |
| PF              | 1.1881                   |
| n_trades        | 2,134                    |
| CVaR5           | −9.16%                   |
| EV/trade        | +0.157%                  |
| Max single loss | −36.9% (MGIH 2024-02-12) |

Key finding (stage1_findings.json): "WJI passes CVaR5 floor; λ\_V does not. WJI is the\
sole viable signal in a gate-close-only design."

---

## Iteration 3 — WJI-v2 (`ParticipationGate(gate_mode="background")`, in progress)

**Introduced:** 2026-06-07, phase WJI-v2 (EPG v2 Background-Floored Decaying-Peak Gate).\
**Source:** `backtest/core/epg/gate.py`, `ParticipationGate`, `gate_mode="background"` branch.\
**Spec:** `docs/EPG-v2-Background-Normalized-Gate.md`

### Signal formula (redesigned)

```
background_V = (μ_buy + μ_sell) × d̄
volume_ratio = λ_V(t) / background_V
I_buy        = λ_buy(t) / (λ_buy(t) + λ_sell(t))   [0.5 if total = 0]

WJI(t) = sqrt(volume_ratio × I_buy)
```

Background WJI (floor reference):

```
WJI_background = sqrt(μ_buy / (μ_buy + μ_sell))
```

### Key architectural differences from POC/OPT

| Dimension            | Iter 1–2 (POC/OPT)                  | Iter 3 (v2)                               |
| -------------------- | ----------------------------------- | ----------------------------------------- |
| Formula shape        | `norm_v^0.5 × norm_buy^0.5`         | `sqrt(vol_ratio × I_buy)`                 |
| Buy term             | standalone EMA kernel λ\_buy_slow   | live Hawkes intensity λ\_buy              |
| Buy normalisation    | divided by μ\_buy (level)           | fraction λ\_buy/(λ\_buy+λ\_sell)          |
| λ\_V ref             | pre-event mean λ\_V_ref (static)    | `(μ_buy + μ_sell) × d̄` (Hawkes background) |
| μ\_sell used         | no                                  | yes — background denominator              |
| μ source             | cold-start fit, not refreshed       | online refit (mandatory)                  |
| Background floor     | none                                | `C × WJI_background` applied to threshold |
| Peak decay condition | slope-driven: FAIL + slope < 0 only | unconditional: every tick                 |
| Peak half-life       | 120 s (conditional)                 | 600 s (unconditional)                     |

### Kernels

**λ\_V:** Same exponential EMA (half_life_seconds from gate constructor).

**λ\_buy, λ\_sell:** Live Hawkes intensities passed as keyword args to `update()`.\
Sourced from `_hawkes_replay_with_refit()`. Online refitting is non-negotiable.

**d̄:** Mean dollar-per-trade over the Hawkes refit window (Cooper-locked decision).

### Peak update (unconditional decay)

```
peak_decay = exp(−dt / tau_peak)
peak_WJI   = max(WJI(t), peak_WJI × peak_decay)
```

tau_peak = 600 s default. No slope condition — the peak decays on every tick whenever\
no new high is set.

### Threshold (background-floored)

```
threshold_open  = max(p_open  × peak_WJI,  C × WJI_background)
threshold_close = max(p_close × peak_WJI,  C × WJI_background)
```

C = 2.0 default. The floor prevents the gate from going dormant in background-rate\
conditions — the background WJI is always the minimum meaningful threshold.

### T3i baseline (gate_mode="peak", 100-event val, seed=42)

This is the peak-mode anchor for the WJI-v2 phase. Reproduces prior behavior bit-for-bit.

| Metric             | Value    |
| ------------------ | -------- |
| PF                 | 1.1458   |
| n_trades           | 385      |
| Win%               | 43.9%    |
| Events with trades | 84 / 100 |

WJI-v2 (background mode) T4+ sweep is in progress. T4 diagnostic charts generated\
at `results/phase_wji_v2/t4_diag/index.html` (98 events, 6 candidate C floors).

---

## Critical Questions

**Q1: What WJI formula is active in the production code right now?**\
`ParticipationGate(gate_mode="background")` — Iteration 3. `gate_mode` defaults to\
`"peak"` (peak-mode behavior from Iteration 2). Background mode is opt-in.

**Q2: What normalises the volume component in each iteration?**

- It1/It2: `λ_V_ref` = pre-event mean of λ\_V over `[session_start, T_event)`.\
  Static per-event scalar, computed once and cached.
- It3: `background_V = (μ_buy + μ_sell) × d̄`. Dynamic — updates with each online refit.\
  Explicitly Hawkes-background-anchored.

**Q3: How is μ\_buy sourced in each iteration?**

- It1/It2: Cold-start Hawkes MLE fit at T_event. One fit per event, not refreshed.
- It3: Online Hawkes refit (`_hawkes_replay_with_refit()`). Mandatory and continuously\
  refreshed throughout the event window.

**Q4: How is the WJI peak managed in each iteration?**

- It1: Ratchets up during PASS or (FAIL + slope ≥ 0); decays (τ=120 s) during FAIL + slope < 0.\
  Decay is conditional on direction and state.
- It2: Monotonically non-decreasing. Never decays under any condition.
- It3: Decays (τ=600 s) on every tick when no new high is set. Softer than It1 by 5×\
  in half-life, but unconditional — the peak always retreats in quiet periods.

**Q5: Where does the buy-side signal come from in each iteration?**

- It1/It2: `λ_buy_slow` — a standalone EMA kernel independent of Hawkes. Increments by\
  β\_slow on each buy trade; half-life ≈ 69 s. Normalised by μ\_buy as a level ratio.
- It3: `I_buy = λ_buy / (λ_buy + λ_sell)` — the live Hawkes buy fraction. Ranges in\
  \[0, 1\]. Background: `μ_buy / (μ_buy + μ_sell)`. Fraction-based, not level-based.

---

## Algebraic Form Comparison

```
Iter 1/2:   WJI = ( λ_V / λ_V_ref )^0.5  ×  ( λ_buy_slow / μ_buy )^0.5

Iter 3:     WJI = sqrt( λ_V / ((μ_buy+μ_sell)×d̄)  ×  λ_buy/(λ_buy+λ_sell) )
```

The It3 formula is a `sqrt(A × B)` — algebraically the same shape as the It1/It2\
geometric mean with α=0.5. The two components differ:

- **Volume component:** same EMA λ\_V; reference changed from static pre-event mean to\
  dynamic Hawkes-background × mean-dollar-per-trade.
- **Buy component:** changed from a standalone EMA normalised by a level (μ\_buy) to the\
  live Hawkes buy fraction (naturally in \[0, 1\], background value `μ_buy/(μ_buy+μ_sell)`).

---

*Approval gate: Do not begin gate code changes, phase rewrites, or diagnostic scripts\
until Cooper has reviewed this timeline and confirmed the formula lineage is correct.*