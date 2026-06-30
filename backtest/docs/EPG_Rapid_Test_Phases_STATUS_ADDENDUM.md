---
tags:
  - type/plan
  - domain/backtest
  - project/scanner-epg-momentum
  - status/addendum
created: 2026-06-30
companion: EPG_Rapid_Test_Phases.md
---

# EPG_Rapid_Test_Phases — Status Addendum (2026-06-30)

**Why this is a separate file, not a full rewrite:** I only had partial visibility into
`EPG_Rapid_Test_Phases.md` in this session (the R1–R4 task-level internals, ~lines
186–859, were truncated in what I read). Rather than guess at content I haven't actually
seen, this addendum is meant to be merged into the existing file by hand — drop the
**Phase Map** replacement in at the top, and the **Phase Status Log** as a new section
right after it, before the detailed C1 spec begins. The original 1044-line file's
phase-by-phase task specs are still the historical record of what was *planned*; this
addendum is the record of what actually *happened*, which now diverges from the plan in
several places.

---

## Updated Phase Map (replaces the original table)

| Phase | Type | Purpose | Actual Status |
|---|---|---|---|
| C1 | Component | `setup_filter` modularity (`rho_fast` + `entry_eligible`) | Built. `entry_eligible()` not used in production path — see R2. |
| C2 | Component | `LuldProximityExit` independent upper/lower | Built. Not called by current exit stack — see R4. |
| C3 | Component | Halt-gap clock pause | Built, **verified active** (T6b fix, commit `724617a`). |
| C4 | Component | ROC 5-min buffer | Built, tested (R3), gate disabled. |
| R0 | Integration | Rapid runner build + parity + gate-consistent baseline | Complete. Two runner bugs found post-hoc (T4e, T6b), fixed in `724617a` — **this invalidated all R0/R1 results that predate the fix.** |
| R1 | Tuning | Gate threshold (`p_open` × `p_close`) | **In progress.** R1-Fixed sweep complete (p=0.50–0.75, T_gate=500 fixed) on `val_r3_stratified.json` — see results table below. **R1-Final selection not made — blocked on DIAG-DQ (data quality).** |
| R1.5 | Tuning (proposed, retroactive) | One-shot time-gate exit (`T_gate`) | **Complete, not originally planned.** Commit `0409332`. T_gate=500s selected as working value. Proposing this get formally numbered R1.5 for the audit trail — confirm naming. |
| R2 | Tuning | Setup filter entry (`rho_fast` × `n_hold`) | **Complete — negative result.** SF cross-and-hold removed from the entry path; rapid entry is now plain `first_pass`. Confirms the prior risk flagged in `EPG_Rapid_Strategy.md` §1.1 (EPG-OPT2-SF pattern). Specific R2 sweep numbers not in my current context — pull from `results/phase_r2/summary.md` if you want them logged in the strategy doc. |
| R3 | Tuning | 5-min ROC gate threshold | **Complete — negative result.** corr(roc_5m, pnl) = −0.431 across all thresholds tested; blocked events outperformed admitted ones. Gate disabled permanently. |
| R4 | Tuning | LULD halt-avoidance, two-sided | **Not run.** Superseded — LULD dropped from the EPG-Rapid exit stack before R4 was reached. No commit/phase record ties this to an explicit decision that I can see from the handoff text alone — **needs your confirmation** on whether this was deliberate (and if so, log the reasoning) or an artifact of other rapid-runner work that should be revisited. |
| R5 | Milestone | Full-val + conditional test | **Not reached.** Blocked behind R1-Final, behind DIAG-DQ. |
| FIX-PREVCLOSE (proposed name) | Fix | `prev_close` 20:00→16:00 ET cutoff bug | **Complete.** Commit `df13c4a`. Post-fix CVaR5 improved dramatically (best −8.27%, clearing the −15% floor). |
| FIX-T4E-T6B (proposed name) | Fix | Runner bugs: entry-lag loop break terminating exit monitoring; gate λ_V decay not halt-aware | **Complete.** Commit `724617a`. Invalidated all prior R0/R1 results. |
| AUDIT-VAL-SAMPLE (proposed name) | Audit | Replace contaminated MDR≥200 diagnostic sample (44/100 events never hit scanner, look-ahead bias) with proper stratified sample | **Complete.** `val_r3_stratified.json`, commit `a3de9c7`. Now the active 100-event val sample. |
| DIAG-ENTRY | Diagnostic | Characterize entry-failure reasons by category | **Complete.** Established the `n_trades_before_scanner` separation between TRADED and non-TRADED events — the core evidence behind the sparse-warmup finding. |
| DIAG-TAPE | Diagnostic | Classify first-appearance events by where the gap actually developed (T1_POSTMARKET / OVERNIGHT_NO_TAPE / T_PREMARKET) | **Complete, but exact counts per category not yet pulled into a summary.** Action item carried forward — see DIAG-DQ below, or a follow-up read of `backtest/results/phase_diag_tape/data_availability.json` and `summary.md`. |
| **DIAG-DQ (new)** | Diagnostic | Data-quality characterization + chart-standards regeneration | **Next phase.** See `Phase_DIAG-DQ_Agent_Prompt.md`. Blocks R1-Final and the sparse-warmup scope decision. |

