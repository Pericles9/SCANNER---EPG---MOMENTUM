---
tags:
  - type/strategy
  - domain/backtest
  - domain/hawkes
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/proposal
created: 2026-06-18
parent_strategy: epg
strategy_id: epg_rapid
---

# EPG-Rapid — Strategy Outline

**Status:** Proposal — pending backtest validation (see `EPG_Rapid_Test_Phases.md`)
**Strategy id:** `epg_rapid`
**Parent:** `epg` (shares live infrastructure, gate code, exit code)

**Related documents:**
- `LULD_Halt_Architecture.md` — full regulatory mechanics of the rebuilt LULD module
- `Phase_LULD_REBUILD.md` — the rebuild's own agent prompt and original (upper-only) sweep

---

## 1. Thesis

Classic EPG enters on the first EPG `FAIL→PASS` rising edge after a 300s warmup. On a
pre-market dead-to-live gap event, that timing is too slow — by the time the gate opens,
the best part of the move is often gone.

EPG-Rapid keeps the **validated exit machinery** and replaces only the **entry**:

- **Entry:** fire on the **first EPG gate PASS tick at or after `t_scanner_hit_sec`**. No rising-edge requirement, no SF cross-and-hold, no n_hold. **Hard floor: no tick before the scanner hit timestamp is eligible for entry** (enforced by the pre-computed `scanner_hit_catalog.json`). Entering into a live PASS state avoids immediate-exit risk.
- **Exit:** EPG window close only. EXIT_D off. LULD proximity exit abandoned 2026-06-20 — structurally blind to discretionary Straddle-State halts (see `Phase_LULD_V3c.md`).
- **Re-entry:** hard off. One trade per ticker per session.

The target event is the **dead-tape-to-burst transition**: a stock trading on near-zero
volume from 4am, news hits 7–9am ET, volume explodes, price runs 100–200%+ over the next
1–2 hours. The scanner fires at `todaysChangePerc ≥ 30%`.

---

## 2. Locked Decisions

| Decision | Choice | Notes |
|---|---|---|
| Gate | `ParticipationGate` — **original symmetric** | `half_life_seconds=300`, `warmup_seconds=300`. No asymmetric hysteresis, no peak cooling. **Not** SlopeGate. Architecture locked; threshold tuned in R1. |
| Gate threshold | `p_open`, `p_close` — **tuned in Phase R1** | Starting point 0.65/0.65 (original). Swept independently in R1. See §2.1. |
| Gate role | Entry qualifier **and** exit driver | Entry requires gate PASS; exit fires on PASS→FAIL/INACTIVE. |
| EXIT_D | **Disabled** | `exit_d.enabled=false`. Code retained, not evaluated. |
| LULD proximity | **ABANDONED 2026-06-20** | Structurally a Limit-State detector; blind to discretionary Straddle-State halts (V3c recall ceiling 0.31). Not used in EPG-Rapid. See `Phase_LULD_V3c.md`. |
| EPG window close | Enabled | PASS→FAIL or PASS→INACTIVE. Primary exit. Timing determined by gate threshold from R1. |
| Re-entry | Hard off | One entry per ticker per session. `closed_today=True` set at entry, before fill confirmation. |
| Entry qualification | First live EPG PASS tick at or after scanner hit | Hard floor: `scanner_hit_t_sec` from pre-computed catalog. See §3.0. |
| ROC gate | 5-minute ROC | `roc_5m ≥ threshold`. Threshold tuned 5–25% (Phase R3). |
| Scanner heat / quartile | **Void** | Computed and stored as analysis fields only. No gate. Legacy Q3/Q4 filter is not applied. |
| Halt handling | Pause decay clocks across halt gaps | Hawkes EMA and gate `λ_V` do not decay across detected halt windows. See §6. |
| Baseline | Best validated EPG on 100-event val | Same exit stack as EPG-Rapid (LULD prox on, EXIT_D off). See §7. |
| Test split | Untouched | Opened once at the very end of the full pipeline. Not in any phase. |

