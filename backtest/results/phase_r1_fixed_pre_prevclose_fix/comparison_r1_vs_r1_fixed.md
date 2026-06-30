# R1 vs R1-Fixed Comparison

**Date:** 2026-06-24
**Fix commit:** `724617a` (T4e + T6b)
**Runner:** `backtest/runner_rapid.py` (fixed)
**Sweep config (both old and new):** `entry_mode=first_pass`, `max_entry_lag_sec=300`,
`p_open=p_close` (symmetric), `roc_min=None`, gate `peak` mode, val MDR≥150 sample
(`val_mdr150_diagnostic.json`, 100 events, seed=42). Only the runner and the output
directory differ.

> **Old-baseline directory note.** The phase prompt names `results/phase_r1/` as "old R1."
> That directory is a *separately-invalidated, even older* run (`entry_mode=cross_and_hold`,
> no `max_entry_lag_sec`, pre-scanner-floor-fix — see `results/phase_r1/INVALIDATED.md`),
> which shows 100% `epg_window_close` and is **not** the T4e-affected run. The config-matched
> T4e baseline (same `first_pass` + `max_entry_lag_sec=300` symmetric sweep) is
> `results/phase_r1_mdr150/sym_p*`, and that is the OLD column used below. **Both** old
> directories are preserved untouched (`phase_r1/` verified byte-identical before/after, T6a).

## What changed

Two fixes were applied to `runner_rapid.py` (`_process_event_rapid`):

- **T4e** — the `max_entry_lag_sec` loop `break` is now gated on `and not in_position`.
  Previously the break fired at the first tick past `scanner_hit + 300s` regardless of whether
  a position was open, terminating exit monitoring of that open position and force-booking
  `session_end` at the final tick of the data. With the guard, the deadline still abandons
  *entry* (no new entry after the deadline) but never interrupts the EPG-window-close exit of a
  trade already on.
- **T6b** — the gate `λ_V` precompute loop now applies the halt `dt`-substitution
  (`gate_t_sec = t_prev + 1e-6` across detected halt windows), matching the Hawkes-EMA
  substitution in `_hawkes_replay_with_refit`, so the gate participation ratio no longer
  collapses across a trading halt. (Bounded effect: only events with a confirmed halt window.)

The behavioral consequence is entirely on the **exit** side: trades now exit where the EPG gate
actually closes (PASS→FAIL, i.e. `λ_V/peak` crossing below `p_close`) instead of being held to
end-of-data. `p_close` is exercised as an exit signal for the first time.

## Symmetric sweep comparison table

| p    | OLD PF | NEW PF | OLD epg_close% | NEW epg_close% | OLD hold_s | NEW hold_s | OLD CVaR5 | NEW CVaR5 |
|------|--------|--------|----------------|----------------|------------|------------|-----------|-----------|
| 0.50 | 2.6685 | 3.9693 | 0.0            | 100.0          | 39126      | 1605       | −68.73    | −29.92    |
| 0.55 | 2.6584 | 3.6631 | 0.0            | 100.0          | 39125      | 1464       | −70.38    | −30.07    |
| 0.60 | 2.8435 | 2.8452 | 0.0            | 100.0          | 39304      | 1318       | −70.27    | −28.89    |
| 0.65 | 3.1769 | 2.6584 | 2.17           | 100.0          | 38620      | 1088       | −68.73    | −27.74    |
| 0.70 | 3.5624 | 2.3073 | 6.52           | 100.0          | 37491      |  958       | −68.73    | −26.05    |
| 0.75 | 3.1834 | 2.2216 | 13.04          | 100.0          | 35842      |  772       | −68.73    | −24.37    |

Extended new-run metrics:

| p    | n  | NEW PF | win%  | mean_pnl% | NEW CVaR5 | mean_hold_s | mean_lag_s (scanner) | ptf/trade |
|------|----|--------|-------|-----------|-----------|-------------|----------------------|-----------|
| 0.50 | 48 | 3.9693 | 58.33 | 12.91     | −29.92    | 1605        | 57.0                 | 1.000     |
| 0.55 | 48 | 3.6631 | 56.25 | 11.15     | −30.07    | 1464        | 57.7                 | 1.000     |
| 0.60 | 46 | 2.8452 | 54.35 |  9.46     | −28.89    | 1318        | 59.0                 | 1.000     |
| 0.65 | 46 | 2.6584 | 54.35 |  8.27     | −27.74    | 1088        | 59.9                 | 1.065     |
| 0.70 | 46 | 2.3073 | 45.65 |  6.50     | −26.05    |  958        | 61.2                 | 1.174     |
| 0.75 | 45 | 2.2216 | 44.44 |  5.86     | −24.37    |  772        | 66.3                 | 1.022     |

## Exit reason shift

This is the primary validation that the T4e fix worked. In every config the exit distribution
flipped completely:

- **OLD:** `session_end` 86.96%–100% of trades; `epg_window_close` 0%–13.04%. The kept trades
  were almost all held to the final tick of the data (`mean_hold_sec` ≈ 35,800–39,300s).
- **NEW:** `epg_window_close` **100.0%** of trades at all six p values; `session_end` **0.0%**.
  Mean hold collapses to ~770–1,600s — the position now closes when the EPG gate closes.