---

## Phase Status Log — narrative

In rough chronological order, for anyone picking this back up:

1. **R0** ran, integration looked clean.
2. Two runner bugs were found in review (not caught by R0's own checks): `max_entry_lag_sec`'s loop break was terminating exit monitoring on already-open positions, force-booking session-end exits early (T4e); and the gate's `λ_V` precompute loop wasn't applying the halt-aware `dt`-substitution, so halted time was still advancing the decay clock (T6b). Both fixed in `724617a`. **Everything generated before this commit is invalid** and was not reused.
3. Separately, the val sample itself was found contaminated — the MDR≥200 diagnostic sample had 44/100 events that never actually hit the scanner (look-ahead bias baked into how it was built). Replaced with `val_r3_stratified.json`, a properly stratified 100-event sample.
4. **R2** (SF cross-and-hold entry) and **R3** (ROC gate) both ran and both came back negative. Both mechanisms were removed rather than tuned further. This is two for two on "make entry smarter" ideas failing — worth keeping in mind if Path B (§11 of the strategy doc) is chosen later, since it's the same category of idea (add a smarter admission/timing layer) that's failed twice already, just applied to a different problem (sparse warmup instead of speed).
5. A `prev_close` data bug was found and fixed (20:00 ET cutoff returning post-market prices instead of the official 16:00 ET close) — this had been inflating scanner thresholds for earnings-gap events. Fixing it produced a large CVaR5 improvement.
6. A time-gate exit (`T_gate=500s`) was added — not in the original R1–R5 plan — after a P&L band divergence was observed at ~500s post-entry (winners and losers occupy the same space before that, separate cleanly after). Selected value improves both PF and CVaR5.
7. **R1-Fixed** (gate threshold sweep, corrected data, T_gate fixed at 500) ran. Results table below. n_trades dropped to 24–25 vs. the old 28-trade floor — but that floor came from the now-retired contaminated sample, so this isn't a real regression, it's the sparse-warmup problem expressed as a trade count.
8. **DIAG-ENTRY** traced the 58/95 non-traded events to entry-timing failure categories (ANCHOR_LATE 42%, WARMUP_AT_DEADLINE 19%, etc.), and found the clean separator: TRADED events average ~38,000 pre-scanner trades, non-TRADED events average ~0. This is the structural sparse-warmup finding.
9. **DIAG-TAPE** classified first-appearance events by where the gap actually developed (post-market the night before vs. genuinely overnight with no tape vs. gradual pre-market) — exact counts not yet summarized.
10. Reviewing R1-Fixed's per-event charts surfaced dirty tick data in some events, and confirmed the charts themselves don't meet the project chart standard. **This is where the project currently sits** — blocked on a combined data-quality + chart-standards phase before R1-Final or the sparse-warmup scope decision can be trusted.

---

## R1-Fixed Results Table (for reference — also in `EPG_Rapid_Strategy.md` §2.1)

`val_r3_stratified.json`, p_open=p_close swept, T_gate=500 fixed:

| p | n_trades | PF | WR% | mean PnL | CVaR5 |
|---|----------|-----|-----|----------|-------|
| 0.50 | 25 | 1.24 | 48.0 | 0.80 | −17.50 |
| 0.55 | 25 | 1.44 | 52.0 | 1.41 | −20.36 |
| 0.60 | 25 | 1.61 | 52.0 | 1.99 | −17.50 |
| 0.65 | 24 | 1.43 | 41.7 | 1.15 | −17.50 |
| 0.70 | 24 | 1.26 | 37.5 | 0.59 | −8.42 |
| 0.75 | 24 | 1.79 | 50.0 | 1.63 | −8.27 |

No selection made yet — pending DIAG-DQ.

---

## Open Naming / Bookkeeping Items For Cooper

- Confirm or rename the proposed phase labels above (`R1.5`, `FIX-PREVCLOSE`,
  `FIX-T4E-T6B`, `AUDIT-VAL-SAMPLE`) — I picked names that fit the existing convention
  (R0/R1/R1.5/R3/DIAG-X/FIX-X/AUDIT-X) but these are my proposals, not yours.
- Confirm whether R4 (LULD) should be formally marked retired, or left open pending a
  decision on whether to restore LULD coverage.
- DIAG-TAPE's category counts should get pulled into an actual summary file — right now
  they're referenced but not tabulated anywhere. Could be folded into DIAG-DQ's scope or
  done separately; your call.
