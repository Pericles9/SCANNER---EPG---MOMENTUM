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

**Thesis:** Extended-hours momentum stocks (≥30% intraday gap) have predictable participation
windows (EPG) where price continues in the direction of the gap. The Hawkes intensity
imbalance (EXIT_D) provides an early exhaustion signal. The setup filter is computed at each
tick but does NOT gate first entry — Phase EPG-OPT2-SF showed the filter blocks the
early-impulse entries that carry this strategy's alpha (mean delta_pf = −0.085 when added).

---

## Entry Stack

### Step 1 — EPG Rising Edge

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

### Step 2 — Gap Gate (backtest only)

`intraday_pct ≥ 30%` at entry time. If gap < 30% at rising edge, queue the entry and
re-check each subsequent PASS tick. Cancel if PASS window closes without reaching 30%.

### Setup Filter — Re-entry gate and continuous disqualifier (not an initial entry gate)

Four-signal composite run on 1-minute OHLCV bars.

| Signal | Condition |
|--------|-----------|
| Range | Day's range (H-L)/VWAP ≥ floor |
| Volume | Dollar volume ≥ floor |
| Thinness | Spread/mid ≤ ceiling |
| Conviction | Body fraction of range ≥ floor |

Composite score Q̃(t) ∈ [0,1]. PASS if Q̃(t) > threshold.

**Roles:**
- **NOT an initial entry gate.** Computed at every tick but does not block first entry.
- **Re-entry gate (backtest):** After EXIT_D fires, SF must be passing before re-entry.
- **Continuous disqualifier (live only):** Q̃ < 0.65 for 15 consecutive bars → disqualify ticker.

**Source:** `core/filters/setup_filter.py`

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

**Current status: disabled** (`exit_d.enabled = false` in `config/strategy.json`). Code retained.
In live, EXIT_D was disabled with the SlopeGate swap (2026-06-03); sole exit = SlopeGate PASS→FAIL.

**Source:** inline in `backtest/runner.py`

### LULD Proximity Exit

Price within `proximity_pct_threshold`% of the Tier 2 LULD band (reference price ± band_pct).
RTH only (09:30–16:00 ET). Warmup 60s after RTH open.

- `proximity_pct_threshold = 2.0%`
- `ref_window_sec = 300.0s` — rolling 5-min reference price window

Phase F finding: **upper band only** (lower band disabled). luld_upper PF=13.47 (val-sample),
17.53 (val-full), 11.73 (test). luld_lower PF=0.059 at all N — destroys value by pre-empting
EXIT_D on declining trades. Lower band is disabled in current config.

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
| Gap gate | 30% (Phase B) / disabled (Phase C) | Phase S spec; removed Phase C |
| CVD filter | disabled (fixed PF=1.7544) | Phase C.5: watermark 5% now best single filter |

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

### Phase B — EXIT_D (T10 best) + Re-Entry (this project)

- 100-event val seed=42, 81 events traded
- PF=1.3825, n_trades=1,689 (385 first + 1,304 re-entries), win=45.2%, mean_pnl=+0.294%
- EXIT_D: 1,350 fires (PF=1.571), LULD: 27 fires (PF=0.044), EPG close: 312
- Pre-market PF=1.395 (recovered from Phase A PF=1.009)
- Gap gate blocked 60.4% of 971 PASS edges

### Phase C — Gap Gate Removed, Backside Filters (this project)

- Same 100-event val seed=42 sample, gap gate disabled
- No-filter baseline: PF=1.7391, n=3,588 (gap gate off, no filter)
- Watermark 5% (Phase C winner): PF=1.9443, n=1,945, 572 blocked
- **CVD filter (FIXED):** PF=1.7544, n=2,214, win=45.93%, mean_pnl=+0.584%, 385 blocked
- CVD filter (BUGGY, INVALID): PF=2.0328, n=1,145 — buggy accumulator mapped ambiguous trades to sells; do not use
- Combined A+C: PF=2.336, n=677 (thin sample; not recommended for deployment)
- **Bias note:** Gap gate removal introduces look-ahead vs Phase B — Phase C PF is not
  directly comparable to Phase B. Watermark 5% requires holdout validation (Phase D).

### Phase D — Intra-Window Rolling High Watermark (this project)

