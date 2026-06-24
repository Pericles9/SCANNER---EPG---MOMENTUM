# Phase CPD-BOCPD — Directional BOCPD Gate: Implementation & Sweep (HARD STOP)

**Date:** 2026-06-09
**Status:** Implementation complete; 16-config val sweep **HARD-STOPPED at T4** (winner fails both
CVaR5 ≥ −10% and PF ≥ 1.10). Escalation priority (plan): CVaR5 > PF → binding criterion is CVaR5.
**Awaiting Cooper instruction. No remedies proposed — data only.**

---

## Spec resolutions (Cooper, pre-implementation)

Two contradictions in the phase doc were resolved with Cooper before coding:

1. **WJI_background.** Doc said both "retained from CPD-1" and "use dynamic Hawkes refit
   background." CPD-1 actually used `WJI_background ≡ 1.0` (Option A; WJI internally normalised
   by a frozen cold-start `mu_buy`). **Resolution: Option A retained** — `WJI_log = log(WJI)`,
   exact CPD-1 signal pipeline; only the gate accumulator changed.
2. **Algorithm degeneracy.** The literal BOCPD spec uses a run-length-INDEPENDENT predictive
   `pred[r] = N(x; 0, sigma_log)`. That cancels in normalisation, forcing `R[0] = lambda_h`
   and `P_regime ≡ 1 − lambda_h` (constant). **Resolution (Cooper Option 2): directional
   surge-aware BOCPD** — proper Adams-MacKay Normal-Normal UPM (known variance `sigma_log^2`,
   unknown run mean, prior mean 0), with `P_regime` restricted to run-length mass whose
   posterior mean is elevated, mirroring CPD-1's one-sided CUSUM.

Path note: doc references `results/phase_cpd_1/`; real artifacts are at `results/phase_cpd/cpd1/`
(present) and `results/phase_cpd/cpd0/sigma_log_summary.json`. Data present → T1a not triggered.

---

## T2 — gate implementation (`gate_mode="bocpd"`)

Additive branch `_update_bocpd` in [core/epg/gate.py](../../../core/epg/gate.py); peak/background/
cusum branches untouched. Per-tick (post-warmup) Adams-MacKay message passing on `WJI_log`:

```
pred[r]   = N(x_t; mu_post_r, sigma_log^2 + var_post_r)      # run-length-dependent UPM
R_new[0]  = lambda_h * sum_r R[r]*pred[r]                    # changepoint
R_new[r+1]= (1-lambda_h) * R[r]*pred[r]                      # growth (absorbs x_t)
R_new    /= sum(R_new)
P_regime  = sum_{r>0} R_new[r] * 1[ mu_post_r > dir_thresh ] # directional, surge-aware
```

- **Swept (2):** `lambda_h ∈ {0.005,0.01,0.02,0.05}`, `p_enter ∈ {0.60,0.70,0.80,0.90}` = 16.
- **Fixed model constants (NOT tuned):** `prior_mean_std=1.0`, `dir_thresh_mult=1.0`
  (`dir_thresh = 1.0·sigma_log`), `max_run_length=600` (fold mass at cap), hysteresis gap 0.10
  (`p_exit = p_enter − 0.10`). `sigma_log` from the 300s warmup window; fallback 0.209.
- **sigma_log prior** (T1, `sigma_log_prior.json`): median 0.2090, p10 0.1117, p90 0.3987,
  n_events_with_warmup_obs_lt_20 = 0.
- **Tests:** [tests/test_bocpd_gate.py](../../../tests/test_bocpd_gate.py) — 12/12 pass
  (flat/step-up/step-down/truncation/halt + validation + cusum regression). The pre-existing,
  unrelated `test_runner_sf.py::test_insufficient_bars_returns_empty` remains red on main
  (independent of CPD; noted in CPD-1).

---

## T3/T4 — 16-config val sweep → HARD STOP

Same event list (`quality_sample_val.json`, 100 events), active-axis WJI, SF entry gate,
gate-close exit, and WJI-OPT scorer as CPD-1. Harness
[tools/phase_cpd_bocpd/t3_sweep.py](../../../tools/phase_cpd_bocpd/t3_sweep.py). 100/100 events ok,
0 errored. Full results: `bocpd_sweep_results.json`; winner: `bocpd_winner.json`.

**Hard filters:** CVaR5 ≥ −10%, n_trades ≥ 60, PF ≥ 1.0. **All 16 eligible** (n_trades ≥ 60 AND
PF ≥ 1.0); **0 pass the full CVaR5 ≥ −10% filter.**