---

### 2.1 EPG Gate Threshold — The Regime Gate Problem

The `ParticipationGate` threshold (`p_open`, `p_close`) was calibrated at 0.65/0.65 for
the classic EPG stack: rising-edge entry, EXIT_D providing downside protection. In that
context the gate was a **regime detector** — is buy-side intensity high enough to justify
holding a position? EXIT_D owned the exit signal.

**EPG-Rapid changes the gate's role.** EXIT_D is gone. The gate now drives the primary
exit directly via PASS→FAIL. This means the original 0.65 calibration was done under
conditions that no longer apply, and recalibration is warranted.

What the threshold controls in EPG-Rapid:
- `p_open`: how high WJI must be to declare the regime active. Affects entry.
- `p_close`: how low WJI must fall before declaring the regime exhausted. **Directly
  controls exit timing.** With EXIT_D absent, a p_close that's too low lets losers run;
  one that's too high chops out of good trades on noise.

The sweep (Phase R1) starts with **symmetric** arms (`p_open = p_close`), then evaluates
**asymmetric** combinations. Key diagnostic: average PASS→FAIL transitions per trade
(gate chatter) and exit-reason distribution. Cooper selects; agent presents data only.

---

---

## 3. Entry Stack

```
Scanner hit (todaysChangePerc ≥ 30%)
    ↓
[HARD FLOOR] t_scanner_hit_sec from scanner_hit_catalog.json
    │  No tick before this timestamp is eligible for entry.
    │  Events with no scanner hit in catalog → skipped entirely.
    ↓
5-min ROC gate: roc_5m ≥ roc_min
    ↓
Context fetch completes
  └─ Hawkes engine warmed via historical replay (halt-gap-aware)
  └─ EventAnchor + ParticipationGate replayed through history
    ↓
First live EPG gate PASS tick at or after scanner hit → ENTRY
    ↓
closed_today = True   (no re-entry this session)
```

### 3.0 Scanner Hit Floor

**Why it exists:** the rapid runner replays all session trades from 4am to warm the Hawkes
model and EPG gate. The Hawkes anchor fires on the RTH open intensity surge — not on price
momentum. Without an explicit floor, entries can and do fire before the stock reaches the
30% threshold that would trigger the live scanner. In 65.4% of val-split events the anchor
fires before scanner hit (median offset −174s; worst case observed: −2,207s = 37 min early).

**Mechanism:** a pre-computed catalog (`data/filtered/scanner_hit_catalog.json`) records
the first trade where `price ≥ prev_close × 1.30` for each val-split event. In the runner,
this timestamp is loaded in `main()` and passed as `scanner_hit_ts_ns` in the work item.
Inside `_process_event_rapid()`, it is converted to the `t_sec` frame
(`scanner_hit_t_sec = (scanner_hit_ts_ns − timestamps[0]) / 1e9`) and used as the first
guard in the entry loop:

```python
if scanner_hit_t_sec is not None and td.t_sec[i] < scanner_hit_t_sec:
    prev_state = cur
    continue
```

**Edge cases:**

- Event in catalog but `scanner_hit_ts_ns = null` (price never reached 30% in session
  trades) → event **skipped** with reason `no_scanner_hit`. This is the correct live
  behavior — the scanner would never have fired on this name.
- Event not in catalog (e.g., train split, future events) → no floor applied; behavior
  identical to pre-floor runner.
- Scanner hit at the very first trade (pre-market gap-up) → `scanner_hit_t_sec < 0`;
  floor is effectively inactive (all ticks pass).

**Diagnostic fields added by the floor fix:**

- Per-trade: `entry_lag_from_scanner_sec` (seconds from scanner hit to entry)
- Event-level: `gate_at_scanner_hit` (gate state at the scanner hit tick)
- Summary: `mean_entry_lag_from_scanner_sec`, `median_entry_lag_from_scanner_sec`,
  `p90_entry_lag_from_scanner_sec`, `pct_events_gate_pass_at_scanner_hit`