- Same 100-event val seed=42 sample, gap gate disabled
- Replaces Phase C global watermark (anchored to T_event high) with per-window rolling high
- Cross-window memory: `prior_window_peak` carries closed window peak into new window open
- Re-entry blocking: Phase D also blocks re-entries when intra-window drawdown > threshold

| Threshold | PF | n_trades | win% | entries_blocked% |
| --------- | ---- | ------- | ---- | --------------- |
| 2% | **2.6529** | 483 | 49.28% | 77.3% |
| 3% | 2.576 | 667 | 48.73% | 72.7% |
| 5% | 2.2155 | 1,213 | 47.16% | 64.0% |
| 7% | 2.1651 | 1,589 | 46.82% | 54.2% |

**Best: 2%** (PF=2.6529, n=483). PF > 2.5 escalation flagged and user-acknowledged.
Re-entry validation: blocked re-entries have 5.72x higher drawdown than allowed.

**Config:** `config/phase_d.json` | **Results:** `results/phase_d/` | **Docs:** [[Phase_D_Results]]

### Phase E — Symmetric Spread-Multiple LULD Exit (this project)

- Same 100-event val seed=42 sample, EXIT_D theta=0.65/tau=4s, watermark 2%, gap gate disabled
- Replaces fixed 2% proximity threshold with N x bid-ask spread buffer on BOTH upper and lower bands
- Trigger: `price < lower_band + N*spread` OR `price > upper_band - N*spread`
- Fallback: if bid/ask invalid, buffer=0 (fires only at band); 266,955 fallback ticks across all N

| N | PF | n_trades | win% | mean_pnl% | luld_lo | luld_hi | luld_lo_pf | luld_hi_pf | exit_d_pf |
|---|-----|---------|------|-----------|---------|---------|-----------|-----------|-----------|
| Phase D (2% fixed, lower only) | **2.6529** | 483 | 49.28 | +1.491% | 13 | 0 | 0.71 | — | 3.074 |
| 1 | 1.9271 | 476 | 48.32 | +0.838% | 20 | 47 | 0.059 | 12.571 | 2.099 |
| 2 | 1.7509 | 467 | 47.11 | +0.638% | 33 | 71 | 0.274 | 4.744 | 1.927 |
| 3 | 1.7901 | 449 | 46.77 | +0.636% | 48 | 81 | 0.430 | 8.089 | 1.843 |

**Best N: 1** (PF=1.9271). **Escalation TRIGGERED: best PF=1.9271 < 2.20 hard stop.**

Key findings:
- `luld_upper` PF=12.57 at N=1 — stocks running toward upper LULD are massive momentum winners; upper-band exit is genuine Phase E contribution (Phase D had zero upper-band exits)
- `luld_lower` PF=0.059 at N=1 — pre-empts EXIT_D on its best declining trades; EXIT_D PF drops 3.074→2.099; lower-band exit destroys value at all N
- Net effect of symmetric LULD: PF regression vs Phase D (2.6529→1.9271, -27%); phase failed its primary success criterion

**Config:** `config/phase_e.json` | **Results:** `results/phase_e/` | **Docs:** [[Phase_E_Results]]

### Phase F — Asymmetric LULD (upper band only)

- Same 100-event val seed=42 sample + full val (1,228 events) + test sample (100 events)
- EXIT_D theta=0.65/tau=4s, watermark 2%, gap gate disabled, luld_lower disabled

| Split | PF | n_trades | win% | luld_upper PF | luld_upper mean% |
|-------|----|----------|------|---------------|-----------------|
| val-sample | 2.2976 | 476 | 50.63% | 13.47 | +4.204% |
| val-full | 1.9194 | 6,004 | 48.57% | **17.53** | +4.633% |
| test-sample | **2.1849** | 611 | 48.12% | 11.73 | +4.152% |

Phase D baseline exceeded on test. Below Phase D on val-full. epg_window_close is the drag
(PF=1.018, 42% of trades on val-full). Pre-market val-full PF=1.497; test pre-market PF=2.133
(possibly period-specific weakness 2023–mid-2024). luld_upper is stable across all splits.

**Config:** `config/phase_f.json` | **Results:** `results/phase_f/` | **Docs:** [[Phase_F_Results]]

### Phase G — Scanner Context Analysis (analysis only, no new backtest)

Source: `results/phase_f/val_full/per_trade.parquet` (6,004 trades), 80 sampled dates.

Key findings (Phase H candidates — not yet implemented):

