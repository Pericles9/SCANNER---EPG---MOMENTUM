# Phase CPD — Change Point Detection Gate (Status Overview)

**Last updated:** 2026-06-09
**Goal:** Replace the heuristic WJI peak-threshold gate with a statistically grounded change-point detector (CUSUM / BOCD), calibrated offline with PELT, validated against the WJI-OPT baseline (PF=1.1881, CVaR5=−9.16%, n=2,134 on the 100-event val sample).
**Design reference:** [docs/CPD_Implementation_Proposal.md](../../docs/CPD_Implementation_Proposal.md)

---

## Current status: CUSUM HARD-STOPPED; diagnostic complete; awaiting Cooper decision

| Sub-phase | Status | Result |
|---|---|---|
| **CPD-0** (PELT calibration) | ✅ Complete, Gate-1 approved | Log-ratio symmetry verified (skew +0.227); σ_log median 0.209; k/h prior ranges derived. [cpd0/cpd0_findings.md](cpd0/cpd0_findings.md) |
| **CPD-1 T5** (CUSUM gate) | ✅ Complete | `gate_mode="cusum"` in `core/epg/gate.py`; 13/13 unit tests pass. |
| **CPD-1 T6** (28-config sweep) | ⛔ **HARD STOP** | No config passes CVaR5 ≥ −10% (best −30.55%). [cpd1/cpd1_findings.md](cpd1/cpd1_findings.md) |
| **CPD-2** (BOCD gate) | ⏸ Not started | Blocked behind the CPD-1 hard-stop decision. |
| **CPD-DIAG** (k12_h8 charts) | ✅ Complete | 15 diagnostic charts; tails driven by slow gate-close exit through regime collapse. [../phase_cpd_diag/cpd_diag_findings.md](../phase_cpd_diag/cpd_diag_findings.md) |

---

## Two foundational decisions (Cooper)

1. **`WJI_background ≡ 1.0`** — the implemented WJI is already static-normalised and rests at ≈1.0; there is no separable dynamic background. So `WJI_log = log(WJI)`. WJI signal stays locked.
2. **Timestamp axis = halt-adjusted active seconds** — wired via `prepare_active_trades(include_extended=True)` (keeps pre-market); WJI EMAs decay on active dt. Traces are therefore not bit-identical to the raw-axis WJI-OPT baseline.

## Headline technical finding
σ_log (0.209) is the *within-surge* variance (the warmup window is the ignition surge, not rest — mean log(WJI) ≈ +1.25). An established regime sits ~7σ above zero, so low-k configs saturate (S_up crosses h in 1–2 ticks). The gate-close exit drains slowly, **holding positions through regime collapses** → deep tails (best −30.55% vs baseline −9.16%). All 28 configs are profitable on average (PF 1.12–1.64) but fail the tail filter.

## Code & tests
- Gate: `core/epg/gate.py` (`gate_mode="cusum"`, additive — peak/background untouched).
- Tools: `tools/phase_cpd/` — `cpd0_t1_traces`, `cpd0_t2_logratio`, `cpd0_t3_pelt`, `cpd1_t6_sweep`, `diag_charts`.
- Tests: `tests/test_cusum_gate.py` (13/13).

## Note on artifacts
Generated `.json` / `.html` / `.pkl` outputs are **gitignored** (large; regenerable). All findings are captured in the committed `.md` files above; rerun the `tools/phase_cpd/*` scripts to regenerate the raw artifacts.