### 3.1 ROC Gate (5-minute)

```
roc_5m = pct_change[t_now] − pct_change[t_now − 5min]
```

- Comparison point is the scanner poll closest to (but at least) 5 minutes before `t_now`.
- If the earliest available poll is < 5 minutes old (early in session / recent first
  appearance), use it as a **partial window** and record the actual lookback used.
- **First appearance = admit.** If no prior poll exists for this ticker, skip the ROC
  check entirely. No special handling for a gapper that dipped below 30% and re-qualified —
  first appearance in the scanner is "first," full stop.
- Gate: `roc_5m ≥ roc_min`. Swept 5–25% (Phase R3); `disabled` included as a baseline arm.

---

## 4. Exit Stack

First exit to fire wins, checked each trade tick.

```
1. LULD proximity (both sides) — RTH only — HALT AVOIDANCE
2. EPG window close — PASS → FAIL / INACTIVE
```

EXIT_D is **not** in the stack.

---

## 5. The LULD Exit — Rebuilt Module, Both Sides

**Important:** EPG-Rapid uses the **rebuilt** LULD module, not the legacy
spread-multiple / continuously-recomputing-reference version. The rebuild (Phase
LULD-REBUILD, design docs `LULD_Halt_Architecture.md` and `Phase_LULD_REBUILD.md`)
replaced `backtest/core/exits/luld_proximity.py` in place. Two structural changes:

1. **Quote-based signal.** The exit watches the National Best Bid (NBB) approaching the
   Upper/Lower Price Band — from `quotes.parquet` `bid_price`/`ask_price` — not trade
   price. This is the actual regulatory Limit State precursor, not a proxy for it.
2. **Sticky reference price.** The real SIP only republishes the reference price when the
   new 5-minute mean moves ≥1% from the current published value. The rebuilt module
   matches this — `reference_update_threshold_pct = 0.01` — instead of recomputing on
   every tick. This was the source of the upper-band late-fire problem (see §5.3,
   resolved) and is now fixed at the module level.

The rebuild's own stated design intent — per the architecture doc — was already
**halt-avoidance, not profit capture**: exit moments before a real halt, not capture a
band-touch as alpha. EPG-Rapid does not need to re-argue that framing; it only needs to
**extend it to both sides**, since the rebuild's first validation pass (Phase
LULD-REBUILD) swept the upper band only (lower band was still locked off in classic EPG
at that time).

### 5.1 What changed and why two-sided is correct here

The project previously locked **lower band disabled** in classic EPG because lower-band
exits *pre-empted EXIT_D* on declining trades (legacy-module luld_lower PF=0.059 — it
fired on trades that EXIT_D would otherwise have handled, and many of those trades
recovered). That rationale is specific to a stack that contains EXIT_D, and specific to
the legacy module's trade-price-based trigger.

**EPG-Rapid disables EXIT_D.** There is nothing for the lower band to pre-empt. So the
lower band is re-enabled on the **rebuilt** module — quote-based, sticky-reference,
halt-avoidance by design on both sides.

> The LULD proximity exit in EPG-Rapid is a **risk-management exit, not an alpha signal.**
> Its job is to get the position flat *before* a LULD halt freezes us in. Being frozen in a
> halted position — unable to exit while the stock is suspended, then gapping on resumption —
> is the specific risk this exit exists to remove. A halt can come from either direction, so
> the exit watches both bands.

### 5.2 The honest cost

The legacy-module luld_lower PF of 0.059 was measured on the **old** trade-price trigger,
not the rebuilt quote-based one — so it isn't a direct read on how the rebuilt module's
lower side will perform. But it's still the right prior: historically, lower-band
proximity fires landed on trades that recovered. In halt-avoidance terms, those are
**false exits** — we left a position that did not actually halt, and gave up PnL to do it.
There's no reason to assume the rebuild eliminates this dynamic on the lower side; it only
fixes the *reference-chasing* failure mode, which was specifically an upper-side problem.