| Signal | Observation | Candidate gate |
|--------|-------------|----------------|
| Rank 1 underperforms | PF=1.18 vs ranks 3–9 PF=2.67–6.04 | Avoid rank 1 entries |
| Heat gate | Cold Q1 PF=1.46 vs Hot Q4 PF=2.62 | Require scanner heat above session median |
| Multi-day runner | PF=2.76 vs fresh PF=1.94 (+0.82) | Prefer tickers with momentum event in prior 5 days |
| TOD midday | 11:30–13:00 ET near-breakeven | Exclude midday window |
| Rank 3 + Hot Q4 | PF=6.46, n=124 | Combined rank × heat filter |

**Results:** `results/phase_g/` | **Docs:** [[Phase_G_Results]]

### Phase G v2 — Momentum-Weighted Scanner Quartile (analysis only)

Replaces Phase G's population-level heat bins with within-snapshot momentum quartiles.
Q1 = dominant movers (≥25% of snapshot momentum), Q4 = secondary names.

| Quartile | n | PF | EV% |
|----------|---|----|-----|
| Q1 (dominant) | 697 | 1.252 | +0.296% |
| Q2 | 448 | 1.911 | +0.814% |
| Q3 | 476 | 2.517 | +1.280% |
| Q4 (secondary) | 1,249 | **3.058** | +1.194% |

Rank 1 and Q1 are structurally equivalent — the dominant mover is definitionally Q1. The rank
1 underperformance finding from Phase G and the Q1 underperformance here are the same phenomenon.

> **NOT ACTIONABLE.** The Q1→Q4 PF gradient is clearly visible in analysis but breaks down in
> practice. No quartile-based entry gate is implemented or planned. Phase G v1/v2 findings are
> analysis-only — do not implement any scanner context filter without a dedicated validation phase.

**Results:** `results/phase_g_v2/` | **Docs:** [[Phase_G_v2_Results]]

### Phase EPG-GRT — Gate Reaction Time Optimization

Swept 117 ParticipationGate variants (asymmetric hysteresis p_open / p_close, τ=120/180/300s,
cooling variants). 300-event training + 100-event val (seed=99).

Key findings:
- **Asymmetric hysteresis dominates**: p_close < p_open consistently outperforms symmetric gate
- **Best val**: var_a_t300_po65_pc30 PF=2.584 (τ=300, p_open=0.65, p_close=0.30, no cooling)
- **Live config selected by user**: var_a_t120_po65_pc65 (symmetric, τ=120) — faster reaction time
  trades lower per-trade quality for earlier entry. Not Borda-ranked but captures first impulse.
- **Year stability**: all top 10 configs profitable every year 2020–2023

**Results:** `results/phase_epg_grt/` | **Config:** `config/phase_epg_grt/`

### Phase EPG-OPT2 — EPG Optimization Stage 2

Multi-stage sweep building on EPG-GRT. Tested p_close floor to 0.15, peak cooling variants,
SlopeGate F_ss and F_sl.

Key findings:
- p_close=0.15 peaks PF=3.06 training but is regime-sensitive; not Borda-selected
- Peak cooling consistently degrades (t120 configs develop 50k–95k pathological trade counts)
- SlopeGate F_ss: all DQ'd on first run due to lookback buffer pruning bug (fixed); best val PF=1.49
- SlopeGate F_sl best: s3_fsl_t180_l60_ko20_pc50, val PF=1.49 — below GRT baseline
- **T8 escalation**: all Stage 1+2 val candidates below GRT baseline (PF=2.584). Hard stop.
- GRT winner var_a_t300_po65_pc30 PF=2.584 remains best overall.

**Results:** `results/phase_epg_opt2/`

### Phase EPG-OPT2-SF — Setup Filter Integration Test

Tested SF as an entry-stack gate on top-decile EPG-OPT2 configs (52 configs, seed=99 val).

- **Result: net negative.** Mean delta_pf = −0.085. 47/52 configs hurt by the filter.
- High-PF τ=300/low-pc configs lose the most (up to −0.32 PF); SF blocks 43–53% of entries.
- **Why it fails:** SF is a sustained-liquidity quality screen. This strategy's edge is in the
  first impulse *before* Q̃ confirms. SF and early-entry alpha are misaligned.
- **Data does not support adding the setup filter to the entry stack.**

**Results:** `results/phase_epg_opt2_sf/`

