---
tags:
  - type/strategy
  - domain/backtest
  - domain/hawkes
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/wip
created: 2026-05-04
---

# Scanner × EPG × LULD Momentum Strategy

## Overview

Simplified momentum strategy derived from the full Scanner × Hawkes × OFI Impact project
(`hawkes-ofi-impact`). Removes the OFI normalization, Gate 3 (burst magnitude), dynamic stop,
and regime stack. Retains the core participation-gated entry and time-to-exhaustion exit logic.

**Thesis:** Extended-hours momentum stocks (≥30% intraday gap, setup filter PASS) have
predictable participation windows (EPG) where price continues in the direction of the gap.
The Hawkes intensity imbalance (EXIT_D) provides an early exhaustion signal that captures
more of the move than waiting for EPG close.

---

## Entry Stack

### Step 1 — Setup Filter

Four-signal composite filter run on 1-minute OHLCV bars from session open to event time.

| Signal | Condition |
|--------|-----------|
| Range | Day's range (H-L)/VWAP ≥ floor |
| Volume | Dollar volume ≥ floor |
| Thinness | Spread/mid ≤ ceiling |
| Conviction | Body fraction of range ≥ floor |

Composite score Q̃(t) ∈ [0,1]. PASS if Q̃(t) > threshold.

**Source:** `core/filters/setup_filter.py`
**Calibration:** Phase F0 (not yet re-run in this project — using parent calibration)

### Step 2 — EPG Rising Edge

**EventAnchor:** Detects when cumulative dollar volume since session open crosses the
k × λ_ref threshold. Fires exactly once per event, setting t_event.

- `k = 5` — crossing at ~100% rate, fires ~30s before scanner trigger
- `λ_ref` = mu_buy + mu_sell from cold-start MLE fit (pure background rate, not equilibrium)

**ParticipationGate:** Tracks λ_V (dollar volume arrival rate, decaying EMA with half-life τ).
- `τ = 300s` — 5-minute half-life
- `p = 0.65` — PASS when λ_V ≥ p × running_peak
- `warmup = 300s` — gate inactive during first 5 minutes after t_event

State machine: INACTIVE → WARMUP → {PASS, FAIL} (cycles until event ends)

**Rising edge condition:** `cur == PASS and prev_state in (INACTIVE, WARMUP, FAIL)`

Note: after exiting a trade mid-PASS window, `prev_state = PASS` → next tick is NOT a
rising edge. Maximum one trade per PASS window by design.

### Step 3 — Gap Gate (backtest only)

`intraday_pct ≥ 30%` at entry time. If gap < 30% at rising edge, queue the entry and
re-check each subsequent PASS tick. Cancel if PASS window closes without reaching 30%.

---

## Exit Stack (first wins, checked each trade tick)

### EXIT_D — Hawkes Intensity Imbalance Timer

```
I(t) = λ_sell(t) / (λ_buy(t) + λ_sell(t))
```

Timer starts when I(t) > theta. Resets when I(t) ≤ theta.
Fires when timer has been running continuously for ≥ tau_min seconds.

**Disabled if I_entry > theta** (already sell-dominant at entry — EXIT_D would fire immediately).

**Calibrated params (T10 sweep on 100-event val seed=42):**
- `theta = 0.65`, `tau_min = 4.0s` → PF=1.3848, n_exit_d=134 fires
- Phase U default: `theta=0.75`, `tau_min=8.0s` → PF=1.0962

**Source:** inline in `backtest/runner.py`

### LULD Proximity Exit

Price within `proximity_pct_threshold`% of the Tier 2 LULD band (reference price ± band_pct).
RTH only (09:30–16:00 ET). Warmup 60s after RTH open.

- `proximity_pct_threshold = 2.0%`
- `ref_window_sec = 300.0s` — rolling 5-min reference price window

Phase U: 16 fires, mean PnL = -5.97%, PF ≈ 0 → fire behavior needs investigation.

**Source:** `core/exits/luld_proximity.py`

### EPG Window Close Exit

Exit when EPG transitions PASS → FAIL or PASS → INACTIVE.
Phase S baseline: 100% of exits via this mechanism (before EXIT_D+LULD added).

---

## Hawkes Engine

**Model:** Univariate K=1 with fixed beta.