This means:

- The lower side should still be assumed to carry a **higher false-exit rate** than the
  upper side until R4's data says otherwise.
- Upper and lower thresholds must be **tuned independently** (Phase R4). A single symmetric
  threshold is almost certainly wrong.
- The tuning objective is explicitly a **precision/recall tradeoff**, not PF maximization:
  maximize the fraction of real halts we exit ahead of, while minimizing exits that were
  not followed by a halt. Cooper selects the operating point on the frontier; the agent
  presents the frontier only.

### 5.3 Known limitations (must be carried into results)

- **RTH only.** The rebuilt module returns INACTIVE outside 09:30–16:00 ET — this did not
  change in the rebuild. Pre-market halts are exchange-discretion with no standardized
  LULD formula, so this exit does **not** protect pre-market positions. Pre-market halt
  exposure is covered only by the generic "no quote > 30s → soft halt" fallback and is a
  separate, unsolved problem. Given the target event profile is heavily pre-market,
  **the LULD exit protects only the RTH portion of the position lifecycle.** Document the
  pre-market/RTH split of halt exposure in results.
- **Reference-price chasing — RESOLVED by the rebuild.** The legacy module recomputed the
  reference price on every tick, which made the modeled upper band chase price up during
  a momentum run and fire *late* on exactly the parabolic run-ups most likely to halt. The
  rebuilt module's sticky reference price (only updates on a ≥1% move, matching the real
  SIP) directly fixes this. R4's T6 task (reference-chase audit) is retained as a
  **regression check** — confirming the fix holds under two-sided tuning — not as an
  open bug investigation.

---

## 6. Halt-Gap Clock Handling

Detected halt windows must not advance any decay clock.

- **Source of truth:** `detect_luld_halts()` in `luld_halt_detection.py` produces a
  `HaltWindow` list (30s VWAP-band breach + gap detection) from the event's trades.
- **Hawkes EMA / gate `λ_V`:** for any trade pair straddling a halt gap (`dt` exceeds the
  halt-gap threshold), substitute `dt = 0` (or epsilon) in the exponential decay term so the
  intensity and `λ_V` do not collapse across the suspension. This is the principle from
  `HPC_Data_Processing.md §7.3` — pause, don't decay.
- **Setup filter bars:** halt gaps generally fall on bar boundaries. A bar spanning a halt
  boundary is treated as the active portion only; no synthetic bars are inserted across the
  gap.
- **Backtest wiring:** the replay loop (`_hawkes_replay_with_refit`) receives the
  `halt_windows` for the event and applies the `dt=0` substitution at gap crossings.

---

## 7. Baseline Definition

The EPG-Rapid numbers are only meaningful against a baseline run with the **same exit
stack**. The baseline is the best validated classic-EPG config on the 100-event val sample
(seed=42), reconfigured to match EPG-Rapid's exits:

- Gate: `ParticipationGate`, original symmetric (`τ=300`, `p=0.65`, `warmup=300`).
- EXIT_D: off. LULD proximity: on, **both sides**, at a fixed reference threshold (the R3
  starting point). EPG window close: on.
- Entry: **classic rising-edge ONLY** — no `entry_eligible()` call, zero SF involvement.
  This is the thing EPG-Rapid replaces. (§1.1: bolting any SF gate onto the classic first
  entry is the known EPG-OPT2-SF failure mode. The baseline must be clean of it.)

Reported deltas: PF, trade count, mean entry lag (t_entry − t_scanner_hit), CVaR5,
exit-reason distribution.

> Note: the live system currently runs SlopeGate F_ss heuristically and is **not** the
> backtest baseline. The baseline here is a backtest-validated ParticipationGate config, so
> the comparison is gate-consistent.

---

