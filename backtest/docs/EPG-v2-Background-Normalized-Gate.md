---
tags:
  - type/design-spec
  - domain/epg
  - project/scanner-epg-momentum
  - status/draft
created: 2026-06-07
supersedes: peak-normalized ParticipationGate (core/epg/gate.py); Phase WJI-Decay volume-only floor
---

# EPG v2 — Background-Floored Decaying-Peak WJI Gate

**Status:** Draft for review. Keeps the EPG pct-of-peak threshold but makes it decay and floors it at a background-derived WJI level. Supersedes the static peak-normalized gate and corrects the volume-only floor from the pushed Phase WJI-Decay prompt.

---

## 1. The Problem With Static Peak Normalization

The current gate sets `threshold = p x running_peak`, where `running_peak = max(lambda_V[T_event : t])` is monotonic. An early burst — opening print, halt release — sets a high peak that anchors the denominator for the rest of the event. Sustained-but-lower participation later reads as FAIL even when it is a valid continuation regime. The peak is a one-way ratchet anchored to a single moment.

Two things are wrong, and they need two different fixes:

1. **The peak never relaxes.** Fix: let it decay, so old spikes are forgotten over time.
2. **A decaying peak alone would eventually fall to zero**, making the gate trivially PASS. Fix: floor the decay at a level derived from the stock's actual background — not at zero, and not at a stale constant.

---

## 2. Core Idea

**Keep the normal EPG pct-of-peak threshold. Make the peak decay. Floor the threshold at `C x WJI_background`.**

```
threshold(t) = max( p x peak_decayed(t),  C x WJI_background(t) )
```

- Early in the event, `p x peak_decayed` is high and dominates — the gate judges participation **relative to its own recent peak**, as today.
- As the peak decays with no new highs, `p x peak_decayed` falls. Once it drops below `C x WJI_background`, the floor takes over and the gate judges participation **relative to the stock's background**.
- `tau_peak` controls *when* that handover happens — how fast the gate relaxes from "near your peak?" to "still above background?"

The threshold literally **decays toward `C x WJI_background`**: with no new WJI highs, `peak_decayed -> 0`, so `p x peak_decayed -> 0`, and `threshold -> C x WJI_background`.

The floor must be in the **same units as the signal and the peak** for the `max()` to be valid. That is the entire reason `WJI_background` is the WJI formula evaluated on the fitted Hawkes background intensity (section 3): same formula, same units, valid comparison.

---

## 3. Mathematical Specification

### 3.1 Participation intensity (unchanged)

```
lambda_V(t_i) = lambda_V(t_{i-1}) * exp(-ln2 * dt / tau) + dv_i * (ln2 / tau)
```
`dv_i = price_i x size_i`. Units: **dollars/sec**. `tau = 300s`. Existing EMA, unchanged.

### 3.2 Background intensity (new, tracked via refit)

```
background_V(t) = ( mu_buy(t) + mu_sell(t) ) x dbar(t)
```
- `mu_buy(t) + mu_sell(t)` — fitted Hawkes background arrival rate (trades/sec), **updated on every online refit** (every 50 trades). The same baseline `EventAnchor` uses, but tracked over time rather than frozen at cold-start.
- `dbar(t)` — average dollar-per-trade (dollars/trade). Source is an open decision (6.4).
- Product is **dollars/sec — same units as `lambda_V`.**

Causal and live-computable: the live stack already refits `mu` and can maintain `dbar` from the same trade stream.

### 3.3 Volume ratio (background-normalized — the "second form")

```
volume_ratio(t) = lambda_V(t) / background_V(t)
```
At pure background, `lambda_V -> background_V` and `volume_ratio -> 1`. During a live regime it is `>> 1`.

### 3.4 WJI composite

WJI is the geometric mean of a **volume term** and a **buy-side-flow term**:

```
WJI(t) = sqrt( g(volume_ratio(t)) * buy_term(t) )
```

The **same formula evaluated on the background state** gives the reference. Because `volume_ratio = 1` at background, the volume contribution collapses to the constant `g(1)`:

```
WJI_background(t) = sqrt( g(1) * buy_term_background(t) )
```

`WJI_background(t)` is in WJI units by construction, drifts only through the refit (via `buy_term_background`), and is the quantity the threshold floors against.

The forms of `g(.)` and `buy_term` are **open decisions — 6.1 and 6.2.** Everything else here is locked.

### 3.5 Decaying peak

The running peak is now of **WJI**, and it decays:

```
peak_WJI(t) = max( WJI(t),  peak_WJI(t-) * exp(-dt / tau_peak) )
```
New highs ratchet up instantly (the `max` with current `WJI`); in the absence of new highs the peak decays at half-life `tau_peak`. This replaces the monotonic peak in the current gate.

### 3.6 Threshold and PASS condition

```
threshold(t) = max( p x peak_WJI(t),  C x WJI_background(t) )
PASS  when  WJI(t) >= threshold(t)
FAIL  otherwise
```

- `p` — pct-of-peak (retain the calibrated `0.65` as default). Governs the peak-relative regime.
- `C` — dimensionless floor multiplier, **C > 1**, set by Cooper via the diagnostic charts. Governs the background-relative regime. At pure background `WJI(live) = WJI_background`, so `C > 1` requires participation to be elevated above background to hold PASS.
- `tau_peak` — peak decay half-life. Sets the crossover between the two regimes. Calibrated by sweep (6.3).

### 3.7 State machine (unchanged)

`INACTIVE -> WARMUP -> {PASS, FAIL}`. WARMUP (first 300s after `T_event`) is retained: the refit is still settling toward a stable `mu` during early price discovery, so `background_V` is not yet trustworthy. Only the PASS/FAIL test (3.6) changes.

---

## 4. Why This Is Better Live, Not Just In Backtest

- **The peak handoff hazard is reduced, not eliminated.** A peak still exists, so the gate object still carries it across the REST->WS boundary — but because it now decays and is floored against a recomputable background, a small reconstruction error self-corrects within a few `tau_peak` rather than persisting for the whole session. `background_V(t)` itself needs no reconstruction (recomputed from running refit `mu` + `dbar`).
- **Self-calibrating per name.** A thick, active stock has a high `background_V`; a thin mover has a low one. `volume_ratio` and the floor are scale-free across the universe, so a single `C` generalizes without per-tier tuning.
- **The floor means the same thing at minute 5 and minute 300.** "C above background" is stationary in meaning; "near the peak" is not. The decay plus floor gives both: peak-relative judgment while the regime is hot, background-relative judgment once it cools.

---

## 5. What Changes vs. Prior Designs

**vs. the current Phase F gate:**

| Aspect | Current | EPG v2 |
|---|---|---|
| Signal | `lambda_V` (dollars/sec) | `WJI` (background-normalized volume term x buy term) |
| Peak | Monotonic, never decreases | Decays at `tau_peak` |
| Threshold | `p x peak` (no floor) | `max(p x peak_decayed, C x WJI_background)` |
| Denominator of "normal" | The event's own high-water mark | The fitted Hawkes background |

**vs. the pushed Phase WJI-Decay prompt** — the decaying-peak machinery is **retained**; two things are corrected:

| Aspect | Pushed prompt | EPG v2 |
|---|---|---|
| Floor quantity | `C x lambda_ref x dbar` (volume only, `lambda_V` units) | `C x WJI_background` (full composite, WJI units) |
| Floor applied to | the peak (`peak_eff = max(decayed, floor)`, then `p x peak_eff`) | the **threshold** (`max(p x peak_decayed, C x WJI_background)`) |
| Signal | `lambda_V` | `WJI` |

The shift of the floor from the peak to the threshold is deliberate: the floor is a hard minimum on the PASS bar itself (`C x WJI_background`), not a `p`-discounted minimum. It is exactly the level the threshold decays to.

---

## 6. Open Decisions

### 6.1 Volume term `g(volume_ratio)`