```
λ_buy(t) = μ_buy + α_buy_self × Σ exp(-β(t-t_i))     for buy events t_i < t
λ_sell(t) = μ_sell + α_sell_self × Σ exp(-β(t-t_i))   for sell events t_i < t
```

Parameters:
- `β = 0.1` (fixed, not MLE-fitted — MLE-optimal β makes EXIT_D degenerate)
- `ρ = 0.99` (forgetting weight for compensator in MLE)
- Online refit every 50 trades on sliding window of 10,000 trades
- Cold-start: first 1,000 trades, 5 restarts

**n_base** = (α_buy_self + α_sell_self) / β per trade, updated at each refit.
Median across val events: ~0.154 (Phase A iter 7).

---

## Parameters

| Parameter | Value | Source |
|-----------|-------|--------|
| EPG k | 5 | Phase R research |
| EPG τ | 300s | Phase R research |
| EPG p | 0.65 | Phase R research |
| EPG warmup | 300s | Phase R research |
| Hawkes β | 0.1 | Fixed design constant (Phase A) |
| Hawkes ρ | 0.99 | Phase A iter 7 |
| Refit interval | 50 trades | Phase A |
| Cold-start size | 1,000 trades | Phase A |
| EXIT_D theta | 0.65 | T10 sweep best combo |
| EXIT_D tau_min | 4.0s | T10 sweep best combo |
| LULD proximity | 2.0% | Phase T |
| Gap gate | 30% | Phase S spec |

---

## Baseline Results (from parent project)

### Phase S — Screening Only (no EXIT_D, no LULD)

- 100-event val seed=42, 81 events traded
- PF=1.2709, n_trades=345, win=43.5%, mean_pnl=+0.38%, mean_hold=312s
- 100% exits via EPG window close
- Pre-market PF=1.73, Regular PF=1.16
- Gap gate blocked 64.5% of 971 PASS edges

### Phase U — EXIT_D + LULD (theta=0.75, tau_min=8s)

- Same 100-event sample
- PF=1.0962, n_trades=385, win=43.1%, mean_pnl=+0.133%
- EXIT_D: 113 fires, PF=1.79
- LULD: 16 fires, PF≈0, mean=-5.97%
- Pre-market regressed: PF 1.73→0.90

### T10 Best Combo (theta=0.65, tau_min=4s)

- PF=1.3848, 134 EXIT_D fires (same 81 events)

---

## Known Limitations

1. **LULD fires are destructive.** 16 fires with mean -5.97% — the proximity exit may be
   exiting positions just before a halt that doesn't materialize, or in situations where
   price recovers immediately after the band proximity.

2. **Pre-market regression with EXIT_D.** Likely EXIT_D fires prematurely in thin pre-market
   order flow where intensity imbalance signals are noisier.

3. **Gap gate queues reduce effective sample.** 40 queued entries out of 971 PASS edges —
   quality of queued entries vs immediate entries not yet analyzed.

4. **T10 best combo not yet validated on full val.** Only tested on 100-event stratified sample.

5. **Setup filter using parent calibration.** Phase F0 has not been re-run for this project.
   The filter params may not be optimally calibrated for the exact data split used here.

---

## Branch Lineage

This strategy was branched from `hawkes-ofi-impact` after Phase U. The branch point and
what was retained vs removed:

| Source phase | What was inherited |
|-------------|-------------------|
| [[Phase_S_Results]] | EPG entry stack (k=5, τ=300s, p=0.65), gap gate (30%), Setup Filter |
| [[Phase_T_Results]] | `LuldProximityExit` module, EXIT_D simulation infrastructure |
| [[Phase_U_Results]] | EXIT_D + LULD integrated runner; T10 best combo θ=0.65, τ=4s adopted as default |

**Removed from parent:** OFI normalization, Gate 3 (burst magnitude), dynamic stop (EXIT_2),
regime stack, OFI directional gate, sell_ratio gate. All calibration phases A–R of the
parent project remain in `hawkes-ofi-impact/`.

## Related

- Parent strategy spec: [[Scanner-Hawkes-OFI Impact]]
- EPG research: [[research_summary]]
- Phase A (this project): [[scanner-epg-momentum/results/phase_a/Phase A Results|Phase A Results]]
- Project directory: [[scanner-epg-momentum/docs/Project_Directory|Project Directory]]
