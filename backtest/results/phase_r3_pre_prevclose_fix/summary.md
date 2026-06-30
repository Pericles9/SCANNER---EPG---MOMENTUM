# Phase R3 — ROC Gate Sweep — Summary

**Date:** 2026-06-25
**Sample commit:** `a3de9c7` (`val_r3_stratified.json`)
**Working params (held constant):** T_gate=500, p=0.65, first_pass, max_entry_lag_sec=300, τ=300, warmup=300.

> **Method note.** ROC is a pure pre-entry **admit/block** filter and is **not** implemented in
> `runner_rapid` (AUDIT-RAPID T8: `roc_min` is read but never applied; `RocBuffer` is not imported
> there). Because ROC only blocks whole events and does not alter the within-event entry/exit of
> admitted events, the six arms are derived by running the **disabled arm once** and post-filtering
> its per-event results on `roc_5m` (`admitted ⟺ first-appearance OR roc_5m ≥ roc_min`). This is
> exactly equivalent to running the gate and avoids an unrequested runner code change.

## Val sample (T1)

N_total = **86** (seed=42). Strata by `gap_pct_at_hit = (scanner_hit_price/prev_close − 1)×100`:

| Stratum | range | target | taken |
|---------|-------|--------|-------|
| low  | [30,100)   | 50 | 50 |
| mid  | [100,200)  | 30 | **16** (only 16 available after MDR≥200 exclusion — took all) |
| high | [200,∞)    | 20 | 20 |

Mid shortfall reduced total from 100 → 86. 1 event (VBIX 2024-07-22) dropped for insufficient
ticks and replaced (T1a). MDR≥200 diagnostic events excluded.

## roc_5m distribution (T2)

Of 86 events: **41 full-window, 4 partial-window, 41 first-appearance** (scanner hit at first
trade / pre-market gap-up → no prior poll → admitted unconditionally). roc_5m computed with
`RocBuffer` on a simulated 20s poll grid.

| stat | min | p10 | p25 | median | p75 | p90 | max |
|------|-----|-----|-----|--------|-----|-----|-----|
| roc_5m (n=45) | 0.0112 | 0.0455 | 0.0895 | 0.1666 | 0.2778 | 0.5602 | 0.8505 |

Only 5/45 (5.8% of all events) have roc_5m < 0.05 → the ">50% below lowest threshold" flag does
**not** trigger; the sweep range {0.05–0.25} spans p10–p75.

## Sweep results (T3)

Disabled-arm run: 80/86 processed (3 skipped, 3 errored — incl. SKYE 2023-12-06 missing
`quotes.parquet`), **28 traded**. All 28 trades fall in the **low-gap stratum**; the mid/high
strata and all 41 first-appearance events produced **zero trades**.

| roc_min | N attempted | N blocked | N 1st-appear | N traded | PF | CVaR5 | Mean Hold | time_gate% | epg_close% |
|---------|-------------|-----------|--------------|----------|------|--------|-----------|------------|------------|
| disabled | 86 | 0 | 41 | 28 | 2.0796 | −16.89% | 755s | 35.71 | 64.29 |
| 0.05 | 81 | 5 | 41 | 24 | 1.0883 | −16.89% | 678s | 41.67 | 58.33 |
| 0.10 | 73 | 13 | 41 | 18 | 0.4498 | −16.89% | 692s | 44.44 | 55.56 |
| 0.15 | 66 | 20 | 41 | 13 | 0.4106 | −16.89% | 623s | 38.46 | 61.54 |
| 0.20 | 60 | 26 | 41 | 7 | 0.5890 | −16.89% | 529s | 42.86 | 57.14 |
| 0.25 | 57 | 29 | 41 | 5 | 0.8005 | −16.89% | 680s | 60.0 | 40.0 |

PF falls monotonically as roc_min rises from disabled (2.08) through 0.10–0.15 (≈0.4). CVaR5 is
constant at −16.89% across arms: with n ≤ 28 the bottom-5% is a single trade, and the worst loser
(−16.89%) has high roc_5m so it survives every threshold (small-sample caveat).