## 8. Code Modularity Changes (backwards-compatible)

All changes are additive or default-preserving. Where a signature changes, every caller is
updated in the same pass.

| Change | File(s) | Compatibility |
|---|---|---|
| `LuldProximityExit` — expose independent upper/lower thresholds | `core/exits/luld_proximity.py` (**the rebuilt, quote-based, sticky-reference module** — see §5) | Add `proximity_threshold_upper` / `proximity_threshold_lower` (fraction of price, NBB-to-band, sticky reference); default both to the rebuild's existing single `proximity_threshold`. Add `lower_enabled: bool` config flag. |
| Halt-gap `dt=0` substitution in replay | `backtest/runner.py` (`_hawkes_replay_with_refit`) | Gated on presence of `halt_windows`; absent → current behavior. |
| ROC 5-min buffer | scanner monitor (live) + backtest snapshot reader | New per-ticker rolling buffer of `(ts, pct_change)`. Additive. |
| Rapid runner | `runner_rapid.py` or `--mode rapid` flag on existing runner | Classic path untouched behind the flag. |

> **Module identity note:** the LULD exit module referenced throughout this document is
> the **rebuilt** version (Phase LULD-REBUILD). Its trigger parameter is
> `proximity_threshold` — a fraction of price representing distance from the relevant
> band, evaluated against the sticky reference price and the NBB — **not** the legacy
> module's `n_spread_multiple` (spread-multiple buffer on trade price). A fixed
> fraction-of-band threshold is the right parameterization here: the regulatory LULD band
> itself is already a fixed percentage of the reference price (10%/20% Tier 2 schedule),
> so a proximity-to-band threshold expressed the same way is the natural fit — the
> spread-multiple approach was a workaround for the legacy module's trade-price/staleness
> problem, which the sticky-reference fix now addresses directly.

---

## 9. Open Decisions (need a call before R4)

| # | Decision | Status |
|---|---|---|
| 1 | LULD reference implementation for tuning | **Resolved.** Rebuild has landed — R4 tunes the rebuilt quote-based, sticky-reference module directly. Not module-agnostic/conditional anymore. |
| 2 | LULD trigger parameterization | **Resolved.** Rebuilt module uses `proximity_threshold` (fraction of price, NBB-to-band, sticky reference) — not spread-multiple. R4 sweeps this parameter independently per side. |
| 3 | Upper-band fate under reference-chasing bug | **Resolved.** Bug fixed by the rebuild (sticky reference). R4's T6 is now a regression check, not an open investigation. |
| 4 | Position-size scope for false-exit cost | Open. RTH-only notional vs full — report both; sizing is a paper-trade concern, not a backtest one. |

---

## 10. Config Skeleton

```json
{
  "strategy_id": "epg_rapid",
  "gate": {
    "type": "participation",
    "half_life_seconds": 300,
    "p_open": 0.65,
    "p_close": 0.65,
    "warmup_seconds": 300
  },
  "entry": {
    "mode": "rapid",
    "require_epg_pass": true,
    "roc_min_5m": 0.05
  },
  "reentry": { "enabled": false },
  "context_fetch": { "max_ticks": 5000 },
  "exit": {
    "exit_d": { "enabled": false },
    "luld": {
      "enabled": true,
      "rth_only": true,
      "upper_enabled": true,
      "lower_enabled": true,
      "proximity_threshold_upper": 0.02,
      "proximity_threshold_lower": 0.02,
      "reference_update_threshold_pct": 0.01,
      "reference_window_sec": 300.0
    },
    "epg_window_close": { "enabled": true }
  },
  "halt": { "gap_seconds": 60, "pause_decay": true }
}
```

All values above are **starting points**, not selected. `p_open`/`p_close` selected in R1;
`roc_min_5m` in R3; `proximity_threshold_upper`/`_lower` in R4.
`reference_update_threshold_pct=0.01` is fixed (matches the real SIP rule, not a tuning target).
