<!-- fullWidth: false tocVisible: false tableWrap: true -->
---
tags:
  - type/strategy
  - domain/backtest
  - domain/hawkes
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/active
created: 2026-06-18
parent_strategy: epg
strategy_id: epg_rapid
---

# 

---

tags:

- type/strategy
- domain/backtest
- domain/hawkes
- domain/microstructure
- project/scanner-epg-momentum
- status/in-progress created: 2026-06-18 updated: 2026-06-30 parent_strategy: epg strategy_id: epg_rapid

---

# EPG-Rapid — Strategy Outline

**Status:** In progress — gate threshold (R1) and entry/exit mechanism (R2/R3) phases complete or superseded; blocked on data-quality + sparse-warmup decision before R1-Final. See §11 and §12. **Strategy id:** `epg_rapid` **Parent:** `epg` (shares live infrastructure, gate code, exit code)

**Related documents:**

- `LULD_Halt_Architecture.md` — full regulatory mechanics of the rebuilt LULD module (historical reference — LULD is not currently in the EPG-Rapid exit stack, see §5)
- `Phase_LULD_REBUILD.md` — the rebuild's own agent prompt and original (upper-only) sweep
- 2026-06-30 context handoff — sparse-warmup problem, data-quality findings, the source for most of the updates in this revision

> **2026-06-30 revision note:** This document was last fully accurate as of 2026-06-18. Since then, R2 (setup-filter entry) and R3 (ROC gate) both resolved negative and their mechanisms were removed; LULD was dropped from the exit stack; a time-gate exit was added outside the original phase plan; and a structural sparse-warmup problem was found that blocks R1-Final. This revision updates every section to match. Items I could not independently verify (I don't have repo access — these are reported from Cooper's own handoff doc) are marked **\[CONFIRM\]**.

---

## 1\. Thesis

Classic EPG enters on the first EPG `FAIL→PASS` rising edge after a 300s warmup. On a pre-market dead-to-live gap event, that timing is too slow — by the time the gate opens, the best part of the move is often gone.

EPG-Rapid keeps the **validated exit machinery** and replaces only the **entry**:

- **Entry:** fire on the first EPG gate PASS tick at or after scanner hit. See §3 — this has changed from the original cross-and-hold design.
- **Exit:** time gate → EPG window close → session end. See §4 — LULD and EXIT_D are both out of the current stack.
- **Re-entry:** hard off. One trade per ticker per session.

The target event is the **dead-tape-to-burst transition**: a stock trading on near-zero volume from 4am, news hits 7–9am ET, volume explodes, price runs 100–200%+ over the next 1–2 hours. The scanner fires at `todaysChangePerc ≥ 30%`.

### 1.1 A known prior risk this design had to confront

**Classic EPG's first entry has no setup-filter gate at all** — it is rising-edge only. This was tested and locked. Phase EPG-OPT2-SF gated classic-EPG first entry on current-bar SF qualification and found it **net-negative**: mean ΔPF = −0.085, 47 of 52 top-decile configs degraded. Reason: *"The filter blocks the early-impulse entries that carry this strategy's alpha."*

EPG-Rapid originally bet that the SF-based admission check would behave differently here because it was solving the opposite problem (rising-edge being too *slow*, not too fast). **That bet did not pay off — see §1.3.**

### 1.2 Open structural problem: the strategy may not be trading its own target event

A sparse-warmup problem (full writeup in §11) means the EPG gate cannot reach PASS within the entry window for true first-appearance, no-pre-tape gap-ups — exactly the dead-tape-to-burst events this strategy was built for. The \~24 events (of \~95 on the current val sample) that currently trade well are **gradual risers**: stocks already actively trading and climbing for hours before crossing 30%, not stocks that jumped from silence. This is not a tuning problem, it's a property of the entry mechanism's dependency on pre-existing tape. **Cooper's current lean is Path A** — narrow scope to the gradual-riser universe explicitly, rather than try to re-architect entry to recover sparse events. This is not yet locked; see §11 for the decision record and §12 for what has to happen first (data-quality validation).