## Selection value (T4)

Admitted = roc_5m ≥ roc_min (or first-appearance); Blocked = roc_5m < roc_min.

| roc_min | Admitted PF | Admitted CVaR5 | Admitted N | Blocked PF | Blocked CVaR5 | Blocked N | PF Δ |
|---------|------------|---------------|------------|-----------|--------------|-----------|------|
| 0.05 | 1.0883 | −16.89% | 24 | None¹ | 0.00% | 4 | — (blocked n<5) |
| 0.10 | 0.4498 | −16.89% | 18 | 93.56 | −1.22% | 10 | −93.11 |
| 0.15 | 0.4106 | −16.89% | 13 | 8.67 | −7.52% | 15 | −8.26 |
| 0.20 | 0.5890 | −16.89% | 7 | 2.96 | −12.20% | 21 | −2.37 |
| 0.25 | 0.8005 | −16.89% | 5 | 2.56 | −12.20% | 23 | −1.76 |

¹ blocked set at 0.05 has no losing trades (PF undefined) — also ≥ admitted.

PF Δ (admitted − blocked) is **negative at every reliable threshold**: the **blocked** events (low
roc_5m) outperform the **admitted** events (high roc_5m). Direct event-level check on the 28
traded events: **corr(roc_5m, pnl) = −0.431**; high-roc (≥median) mean pnl = **−2.55%** vs low-roc
mean pnl = **+8.23%**.

## Partial-window sensitivity (T5)

Disabled-arm trades by ROC window group:

| group | N | PF | CVaR5 |
|-------|---|------|--------|
| full window (≥300s) | 27 | 2.6979 | −12.20% |
| partial window (<300s) | 1 | 0.0 | −16.89% |
| first appearance (no roc) | 0 | — | — |

Only 1 partial-window event traded (a loser); first-appearance events did not trade at all, so the
partial-window admission rule is not driving the sweep result. (Partial events were threshold-checked
using their partial roc value.)

## Escalation status

| Criterion | Threshold | Observed | Result |
|-----------|-----------|----------|--------|
| Any stratum < 10 available after exclusion | <10 | low 562 / mid 16 / high 47 | cleared |
| >50% of events roc_5m < 0.05 | >50% | 5.8% | not flagged |
| **Best ROC arm CVaR5 < −15%** | <−15% | **−16.89%** (all arms) | **TRIGGERED** |
| Tightest arm (0.25) n_traded < 20 | <20 | **5** | **FLAGGED** (also 0.10/0.15/0.20 <20) |
| **Blocked PF ≥ admitted PF at every threshold** | all | **yes (PF Δ<0 all; corr −0.43)** | **TRIGGERED** |
| Prior result dirs modified (T3a) | any | byte-identical (142 files) | cleared (PASS) |

**Two hard-stop criteria triggered:** (1) ROC is anti-selective — blocked (low-roc) events
outperform admitted (high-roc) events at every threshold; (2) best ROC-arm CVaR5 = −16.89% < −15%.
Per the escalation protocol, the sweep/selection/partial tables are posted and no follow-on phase is
begun. One flag: tight arms (roc_min ≥ 0.10) trade < 20 events (small-sample / unreliable).

## Working-parameters note

T_gate=500 and p=0.65 were calibrated on the **MDR≥200 diagnostic sample (retired)**. These are
working parameters only. **R1-Final** (p_close re-sweep on `val_r3_stratified`) and **R2** (SF entry
tuning) must complete before **R5** (full val).

## Deviation note

Per-event charts produced for the **disabled arm only**. Cooper-flagged threshold-arm charts to
follow after selection (Cooper cannot flag an arm before seeing the sweep; this is the documented
deviation from the standard's "Cooper-flagged arm first").

## Results locations

`backtest/results/phase_r3/` (this phase). Baselines `phase_r1_fixed/` and `phase_r15/` — UNTOUCHED
(T3a verified, 142 files byte-identical).
