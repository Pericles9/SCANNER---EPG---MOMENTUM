# Phase R1.5 — Time Gate Sweep — Summary

**Date:** 2026-06-24
**Commit:** `0409332` — `feat(runner_rapid): R1.5 time-gate exit — cut losers at T_gate seconds`

## Mechanism

Single-shot time gate. At the first tick where `t_since_entry >= T_gate`, read open P&L; if
< 0, exit at the current tick (`exit_reason="time_gate"`). Once checked (win or lose), the gate
is disabled for the rest of the hold; EPG window close governs otherwise. Exit stack:
time_gate → epg_window_close → session_end. CLI `--t-gate-sec` (float|None, default None;
None is fully backward-compatible). Verified: 403 tests pass; sanity A (OMH loser →
`time_gate` @ 500.4s), sanity B (LRHC sub-400s winner → `epg_window_close` @ 324.6s).

## Sweep config

`entry_mode=first_pass`, `max_entry_lag_sec=300`, `p_open=p_close=0.65`, `roc_min=None`,
gate `peak` (τ=300, tau_peak=600, C=1.5, warmup=300), val MDR≥150 (n=100, seed=42),
`T_gate ∈ {400, 500, 600}`. Results in `backtest/results/phase_r15/`.

## Headline result

| T_gate | PF | CVaR5 | Mean hold | time_gate exits | false-gate% |
|--------|------|--------|-----------|-----------------|-------------|
| None   | 2.6584 | −27.74% | 1088s | 0 | — |
| 400    | 2.2166 | −25.93% | 774s | 18 | 33.3% |
| **500**| **2.8805** | **−25.42%** | 847s | 19 | 15.8% |
| 600    | 2.6919 | −29.61% | 892s | 19 | 21.1% |

**T_gate=500 is the sweet spot:** improves PF (+0.222) *and* CVaR5 (+2.32pp) vs the no-gate
baseline, with the lowest false-gate rate (15.8% — cuts 16 losers, 3 winners). T=400 fires too
early (33% false, sacrifices MBIO +24%, PF below baseline); T=600 fires too late (CVaR5 −29.61%,
worse than baseline, because losers bleed further before the cut). The tail benefit peaks at
~500s and reverses by 600s — consistent with the DIAG-MFE-MAE Chart 7 divergence point.

## Escalation status

No hard stops, no flags. n_time_gate_exits > 0 at every arm (18/19/19); CVaR5 improved at 400 and
500 (flag needs all-worse); false-gate < 50% at every arm; `phase_r1_fixed/` byte-identical
before/after (T5a PASS, 70 files).

## Deviation note

Per-event charts produced for the **T_gate=500 arm only** (intentional, per phase prompt): it is
the theoretically motivated arm from DIAG-MFE-MAE Chart 7, and the goal is confirming the
mechanism fires correctly on individual events. Final-arm charts follow Cooper's selection.

## Approval gate

Do not begin the R1 p_close re-sweep (selecting p_close with T_gate fixed) or any follow-on phase
until Cooper has reviewed these results and given explicit approval.