### 1.3 Setup-filter cross-and-hold — tested, removed

R2 tested SF-based cross-and-hold admission (§3.2's original design, kept below for the record). The mechanism was removed from the live entry path; the current rapid runner uses plain `first_pass` entry with no SF precondition. This is consistent with the §1.1 prior — SF admission, even retuned for speed, ended up costing the same kind of early-impulse entries that classic EPG's EPG-OPT2-SF phase found. **\[CONFIRM\]** — I don't have the actual R2 sweep table in context; if you want the specific PF deltas logged here for the audit trail, point me at `results/phase_r2/summary.md` and I'll fold it in.

---

## 2\. Locked / Current Decisions

| Decision                       | Choice                                                                 | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------------ | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Gate                           | `ParticipationGate` (`EPGClassicGate`) — peak mode, symmetric          | `half_life_seconds=300`, `warmup_seconds=300`. `tau_peak`/`C` confirmed **inert** in peak mode (verified directly against `gate.py`, 2026-06-30) — they only matter in the unused WJI-v2 `"background"` branch. Peak cooling disabled (`m_cool_sec` not passed → 0.0, no decay).                                                                                                                                                                                                                                                                                                                                                                                                           |
| Gate threshold                 | `p_open = p_close` — **R1-Fixed sweep done, final selection NOT yet made** | Swept 0.50–0.75 on `val_r3_stratified.json` with T_gate=500 fixed (table in §2.1). R1-Final is blocked behind the data-quality phase (§12) — do not lock a value before that runs.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Gate role                      | Entry qualifier **and** primary exit driver                            | Entry requires gate PASS; exit fires on PASS→FAIL/INACTIVE via EPG window close.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| EXIT_D                         | **Disabled**                                                           | Code retained, not evaluated. Unchanged from original design.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Time gate (`T_gate`)           | **Added — not in original plan**                                       | One-shot check at `T_gate=500s` since entry: if open P&L < 0, cut. Fires once, then disabled. `T_gate=500` selected as working value (improves PF and CVaR5 vs no-gate on the diagnostic sample). This effectively replaced LULD as the "stop the bleeding" mechanism — see §4. Verified in source, commit `0409332`. Should retroactively get a phase number for the audit trail — proposing **R1.5** per project convention (R0, R1, R1.5, R3...).                                                                                                                                                                                                                                       |
| LULD proximity                 | **Dropped from the EPG-Rapid exit stack entirely**                     | Was "enabled, both sides, R4-tuned" in the original plan. Per the 2026-06-30 handoff this is no longer in the runner's exit stack at all. **\[CONFIRM\]** — I don't have a phase record (no R4 run, no FIX-X commit) tying this to an explicit decision. Worth confirming whether this was a deliberate call (e.g., RTH-only coverage wasn't worth it once the time gate existed) or fell out as a side effect of other rapid-runner work. If deliberate, log the reasoning here and retire §5 below or mark it formally superseded. If accidental, this needs its own look — losing halt-avoidance coverage on RTH portions of trades is a real risk-management gap, not a free simplification. |
| EPG window close               | Enabled                                                                | PASS→FAIL or PASS→INACTIVE. Across all runs, 50–80% of exits are EPG window close regardless of other mechanisms — this remains the binding exit constraint.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Re-entry                       | Hard off                                                               | One entry per ticker per session. `closed_today=True` set at entry.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| Entry qualification            | **`first_pass`** — first EPG gate PASS tick at or after scanner hit    | No rising-edge requirement. No setup-filter precondition (removed, §1.3). No ROC precondition (disabled, below). Entry deadline `max_entry_lag_sec=300` — abandon if no PASS within 300s of scanner hit.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ROC gate                       | **Disabled, permanently**                                              | R3 found it anti-selective: corr(roc_5m, pnl) = −0.431 across thresholds — blocked events outperformed admitted ones. Do not re-enable without new evidence overturning this.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Scanner heat / quartile        | Void as a gate — analysis fields only                                  | Q3/Q4 heat environments significantly outperform Q1/Q2; rank-1-on-scanner systematically underperforms regardless of heat. Confirmed not a thin-day artifact. No gate applied.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Halt handling                  | Pause decay clocks across halt gaps                                    | Hawkes EMA and gate `λ_V` do not decay across detected halt windows. T6b fix verified in source — `dt`-substitution applied at halt-window crossings.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Sparse-warmup / universe scope | **Open — current lean Path A**                                         | See §11. Blocking decision, pending §12.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Data quality                   | **Open — blocking**                                                    | See §12. Dirty tick data found in some events post-R1-Fixed; severity/concentration not yet characterized. Next phase.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Baseline                       | Best validated EPG on val sample, **same exit stack as current EPG-Rapid** | §7's original baseline assumed LULD-on. Needs re-running against the actual current stack (time gate + EPG window close + session end, no LULD) once that's confirmed locked — see the LULD `[CONFIRM]` row above.                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Test split                     | Untouched                                                              | Opened once at the very end of the full pipeline. Nowhere near that point.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |

---

### 2.1 EPG Gate Threshold — R1-Fixed Results (not yet selected)

Corrected R1-Fixed sweep, `val_r3_stratified.json` (95–97 events, seed=42, stratified 50/27/20 low/mid/high gap), `p_open = p_close`, T_gate=500 fixed:

| p    | n_trades | PF   | WR%  | mean PnL | CVaR5  |
| ---- | -------- | ---- | ---- | -------- | ------ |
| 0.50 | 25       | 1.24 | 48.0 | 0.80     | −17.50 |
| 0.55 | 25       | 1.44 | 52.0 | 1.41     | −20.36 |
| 0.60 | 25       | 1.61 | 52.0 | 1.99     | −17.50 |
| 0.65 | 24       | 1.43 | 41.7 | 1.15     | −17.50 |
| 0.70 | 24       | 1.26 | 37.5 | 0.59     | −8.42  |
| 0.75 | 24       | 1.79 | 50.0 | 1.63     | −8.27  |

CVaR5 clears the −15% floor comfortably at p=0.70/0.75 (best −8.27%) — a real improvement from the `prev_close` fix and the time gate. PF is materially lower than the old (retired, contaminated) MDR≥200 sample's best \~3.9 — expected, since that sample was best-case 200%+ movers only. n_trades=24 at p=0.65 triggered the escalation against the prior sample's 28-trade floor, but that floor came from the retired sample — the real read is "the strategy currently catches \~24–25 of \~95 events," which is the §11 problem expressed as a trade count, not a regression.

**What controls what:**

- `p_open` — how high participation must be to declare the regime active (entry).
- `p_close` — how low it must fall before declaring exhaustion (exit timing). With EXIT_D absent, too-low lets losers run; too-high chops out of good trades on noise.

R1-Final selection of `p_open`/`p_close` is deferred until the data-quality phase (§12) confirms these numbers aren't contaminated by dirty ticks.

---

## 3\. Entry Stack (current)

```
Scanner hit (todaysChangePerc ≥ 30%)
    ↓
EPG gate evaluated — PASS check on each live tick
  (if WARMUP/INACTIVE at scanner hit, wait for first live PASS tick)
    ↓
First PASS tick at or after scanner hit → ENTRY (entry_mode = first_pass)
  No rising-edge requirement.
  No setup-filter precondition (§3.2 — removed).
  No ROC precondition (§3.1 — disabled).
    ↓
Entry deadline: max_entry_lag_sec = 300s.
  No valid entry within 300s of scanner hit → event abandoned.
    ↓
closed_today = True   (no re-entry this session)

```

The bimodal entry lag (median \~0s, mean \~58s on the earlier sample) is the expected `first_pass` distribution given gate state at scanner hit, not a bug.

### 3.1 ROC Gate (5-minute) — DISABLED

> Kept for the record. **Not active.**

Originally: `roc_5m = pct_change[t_now] − pct_change[t_now − 5min]`, gate `roc_5m ≥ roc_min`, swept 5–25% in Phase R3. **Result: anti-selective.** corr(roc_5m, pnl) = −0.431 — blocked (low-ROC) events outperformed admitted (high-ROC) events at every threshold tested. Disabled permanently. Do not re-enable without new evidence.

### 3.2 Setup Filter — Cross-and-Hold — REMOVED

> Kept for the record as the original design rationale and the reason this was tried. **Not active.** Current entry is plain `first_pass` (§3 above) with no SF gate of any kind on first entry.

The original premise: replace classic EPG's rising-edge timing signal entirely with an SF-based admission check (`entry_eligible()`, `rho_fast` lowered toward \~0.75 to cut smoothing lag), on the bet that the dead-tape-to-burst transition is different enough from a generic EPG rising edge that SF admission would help rather than hurt, unlike in classic EPG's EPG-OPT2-SF finding (§1.1). R2 tested this and it did not hold — see §1.3. The 15-bar sustain remains in place as the **live system's** continuous disqualifier during position management; that's a separate mechanism from the backtest's first-entry gate and was never affected by this change.

---

## 4\. Exit Stack (current)

First exit to fire wins, checked each trade tick.

```
1. Time gate (T_gate = 500s) — one-shot, fires once at 500s since entry,
   cuts if open P&L < 0, then disabled for the remainder of the trade
2. EPG window close — PASS → FAIL / INACTIVE
3. Session end — fallback if still in position at last tick

```

EXIT_D is **not** in the stack (unchanged from original design). LULD proximity is **not** in the stack (changed — see §2 and §5).

---

## 5\. The LULD Exit — SUPERSEDED, NOT CURRENTLY ACTIVE

> **Status as of 2026-06-30: not in the EPG-Rapid exit stack.** Everything below is the original design — kept for history and in case this gets revisited. **\[CONFIRM\]** with Cooper whether this was an intentional removal and, if so, why, before treating this section as dead. If LULD coverage is something you still want (it was specifically a halt-avoidance/risk-management exit, not an alpha source — losing it means RTH halt exposure during open positions is currently unmitigated, covered only by the time gate and EPG window close, neither of which is halt-aware in the way LULD proximity was designed to be), this is worth restoring rather than letting it quietly disappear.

**Original design (kept for reference):** EPG-Rapid was to use the **rebuilt** LULD module (quote-based, sticky reference price) on both sides — upper and lower bands independently tuned in Phase R4. The lower band was specifically re-enabled relative to classic EPG because the rationale for disabling it (pre-empting EXIT_D) doesn't apply once EXIT_D is gone. RTH-only by construction (the rebuilt module returns INACTIVE outside 09:30–16:00 ET) — pre-market halt exposure was always uncovered by this exit regardless. Given the target event is heavily pre-market, this was already a known partial-coverage exit even when active.

Full original mechanics, the reference-chasing bug fix, and the precision/recall tuning objective are preserved in `LULD_Halt_Architecture.md` and `Phase_LULD_REBUILD.md` if this gets revived.

---

## 6\. Halt-Gap Clock Handling

Unchanged, verified.

- **Source of truth:** `detect_luld_halts()` produces a `HaltWindow` list (30s VWAP-band breach + gap detection) from the event's trades.
- **Hawkes EMA / gate `λ_V`:** for any trade pair straddling a halt gap, substitute `dt = 0` (or epsilon) in the exponential decay term so intensity and `λ_V` do not collapse across the suspension. T6b fix (commit `724617a`) confirmed this is correctly wired — verified directly against source.
- **Setup filter bars:** halt gaps generally fall on bar boundaries; a bar spanning a halt boundary is treated as the active portion only.

---

## 7\. Baseline Definition — NEEDS RE-RUN

The EPG-Rapid numbers are only meaningful against a baseline run with the **same exit stack**. The original baseline spec below assumed LULD-on — that's now wrong given §5. **Re-run the baseline against the current stack (time gate + EPG window close + session end, no LULD, no EXIT_D) once the LULD question in §2/§5 is resolved one way or the other.** Don't compare current EPG-Rapid numbers to the old LULD-inclusive baseline in the meantime — it's not an apples-to-apples comparator anymore.

Baseline entry stays **classic rising-edge only** — no `entry_eligible()` call, no setup-filter precondition of any kind on first entry. This was the actual confirmed behavior of classic EPG and is unaffected by anything in this revision.

Reported deltas: PF, trade count, mean entry lag, CVaR5, exit-reason distribution.

---

## 8\. Code Modularity Changes

Historical record of the additive changes made to reach the current state. Status column added to reflect what's actually active.

| Change                                               | File(s)                                    | Status                                                                                                                                                                                                                                      |
| ---------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `_compute_setup_signals(..., rho_fast=RHO_FAST)`     | `setup_filter.py` (root + 2 copies)        | Built (C1). Not exercised in current entry path since SF entry was removed (§3.2). May still be used elsewhere (live system continuous disqualifier) — unaffected.                                                                          |
| `entry_eligible(result, n_hold)`                     | `core/filters/rapid_entry.py`              | Built (C1). **Not called** by the current rapid entry path (`first_pass` doesn't use it). **\[CONFIRM\]** whether to delete, deprecate-in-place, or keep dormant for a possible Path B revival (§11) that might want a gated-admission mechanism again. |
| `LuldProximityExit` independent upper/lower thresholds | `core/exits/luld_proximity.py`             | Built (C2). Not called by the current rapid exit stack (§5).                                                                                                                                                                                |
| Halt-gap `dt=0` substitution in replay               | `runner.py` / `runner_rapid.py`            | **Active, verified (T6b).**                                                                                                                                                                                                                 |
| ROC 5-min buffer                                     | scanner monitor + backtest snapshot reader | Built (C4), tested (R3), **disabled** at the gate level. Buffer itself may still be computed/logged even though the gate isn't applied — confirm if you want it stripped or kept as a logged-only feature.                                  |
| Time gate (`T_gate`)                                 | `runner_rapid.py`                          | **Active, new, not originally planned.** Commit `0409332`.                                                                                                                                                                                  |
| Rapid runner                                         | `runner_rapid.py`                          | Active. T4e/T6b fixes landed (`724617a`).                                                                                                                                                                                                   |

---

## 9\. Open Decisions

| \#  | Decision                                                 | Status                                           |
| --- | -------------------------------------------------------- | ------------------------------------------------ |
| 1   | LULD: revive, formally retire, or leave dormant          | **Open — see §2/§5 \[CONFIRM\]**                 |
| 2   | Sparse-warmup / universe scope (Path A/B/C)              | **Open — lean Path A, see §11.** Blocked behind §12. |
| 3   | Data quality characterization and remediation            | **Open — blocking everything else.** See §12.    |
| 4   | `T_gate` retroactive phase numbering (proposed R1.5)     | Open — administrative, doesn't block research    |
| 5   | `entry_eligible()` / dormant SF-entry code: keep or remove | Open — low priority                              |
| 6   | Position-size scope for false-exit cost reporting        | Open — paper-trade concern, not blocking         |

---

## 10\. Config Skeleton (current, approximate)

```json
{
  "strategy_id": "epg_rapid",
  "gate": {
    "type": "participation",
    "mode": "peak",
    "half_life_seconds": 300,
    "p_open": null,
    "p_close": null,
    "warmup_seconds": 300,
    "m_cool_sec": 0.0
  },
  "entry": {
    "mode": "first_pass",
    "max_entry_lag_sec": 300,
    "require_epg_pass": true
  },
  "reentry": { "enabled": false },
  "exit": {
    "time_gate": { "enabled": true, "t_gate_sec": 500 },
    "epg_window_close": { "enabled": true },
    "exit_d": { "enabled": false },
    "luld": { "enabled": false }
  },
  "halt": { "gap_seconds": 60, "pause_decay": true },
  "roc_gate": { "enabled": false }
}

```

`p_open`/`p_close` left null — R1-Final selection pending §12. This skeleton replaces the 2026-06-18 version, which included an active ROC gate and a two-sided LULD block that no longer reflect the running config. **\[CONFIRM\]** this against the actual `runner_rapid.py` config schema before treating it as authoritative — I built it from the handoff's prose description, not the source file.

---

## 11\. Open Structural Decision — Sparse Warmup / Universe Scope

Full investigation in the 2026-06-30 context handoff. Summary:

**The problem:** the EPG gate needs accumulated pre-scanner trade history to reach PASS (Hawkes replay → EventAnchor fire → 300s warmup, in sequence). True first-appearance gap-ups have little or no pre-scanner tape, so this chain can't complete before the 300s entry deadline. 61% of events fail entry on this (ANCHOR_LATE 42%, WARMUP_AT_DEADLINE 19%). The high-gap stratum (>200% gap) is 0/20 traded — a complete structural failure for that stratum, not a tuning artifact.

**The deeper tension:** the events that *do* trade (\~24 of \~95) are gradual risers — already actively trading for hours before crossing 30%. The strategy was designed for dead-tape-to-burst events, which are structurally the events it currently can't trade.

**Three paths, evaluated in the handoff:**

- **Path A** — accept the gradual-riser universe, add an explicit `n_trades_before_scanner > threshold` filter, document it as a design constraint, optimize on that universe. Honest, immediately actionable, abandons the original thesis.
- **Path B** — force anchor = scanner hit + reduced warmup for sparse events (`sparse_warmup_sec` swept 0/30/60/120s). Tries to recover the high/mid gap strata. Requires its own MFE/MAE validation before trusting the PASS signal on this new event class — this would effectively be a new mini R0/R1, not a parameter tweak.
- **Path C** — trade T1_POSTMARKET events the night before, in after-hours. Only helps one of three gap sub-categories (post-market gappers); thin liquidity, overnight gap risk, different trade structure entirely. Probably a separate strategy/project.

**Current lean: Path A.** Narrow the universe explicitly rather than chase the original thesis through an unvalidated entry re-architecture. This is not locked — it's deferred until §12 (data quality) clears, since the sparse-warmup failure counts themselves need to be confirmed clean before they're trusted as the basis for a scope decision.

---

## 12\. Data & Chart Quality — Blocking, Next Phase

Reviewing per-event charts from the corrected R1-Fixed run surfaced dirty tick data in some events (large price spikes/reversions, possible zero/negative prices or sizes, duplicate timestamps, out-of-session ticks, halt-inconsistent gaps, possible further `prev_close` anomalies beyond the one already fixed). Severity and concentration are not yet characterized — could be concentrated in illiquid sub-$1 names, could be spread evenly, unknown until measured.

Separately, the per-event charts from the corrected R1 run don't meet the project standard (`Agent_Prompt_Standard.md` §7) and need regeneration.

**Both block trust in every downstream number** — the R1-Fixed table in §2.1 and the sparse-warmup stratum counts in §11 are both built on this data. **This is the next phase.** See the companion agent prompt, `Phase_DIAG-DQ_Agent_Prompt.md`, for the task breakdown.

---

## 13\. One-Paragraph Current State

EPG-Rapid's mechanics are verified clean: the gate, the T4e/T6b fixes, the time gate, and the halt-aware decay are all confirmed against source. SF-based entry and the ROC gate were both tried and abandoned on real evidence. LULD was dropped from the exit stack — status needs confirming. On the current (possibly-dirty) val sample the strategy trades \~24–25 of \~95 events, all gradual risers, at PF \~1.2–1.8, CVaR5 clearing −15% at the higher thresholds. The strategy may not currently trade its own target event type (dead-tape-to-burst) at all — that's an open scope decision (§11, lean Path A) gated behind a data-quality and chart-standards pass (§12) that has to happen first.