| Option | Form | Tradeoff |
|---|---|---|
| Raw | `g(x) = x` | Most responsive; a 50x burst dominates the geometric mean |
| Capped | `g(x) = min(x, x_cap)` | Matches the setup filter's `min(.,1)` precedent; bounds burst dominance |
| Log | `g(x) = max(ln(x), 0)` or `1 + ln(x)` | Smoothest; may under-weight genuine surges |

Default lean: capped, given precedent and the burst-dominance risk. Note `g(1)` feeds `WJI_background` directly, so the choice sets the floor's volume contribution too.

### 6.2 Buy-side-flow term `buy_term`

- **Live:** `I_buy(t) = lambda_buy / (lambda_buy + lambda_sell)` — already computed (Panel 3 of the standard charts).
- **Background:** `mu_buy(t) / (mu_buy(t) + mu_sell(t))` — background directional split from the fit.

Open: raw `[0,1]`, or re-centered (e.g. `max(2*I_buy - 1, 0)` so a 50/50 book contributes nothing and only buy-dominance scores)? Raw keeps a neutral book at 0.5; re-centered makes the gate explicitly reward buy conviction.

### 6.3 `tau_peak` (peak decay half-life)

Calibrated by sweep on the 100-event val sample. Selection criterion: favor the **largest** `tau_peak` whose val PF is within noise of the best (slower decay = more memory, less overfit), consistent with prior phase discipline.

### 6.4 `dbar(t)` source

| Option | Tradeoff |
|---|---|
| Refit-window mean | Consistent with `mu` (same 10k window); steps at each refit. Natural default. |
| Slow EMA | Smooth; risks inflating during a large-trade burst |
| Cold-start frozen | Simplest; goes stale on long events (the failure we are removing) |

Subtlety: `mu` is the zero-excitation background rate, while `dbar` over any window includes burst-sized trades. So `background_V` is "dollars/sec at the background arrival rate with current average trade size," not purely background. Acceptable, but a deliberate reading — flag if a purer background `dbar` is wanted.

### 6.5 `C` multiplier

Set by Cooper from the diagnostic charts. `C > 1`. No auto-selection.

### 6.6 Thin-name guard

When `background_V` is near zero (very thin pre-market names), `volume_ratio` explodes and the gate trivially PASSes. Needs an explicit guard: a minimum `background_V` floor, or reliance on the upstream setup filter to exclude such names before the gate sees them.

---

## 7. Migration & Backward Compatibility

- Add `gate_mode: "peak" | "background"` to config, default `peak`, so the Phase F pipeline is untouched until v2 is validated.
- Validate `background` A/B against `peak` on the 100-event val sample (seed=42), then full val, against the Phase F baseline. Val-only; test split stays locked.
- Interface change: `update()` must receive `mu_buy(t)`, `mu_sell(t)`, `lambda_buy(t)`, `lambda_sell(t)`, and `dbar(t)` (currently only `dollar_vol`, `timestamp`). Both the backtest runner and live `LiveSignalState` must pass the running refit state in.

---

## 8. Diagnostic Chart (for choosing C and reading tau_peak)

Per event, 2 panels, shared x (seconds from session start):

- **Panel 1 — Price:** 1-minute candlesticks.
- **Panel 2 — WJI:** the live `WJI(t)` line; the decaying `p x peak_WJI(t)` line; the `WJI_background(t)` reference (tracking, **not flat** — drifts with the refit); and candidate `C x WJI_background` floor lines in distinct colors. The effective threshold is the upper envelope of the decaying-peak line and the chosen floor.

Eyeball target for `C`: the floor should sit below sustained-regime WJI plateaus and above the post-regime decay back toward background — and at a level where, once the peak has decayed past it, the handover to background judgment happens where you'd want the regime called over.

---

## 9. Open Questions Summary

1. `g(.)` — raw / capped / log? (6.1)
2. `buy_term` — raw `[0,1]` or re-centered? (6.2)
3. `tau_peak` — sweep range and default? (6.3)
4. `dbar(t)` — refit-window mean / slow EMA / frozen? (6.4)
5. Thin-name guard — `background_V` floor or upstream exclusion? (6.6)

Once 1-5 are pinned, the gate implementation and the Phase WJI-Decay rewrite are mechanical.
