# Phase CPD-1 — CUSUM Gate: Implementation & Sweep (HARD STOP)

**Date:** 2026-06-09
**Status:** Implementation complete; 28-config val sweep **HARD-STOPPED at T6c** (no config passes the CVaR5 ≥ −10% hard filter). Awaiting Cooper instruction.
**Baseline:** WJI-OPT `p065_single` — PF=1.1881, CVaR5=−9.16%, EV=+0.157%, n=2,134.

---

## T5 — CUSUM gate implementation

Additive `gate_mode="cusum"` branch in [core/epg/gate.py](../../../core/epg/gate.py) (`peak`/`background` modes untouched; prior phases remain reproducible). Per-tick recursion:

```
wji_log(t)  = log( WJI(t) / WJI_background(t) )      # WJI_background ≡ 1.0 (Gate-1 Option A)
deviation   = wji_log(t) / sigma_log                  # H0 mean = 0 by construction
S_up(t)     = max( 0,  S_up(t-1) + deviation - k )    # one-sided accumulator, no dt term
```

- **State:** PASS `S_up > h`; FAIL `S_up ≤ 0`; hold in between (natural hysteresis).
- **sigma_log** finalised once at warmup exit from the `[T_event, T_event+300s active)` window; fallback **0.209** (CPD-0) if < 20 obs. Pre-event ticks are excluded from the warmup sample.
- **Halt-adjustment:** `S_up` holds across halts (no dt term; halted ticks skipped — explicit `is_halted` guard for raw feeds).
- **Inputs:** consumes the pre-computed (locked) WJI via `update(wji=, wji_background=1.0, is_halted=)`. Diagnostics: `s_up`, `sigma_log`, `last_cusum_debug`.
- **Tests:** [tests/test_cusum_gate.py](../../../tests/test_cusum_gate.py) — 13/13 pass (11 required cases + sigma-from-warmup + pre-event-exclusion regression).
  - One **pre-existing, unrelated** suite failure (`test_runner_sf.py::test_insufficient_bars_returns_empty`) — red on main, independent of CPD (Cooper OK'd proceeding).

---

## T6 — 28-config val sweep  →  HARD STOP

Grid (Cooper-approved, widened beyond proposal defaults given CPD-0's σ_log-saturation finding): **k ∈ {0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0} × h ∈ {2, 4, 8, 12} = 28 configs.** Harness [tools/phase_cpd/cpd1_t6_sweep.py](../../../tools/phase_cpd/cpd1_t6_sweep.py) reuses the CPD-0 active-axis WJI extraction and the WJI-OPT scorer (identical metric definitions). Gate-close-only exit + SF entry gate, matching the baseline. Full results: `cusum_sweep_results.json`.

**Hard filters:** CVaR5 ≥ −10%, n_trades ≥ 60, PF ≥ 1.0.
**Triggering condition (T6c):** all 28 configs fail **CVaR5 ≥ −10%**. Best CVaR5 = **−30.55%** (k12_h8); worst = −64.75% (k0.5_h8). n_trades and PF floors are satisfied everywhere — tail risk is the sole binding constraint.

| config | k | h | PF | n | CVaR5 | EV | capture |
|---|---|---|---|---|---|---|---|
| k0.5_h12 | 0.5 | 12 | 1.643 | 152 | −64.62 | 5.040 | 0.0939 |
| k0.5_h8 | 0.5 | 8 | 1.594 | 154 | −64.75 | 4.608 | 0.0871 |
| k0.5_h2 | 0.5 | 2 | 1.590 | 163 | −61.74 | 4.380 | 0.0801 |
| k0.5_h4 | 0.5 | 4 | 1.575 | 157 | −64.46 | 4.429 | 0.0835 |
| k1_h2 | 1 | 2 | 1.538 | 171 | −61.53 | 3.691 | 0.0698 |
| k1_h4 | 1 | 4 | 1.512 | 170 | −61.73 | 3.576 | 0.0681 |
| k1_h12 | 1 | 12 | 1.508 | 163 | −61.92 | 3.673 | 0.0691 |
| k1_h8 | 1 | 8 | 1.504 | 167 | −61.74 | 3.586 | 0.0684 |
| k2_h2 | 2 | 2 | 1.393 | 202 | −56.83 | 2.555 | 0.0509 |
| k5_h8 | 5 | 8 | 1.388 | 196 | −54.33 | 2.550 | 0.0569 |
| k2_h12 | 2 | 12 | 1.370 | 185 | −59.90 | 2.595 | 0.0529 |
| k2_h4 | 2 | 4 | 1.364 | 194 | −59.90 | 2.475 | 0.0501 |
| k2_h8 | 2 | 8 | 1.361 | 189 | −59.95 | 2.516 | 0.0510 |
| k5_h12 | 5 | 12 | 1.358 | 190 | −54.15 | 2.450 | 0.0546 |
| k3_h4 | 3 | 4 | 1.318 | 198 | −59.18 | 2.087 | 0.0437 |
| k3_h12 | 3 | 12 | 1.315 | 184 | −59.28 | 2.220 | 0.0476 |
| k3_h2 | 3 | 2 | 1.309 | 207 | −56.57 | 1.969 | 0.0417 |
| k5_h4 | 5 | 4 | 1.307 | 196 | −53.94 | 2.048 | 0.0460 |
| k3_h8 | 3 | 8 | 1.303 | 188 | −59.32 | 2.128 | 0.0451 |
| k5_h2 | 5 | 2 | 1.293 | 203 | −51.19 | 1.862 | 0.0418 |
| k8_h2 | 8 | 2 | 1.200 | 208 | −36.13 | 1.158 | 0.0297 |
| k8_h12 | 8 | 12 | 1.179 | 199 | −36.87 | 1.095 | 0.0280 |
| k8_h8 | 8 | 8 | 1.174 | 200 | −37.09 | 1.062 | 0.0272 |
| k8_h4 | 8 | 4 | 1.172 | 202 | −35.95 | 1.048 | 0.0269 |
| k12_h2 | 12 | 2 | 1.171 | 151 | −30.70 | 0.865 | 0.0220 |
| k12_h12 | 12 | 12 | 1.145 | 140 | −30.70 | 0.798 | 0.0200 |
| k12_h4 | 12 | 4 | 1.139 | 147 | −30.83 | 0.741 | 0.0190 |
| **k12_h8** | 12 | 8 | 1.118 | 143 | **−30.55** | 0.647 | 0.0165 |

### Factual observations
- **CVaR5 improves monotonically with k** (k0.5 ≈ −62 to −65% → k12 ≈ −30.5 to −30.8%); h has minor effect. Even the most evidence-demanding corner is 3× beyond the −10% floor.
- **PF, EV, capture are positive across all 28 configs** (PF 1.12–1.64). The gate is profitable on average — rejection is purely tail risk.
- **Trade counts ~140–208 total (~1.5–2/event)** vs the baseline's 2,134 (~21/event) — a ~10× reduction.
- The best CUSUM tail (−30.55%) is ~3× worse than the WJI-OPT baseline (−9.16%).

### Structural read (from CPD-0 + the sweep)
σ_log (0.209) is the *within-surge* variance, so an established regime at log(WJI)≈1.5 sits ~7σ above zero → low-k configs saturate (S_up crosses any h within 1–2 ticks). The gate-close exit (S_up draining to 0) is slow relative to a price reversal: from a high S_up it takes many ticks of background-level WJI to drain below 0, so positions are **held through regime collapses**, producing the deep tails. This is consistent across the grid and is the binding failure mode. The k12_h8 behaviour is inspected per-event in **Phase CPD-DIAG**.

---

## Artifacts (gitignored — local only)
`cusum_sweep_results.json`, `cusum_winner.json` (status: hard_stop). Regenerate via `python -m tools.phase_cpd.cpd1_t6_sweep`.
