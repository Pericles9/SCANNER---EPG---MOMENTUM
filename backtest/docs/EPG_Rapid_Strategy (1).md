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

- **Entry:** fire on the **first tick at or after `t_scanner_hit`** where `gate.state == PASS`. The scanner hit timestamp is a hard floor — no tick before it is eligible regardless of gate state. In the majority of events the gate is already PASS at scanner hit (warmed on pre-event history), so entry fires within seconds. No rising-edge requirement. No SF involvement of any kind.
- **Exit:** EPG window close (primary). EXIT_D off. LULD proximity (rebuilt module, both sides, RTH only) active — halt avoidance. Tuned in R4. See §5.
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
| LULD proximity | **Disabled** | Not in EPG-Rapid exit stack. `LuldProximityExit` retained unchanged for classic EPG runner only. |
| EPG window close | Enabled | PASS→FAIL or PASS→INACTIVE. Primary exit. Timing determined by gate threshold from R1. |
| Re-entry | Hard off | One entry per ticker per session. `closed_today=True` set at entry, before fill confirmation. |
| Entry qualification | First PASS tick at or after `t_scanner_hit` | Hard scanner hit floor checked before gate state. No SF, no rising-edge, no `n_hold`. |
| ROC gate | 5-minute ROC | `roc_5m ≥ threshold`. Threshold tuned 5–25% (Phase R3). |
| Scanner heat / quartile | **Void** | Computed and stored as analysis fields only. No gate. Legacy Q3/Q4 filter is not applied. |
| Halt handling | Pause decay clocks across halt gaps | Hawkes EMA and gate `λ_V` do not decay across detected halt windows. See §6. |
| Baseline | Classic EPG on MDR≥150 diagnostic sample (first-PASS, same exit stack) | Scanner floor active. See §7. |
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

`max_entry_lag_sec` (Phase R1 starting point 180s): maximum allowed seconds from scanner
hit to entry. Applied per-event as a hard filter in R1 — events where
`t_entry − t_scanner_hit > max_entry_lag_sec` are excluded from scoring. R0 logs the lag
distribution; Cooper sets the value before R1 begins.

---

---

## 3. Entry Stack

```
Scanner hit  (todaysChangePerc ≥ 30%; pre-computed in scanner_hit_catalog.json)
    ↓
ROC gate  roc_5m = pct_change_now − pct_change_5min_ago ≥ roc_min
    ↓
Context fetch + historical replay
    ├─ Hawkes engine: warmed from 4am, halt-gap dt=0 substitution active
    └─ ParticipationGate: replayed through full pre-event history
    ↓
Entry loop — first tick at or after t_scanner_hit where BOTH conditions hold:
    ├─ [FLOOR]  t_sec ≥ t_scanner_hit          (hard floor — always checked first)
    └─ [GATE]   gate.state == PASS
    ↓
ENTRY  →  closed_today = True  (no re-entry)
```

*Note:* ROC gate checked on the pre-computed snapshot, before the replay. Scanner hit floor (`t_sec ≥ t_scanner_hit`) is always the first guard inside the entry loop; context fetch does not depend on it.

### 3.0 Scanner Hit Floor

`t_scanner_hit_sec` is the timestamp when `todaysChangePerc ≥ 30%` was first satisfied,
read from `scanner_hit_catalog.json`. Events with no confirmed scanner hit in the catalog
are skipped (`reason=no_scanner_hit`).

The gate is replayed through all pre-event history from 4:00am, so it is already warm —
and often in PASS state — at the scanner hit tick. In 41.8% of val events the gate was in
PASS at scanner hit; entry fired within seconds.

The floor guard is always the first check in the entry loop. No gate-state condition is
evaluated for ticks before `t_scanner_hit_sec`.

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

EPG-Rapid numbers are only meaningful against a baseline run with the **same exit stack**.
The baseline used throughout R0–R4 is classic-EPG first-PASS entry on the **MDR≥150
diagnostic sample** (100 events, randomly selected from events where `mom_pct ≥ 150` and
`t_scanner_hit_sec IS NOT NULL`, not top-ranked), configured to match EPG-Rapid exits.

- Gate: `ParticipationGate`, original symmetric (`τ=300`, `p=0.65`, `warmup=300`).
- EXIT_D: off. LULD: off. EPG window close: on.
- Entry: **classic first-PASS** — first tick where `gate.state == PASS` at or after
  `t_scanner_hit`. Scanner floor active. No SF, no rising-edge, no `n_hold`.

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
    "scanner_floor": true,
    "max_entry_lag_sec": null,
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
`max_entry_lag_sec` set by Cooper after R0 T7 distribution; `null` in R0 (log only).
`reference_update_threshold_pct=0.01` is fixed (matches the real SIP rule, not a tuning target).