Per-config one-liners (does `epg_window_close` now fire?):

- p=0.50 — yes: 0.0% → 100.0% (48/48). Hold 39,126s → 1,605s.
- p=0.55 — yes: 0.0% → 100.0% (48/48). Hold 39,125s → 1,464s.
- p=0.60 — yes: 0.0% → 100.0% (46/46). Hold 39,304s → 1,318s.
- p=0.65 — yes: 2.17% → 100.0% (46/46). Hold 38,620s → 1,088s.
- p=0.70 — yes: 6.52% → 100.0% (46/46). Hold 37,491s → 958s.  ← single-event MLGO sanity also confirmed.
- p=0.75 — yes: 13.04% → 100.0% (45/45). Hold 35,842s → 772s.

A genuine, monotonic `p_close` response now exists: raising `p_close` makes the gate close
earlier → shorter hold (1,605s → 772s) and lower PF (3.97 → 2.22, cutting winners sooner) but a
less-negative CVaR5 (−29.92% → −24.37%). Under the old (broken) runner the sweep moved almost
none of these — `p_close` was decoupled from the (session_end) exit.

## Chatter comparison

| p    | OLD ptf/event (all 100 ev) | NEW ptf/trade (traded ev) | NEW median | NEW p90 | NEW % ≥3 |
|------|----------------------------|---------------------------|------------|---------|----------|
| 0.50 | 0.021 | 1.000 | 1.0 | 1.0 | 0.0  |
| 0.55 | 0.021 | 1.000 | 1.0 | 1.0 | 0.0  |
| 0.60 | 0.021 | 1.000 | 1.0 | 1.0 | 0.0  |
| 0.65 | 0.062 | 1.065 | 1.0 | 1.0 | 2.2  |
| 0.70 | 0.135 | 1.174 | 1.0 | 1.0 | 2.2  |
| 0.75 | 0.115 | 1.022 | 1.0 | 1.0 | 0.0  |

Note the unit difference: OLD reports `mean_passtofail_per_event` averaged over all 100 events
(most of which never trade, so the mean is ≈0); NEW reports `mean_passtofail_per_trade` over the
traded events only. The increase is expected and is a direct consequence of the T4e fix: under
the old runner the loop `break` cut the post-scanner window short, so almost no PASS→FAIL
transitions were ever counted for the kept trades. The fixed loop runs the full post-scanner
session, so each trade records its own genuine exit transition (≈1.0) plus, on a couple of events,
post-exit gate chatter (max observed = 8 transitions on one p=0.70 event). With re-entry disabled
there is at most one trade per event, so per-trade ≈ per-event for traded events. The counter
spans the whole post-scanner window (not clipped to the hold), so it also captures chatter after
the trade has already exited.

Chatter is benign at every p: median 1.0, p90 1.0, and the fraction of trades with ≥3 PASS→FAIL
transitions is ≤2.2% everywhere — far below the 20% flag threshold.

## Escalation status

| Criterion | Threshold | Observed | Result |
|-----------|-----------|----------|--------|
| MLGO single-event exit still `session_end` after T4e fix | any | `epg_window_close`, hold 866.7s | cleared |
| `halt_intervals` not in scope at precompute loop | any | in scope (assigned `runner_rapid.py:493`) | cleared |
| `epg_window_close` exit rate across all 6 p = 0% | =0% | 100% at every p | cleared (fix produced expected change) |
| **Best new R1 CVaR5 < −15%** | < −15% | **best = −24.37% (p=0.75)** | **TRIGGERED** |
| New R1 best PF < R0 baseline PF | any | best new PF = 3.9693 vs R0 2.97 (T5) / 3.20 (T4) | cleared (not below) |
| Chatter > 20% with ≥3 transitions for all p | >20% all p | max 2.2% | not flagged |

**One hard-stop criterion is triggered: best new R1 CVaR5 = −24.37% (p=0.75), which is below the
−15% bar from the original R1 spec.** Per that spec the action is "post sweep table and exit
breakdown; do not proceed to R2." This phase does not proceed to R2 (the Approval Gate also
forbids it). All R1-Fixed deliverables (sweep, exit breakdown, chatter diagnostic, p=0.70
per-event charts, this report) are produced for Cooper's review.

Context for the CVaR5 reading (data only, no recommendation): the fix **improved** the tail
substantially — old CVaR5 ≈ −68% to −70% (deep end-of-data drawdowns on low-float names) →
new CVaR5 ≈ −24% to −30% (gate-driven exits). The tail still exceeds the −15% threshold at all
six symmetric p values. The least-negative CVaR5 occurs at the highest p (earliest exit). The
R0 T7 post-hoc `max_entry_lag_sec=300` reference row (genuine gate exits, no max-lag break)
reported CVaR5 = −27.74% at the same lag filter, consistent with the new p=0.65 value (−27.74%).

## Old results location

`backtest/results/phase_r1_mdr150/sym_p*`  ← config-matched T4e baseline (OLD column above; T4e active, invalid as exit calibration)
`backtest/results/phase_r1/`               ← older separately-invalidated run (cross_and_hold, pre-floor-fix); preserved untouched (T6a)

## New results location

`backtest/results/phase_r1_fixed/`         ← corrected runner (T4e + T6b)
