# Phase FIX-T4E-T6B — Summary

**Date:** 2026-06-24
**Commit:** `724617a` — `fix(runner_rapid): T4e + T6b — exit loop and gate halt substitution`

## Fixes

- **T4e** (`runner_rapid.py` ~line 603): `max_entry_lag_sec` loop `break` now gated on
  `and not in_position`. Previously it terminated exit monitoring of an open position and
    force-booked `session_end` at end-of-data. All R1 results with `max_entry_lag_sec` set were
      invalidated by this.
      - **T6b** (`runner_rapid.py` ~line 551): gate `λ_V` precompute loop now applies the halt
        `dt`-substitution (`gate_t_sec = t_prev + 1e-6` across detected halt windows), matching the
          Hawkes-EMA substitution. `anchor.update()` keeps the raw timestamp.

          Verification: 403 tests pass; MLGO 2023-12-06 single-event sanity flipped from `session_end` to
          `epg_window_close` (hold 866.7s, +1.96%).

          ## Sweep config (R1-Fixed T1, symmetric)

          `entry_mode=first_pass`, `max_entry_lag_sec=300`, `roc_min=None`, gate `peak` mode
          (tau_peak=600, C=1.5, EPG τ=300, warmup=300), `p_open=p_close ∈ {0.50,0.55,0.60,0.65,0.70,0.75}`,
          val MDR≥150 sample (`val_mdr150_diagnostic.json`, 100 events, seed=42). Results in
          `backtest/results/phase_r1_fixed/`.

          ## Headline result

          Exit distribution flipped from **0–13% `epg_window_close` (old) to 100% at every p (new)**;
          `session_end` 87–100% → 0%. Mean hold collapsed from ~36k–39k s to ~770–1,600s. `p_close` now
          controls exit timing (monotonic: higher p → earlier exit → shorter hold, lower PF, less-negative
          CVaR5). Chatter benign (median 1.0/trade, ≤2.2% with ≥3 transitions).

          ## Escalation status

          **One hard-stop triggered:** best new R1 CVaR5 = **−24.37%** (p=0.75) < −15% (original R1 spec
          bar). Action per spec: post sweep table + exit breakdown; do not proceed to R2. The Approval Gate
          independently blocks R2. The fix nonetheless improved the tail materially (old CVaR5 ≈ −68% to
          −70% → new ≈ −24% to −30%).

          Other criteria cleared: MLGO no longer `session_end`; `halt_intervals` in scope; `epg_close` rate
          > 0% (100%); best new PF (3.97) not below R0 baseline (2.97/3.20); chatter not flagged.

          ## Deviation note

          Per-event charts produced for the **p=0.70 arm only** (deviation from the R1 spec's "chart the
          Cooper-flagged arm"), because the primary goal here is confirming the T4e fix works, not arm
          selection. Documented in the phase prompt.

          ## Approval gate

          Do not begin R2 or select a `p_close` value until Cooper has reviewed these R1-Fixed results and
          given explicit approval.
          