### Phase WJI-SlowEMA — Slow EMA Gate (T3b escalation — parked)

Hypothesis: replace the `RunningMaxGate` (monotonically non-decreasing peak reference) with a
slow EMA of WJI that adapts to current momentum level. An EMA reference would avoid stale-peak
lock-in while still filtering noisy entries via asymmetric hysteresis (p_open > p_close).

**Signal (unchanged from WJI-OPT):**
```
WJI(t) = norm_λ_V^0.5 × norm_λ_buy_slow^0.5   (τ_V=180s, β_slow=0.01)
```

**Reference (new):**
```
WJI_slow(t) = WJI_slow × exp(-ln2·dt/τ_slow) + WJI × (1 − exp(-ln2·dt/τ_slow))
```
State transitions: FAIL→PASS at `WJI ≥ p_open × WJI_slow`; PASS→FAIL at `WJI < p_close × WJI_slow`.
`dt` is **halt-adjusted active seconds** (T1 audit: halt detection was not wired into any prior sweep runner; wired here for the first time).

**T3 sweep — 25 configs** (τ_slow ∈ {300,600,900,1200,1800}, p_open ∈ {0.70,…,0.90}, p_close=0.55 fixed):

| Best PF | Best CVaR5 | Baseline PF | Baseline CVaR5 |
|---------|-----------|-------------|----------------|
| 1.2219 (t300_po75) | −16.79% (t900_po70) | 1.1881 | −9.16% |

**T3b escalation: zero configs met CVaR5 ≥ −10.0%.** Hard stop; T4/T5/T7 blocked.

**Root cause:** The EMA reference adapts downward during momentum deceleration, maintaining
`WJI/WJI_slow ≈ 1.0` as both decline together. The gate stays PASS through deceleration and
reversal, producing deep tail losses. The `RunningMaxGate` avoids this because the peak never
falls — decelerating WJI exits immediately. Stagnation (T3c): ~57–113 PASS↔FAIL cycles per
event (threshold: 8), reflecting continuous threshold crossings due to tick-scale WJI noise
against a near-stationary EMA level.

**Status:** Parked (TBD — not abandoned, not approved for follow-on). Three remediation paths
identified: (1) tighten p_close sweep {0.70–0.80}; (2) add a peak floor to WJI_slow
(`max(ema, peak × floor)`); (3) abandon in favour of `RunningMaxGate`. Requires Cooper decision.

**Results:** `results/phase_wji_slow_ema/t3_sweep.json` | **Gate doc:** [[wji_slow_ema_gate]]

---

## Known Limitations

1. **LULD lower band is destructive; upper band is highly valuable.** Phase E confirmed:
   luld_lower PF=0.059 (pre-empts EXIT_D on declining trades). luld_upper PF=11–18 across
   all splits — captures parabolic moves that exhaust at the LULD ceiling. Current config:
   upper only.

2. **epg_window_close is near-breakeven on full val (PF=1.018).** Sample runs are optimistic
   for this exit reason. 100-event samples consistently overstate performance vs val-full
   by ~0.38 PF. Weight val-full numbers more heavily.

3. **Gap gate removal (Phase C+) introduces look-ahead bias.** Backtest admits sub-30% gap
   entries the live scanner wouldn't flag. Intra-window watermark partially mitigates it but
   does not fully correct the bias.

4. **CVD accumulator bug (fixed).** Original Phase C CVD PF=2.0328 was invalid — ambiguous
   trades (~9.5%) were mapped to sells. Fixed PF=1.7544 (Phase C.5). Watermark 5%
   (PF=1.9443) is the current best single filter.

5. **Setup filter excluded from initial entry gate.** Phase EPG-OPT2-SF confirmed this is
   correct: adding SF as entry gate reduces mean PF by 0.085 (47/52 configs hurt). SF blocks
   the early-impulse entries that carry this strategy's edge.

6. **Rank 1 underperformance (Phase G/G v2).** The fastest-moving scanner name at entry
   time produces PF=1.18 vs 2.67–6.04 for ranks 3–9. Q1 (dominant momentum share) and rank
   1 are structurally equivalent. A rank gate is a Phase H candidate; not yet validated.

7. **SlopeGate F_ss deployed live but not backtested.** Live EPG core replaced
   ParticipationGate with SlopeGate F_ss (2026-06-03). The backtest runner still uses
   ParticipationGate. No backtest validation of the swap exists.

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