| cfg | lambda_h | p_enter | PF | n | CVaR5 | EV | capture | pass_frac | Borda rank |
|---|---|---|---|---|---|---|---|---|---|
| lh0.05_pe0.9 | 0.05 | 0.90 | 1.040 | 1260 | −20.12 | 0.075 | 0.00213 | 0.892 | 11 |
| lh0.05_pe0.6 | 0.05 | 0.60 | 1.043 | 1221 | −20.60 | 0.082 | 0.00229 | 0.894 | 9 |
| lh0.05_pe0.8 | 0.05 | 0.80 | 1.032 | 1238 | −20.64 | 0.061 | 0.00175 | 0.893 | 13 |
| lh0.05_pe0.7 | 0.05 | 0.70 | 1.037 | 1229 | −20.68 | 0.070 | 0.00198 | 0.893 | 12 |
| lh0.02_pe0.7 | 0.02 | 0.70 | 1.064 | 1148 | −20.76 | 0.125 | 0.00353 | 0.894 | 2 |
| lh0.01_pe0.7 | 0.01 | 0.70 | 1.050 | 1129 | −20.81 | 0.098 | 0.00275 | 0.894 | 6 |
| lh0.02_pe0.8 | 0.02 | 0.80 | 1.050 | 1163 | −20.91 | 0.098 | 0.00277 | 0.893 | 7 |
| lh0.01_pe0.8 | 0.01 | 0.80 | 1.036 | 1130 | −20.93 | 0.072 | 0.00201 | 0.893 | 15 |
| **lh0.01_pe0.6** | **0.01** | **0.60** | **1.078** | **1117** | **−20.99** | **0.154** | **0.00426** | **0.894** | **1 (WINNER)** |
| lh0.02_pe0.6 | 0.02 | 0.60 | 1.057 | 1151 | −21.02 | 0.111 | 0.00310 | 0.894 | 4 |
| lh0.005_pe0.7 | 0.005 | 0.70 | 1.052 | 1104 | −21.02 | 0.105 | 0.00292 | 0.894 | 10 |
| lh0.005_pe0.9 | 0.005 | 0.90 | 1.053 | 1104 | −21.06 | 0.108 | 0.00299 | 0.893 | 8 |
| lh0.005_pe0.8 | 0.005 | 0.80 | 1.067 | 1106 | −21.12 | 0.134 | 0.00374 | 0.893 | 3 |
| lh0.005_pe0.6 | 0.005 | 0.60 | 1.063 | 1106 | −21.12 | 0.125 | 0.00348 | 0.894 | 5 |
| lh0.01_pe0.9 | 0.01 | 0.90 | 1.044 | 1134 | −21.32 | 0.087 | 0.00243 | 0.893 | 14 |
| lh0.02_pe0.9 | 0.02 | 0.90 | 1.041 | 1164 | −21.37 | 0.082 | 0.00230 | 0.892 | 16 |

**Winner (Borda over capture, EV, CVaR5):** `lh0.01_pe0.6` — PF 1.0779, n 1117, CVaR5 −20.99%,
EV 0.1537, capture 0.00426, pass_frac 0.894. Worst trade −56.82% (JL 2024-02-29). Per-year:
2023 PF 0.680 (n 112, CVaR5 −25.85); 2024 PF 1.139 (n 1005, CVaR5 −20.36).

### Escalation check

| Criterion | Threshold | Winner observed | Result |
|---|---|---|---|
| Winner CVaR5 | ≥ −10% | **−20.99%** | **FAIL (binding)** |
| Winner PF | ≥ 1.10 | **1.0779** | **FAIL** |
| n_trades | ≥ 60 | 1117 | pass |
| ≥1 config CVaR5 ≥ −10% | — | 0 / 16 | none |

### Comparison

| phase | config | PF | n | CVaR5 | EV | capture |
|---|---|---|---|---|---|---|
| CPD-BOCPD (winner) | lh0.01_pe0.6 | 1.078 | 1117 | −20.99 | 0.154 | 0.00426 |
| CPD-1 (best) | k12_h8 | 1.118 | 143 | −30.55 | 0.647 | 0.01645 |
| WJI-OPT (baseline) | p065_single | 1.188 | 2134 | −9.16 | 0.157 | 0.00000* |

\*capture not recorded for the WJI-OPT baseline row.

### Factual observations
- **CVaR5 is tightly clustered −20.1% to −21.4%** across all 16 configs; ~2× beyond the −10% floor.
  It is the sole binding constraint (n_trades and PF floors are met everywhere).
- BOCPD CVaR5 (~−21%) is less negative than CPD-1's CUSUM best (−30.55%) but still well past −10%.
- **pass_fraction ≈ 0.89 for every config** — the gate sits in PASS ~89% of post-warmup time.
- **n_trades ≈ 1100–1260** (~11–13 / event) vs CPD-1's 143 (~1.5 / event): the directional gate
  with the fixed 0.10 hysteresis gap toggles frequently (median per-trade PnL 0.00%).
- PF is just above breakeven everywhere (1.03–1.08). 2023 winner PF < 1 (0.680, n 112).

### Halted (escalation protocol)
T5 per-event charts and T6 timing benchmark were NOT run — the plan halts at the first
escalation. `event_charts/` and `timing_benchmark.json` are not produced.

## Artifacts (gitignored — local only)
`sigma_log_prior.json`, `bocpd_sweep_results.json`, `bocpd_winner.json`,
`bocpd_winner_per_year.json`. Regenerate via `python -m tools.phase_cpd_bocpd.t3_sweep`.
