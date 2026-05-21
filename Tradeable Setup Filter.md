---
status: ACTIVE
created: 2026-03-01
updated: 2026-03-06
tags:
  - type/strategy
  - domain/microstructure
  - domain/signal
  - project/hawkes-ofi-impact
  - status/approved
linked-thesis: [[Scanner-Hawkes-OFI Impact]]
---

# Tradeable Setup Filter

> **Purpose:** Universe gate. Continuously determines whether a stock is liquid and naturally active — or has gone illiquid. Applied in real time during live trading and retrospectively to the historical catalog before any calibration or backtesting. Any event that fails this filter is excluded from calibration, Phase 0 counts, and backtest results.

> **Module to build:** `core/filters/setup_filter.py` — `SetupFilter`. This module does not exist yet. All specification below describes what needs to be implemented. See [[Project_Directory]] for the full project structure.

---

## The Problem This Solves

The historical event catalog contains two structurally different event types that are identical by price move alone:

**Type A — Genuine momentum event:** The stock is actively traded continuously across the session. Candles have real bodies. Volume is consistently present. Price is moving in both directions as participants discover value.

**Type B — Repricing event:** The stock gaps to a new price level on news, then goes silent. The initial burst can be dramatic. But after it, candles collapse to doji, volume disappears, and only a handful of trades move price substantially. The market repriced and left.

Both clear the >50% move threshold used during data collection. The difference is not the size of the move — it is the **ongoing character of the session after the move**.

---

## Core Intuition

A stock is tradeable when participants are continuously engaged. Four symptoms reveal when they stop:

1. **Bar range collapses** — price stops moving. Candles get tiny.
2. **Volume drops** — participants have left. Arrival rate fell.
3. **Few trades moving price a lot** — the book is thin. High price impact per dollar traded.
4. **No directional commitment** — candles go sideways and become doji.

These four symptoms tend to occur together. But any one alone is not sufficient — the composite of all four is what distinguishes genuine illiquidity from normal variation.

The filter tracks all four continuously using exponential forgetting. Each signal normalizes itself against its own recent history — self-calibrating per symbol, per session. No fixed volume threshold. No assumed session structure. No discrete fitting windows.

---

## Mathematical Specification

All signals use exponential forgetting:

$$S_t = \rho \cdot S_{t-1} + (1 - \rho) \cdot x_t$$

where $\rho \in (0, 1)$ is the forgetting rate and $x_t$ is the current observation. No window, no hard cutoff, no stationarity assumption.

Two forgetting rates are used:

- $\rho_{slow} \approx 0.985$: background prior for each signal. Represents "what is normal for this stock right now."
- $\rho_{fast} \approx 0.90$: state tracker for the composite score. Responds to current conditions without flipping on a single bar.

---

### Signal 1 — Bar Range (Is Price Moving?)

$$r_t = \frac{high_t - low_t}{close_t}$$

Background prior:

$$\mu_r(t) = \rho_{slow} \cdot \mu_r(t^-) + (1 - \rho_{slow}) \cdot r_t$$

Score:

$$\text{RangeScore}(t) = \min\!\left(\frac{r_t}{\mu_r(t) \cdot c_r},\ 1\right)$$

where $c_r = 0.60$ (floor multiplier). When $r_t$ falls below 60% of its own running background, the score decays toward zero.

**Catches:** Candles getting tiny. Price discovery stopping. Sideways action.

---

### Signal 2 — Volume (Are Participants Present?)

$$\mu_v(t) = \rho_{slow} \cdot \mu_v(t^-) + (1 - \rho_{slow}) \cdot v_t$$

$$\text{VolScore}(t) = \min\!\left(\frac{v_t}{\mu_v(t) \cdot c_v},\ 1\right)$$

where $c_v = 0.30$.

This is not a raw volume threshold. A thick liquid stock has a high $\mu_v(t)$ by construction. A thin momentum stock has a low $\mu_v(t)$ at baseline — it only scores well when event volume is elevated relative to its own normal.

**Catches:** Participants leaving. Arrival rate dropping. The event ending.

---

### Signal 3 — Thinness (Are a Few Trades Moving Price?)

$$\tau_t = \frac{(high_t - low_t) \cdot P_t}{dv_t}$$

Price move per dollar traded — price impact per dollar of volume. When high, a small amount of dollar volume moves price a large amount. Signature of a thin book.

> **Why dollar volume, not share volume:** Share-volume thinness is sensitive to lot size — a single 1-share trade moving price 1% produces a thinness reading 100× higher than a 100-share trade with the same move. The dollar-volume formulation is fully scale-invariant: the same price impact per dollar traded produces the same thinness reading regardless of share price, lot size, or tick structure.

Background prior:

$$\mu_\tau(t) = \rho_{slow} \cdot \mu_\tau(t^-) + (1 - \rho_{slow}) \cdot \tau_t$$

Score is inverted — high thinness is bad:

$$\text{ThinScore}(t) = \max\!\left(1 - \frac{\tau_t}{\mu_\tau(t) \cdot c_\tau},\ 0\right)$$

where $c_\tau = 2.50$. When thinness blows beyond 2.5× its own background, the score collapses toward zero.

**Catches:** The "few trades to move it" symptom. Illiquid book. Outsized price impact per dollar.

**Data requirement:** Each bar must have dollar volume available. Compute as $dv_t = v_t \cdot vwap_t$ if not stored directly. If $dv_t = 0$, set $\tau_t = \mu_\tau(t^-)$.

---

### Signal 4 — Body Conviction (Is There Directional Commitment?)

$$b_t = \frac{|close_t - open_t|}{high_t - low_t}$$

The ratio of candle body to candle range. Doji and indecision bars have $b_t \approx 0$. Directional bars have $b_t$ close to 1.

Background prior:

$$\mu_b(t) = \rho_{slow} \cdot \mu_b(t^-) + (1 - \rho_{slow}) \cdot b_t$$

Score:

$$\text{BodyScore}(t) = \min\!\left(\frac{b_t}{\mu_b(t) \cdot c_b},\ 1\right)$$

where $c_b = 0.40$.

**Catches:** Random walk, choppy, no momentum. The doji symptom. Eliminates sideways repricing sessions from the genuine momentum catalog.

---

### Composite Score

$$Q(t) = \sqrt[4]{\text{RangeScore}(t) \cdot \text{VolScore}(t) \cdot \text{ThinScore}(t) \cdot \text{BodyScore}(t)}$$

Geometric mean of all four signals. One failing signal collapses the composite. Arithmetic mean would let three healthy signals mask a failing fourth.

Apply $\rho_{fast}$ smoothing to the composite:

$$\tilde{Q}(t) = \rho_{fast} \cdot \tilde{Q}(t^-) + (1 - \rho_{fast}) \cdot Q(t)$$

**Qualification condition:** A stock is tradeable at time $t$ if:

$$\tilde{Q}(t) \geq 0.65 \quad \text{sustained for at least 15 minutes}$$

The 15-minute persistence requirement prevents a single good bar from qualifying a name mid-collapse.

---

### ψ — Data Integrity Check

$$\psi_t = \frac{close_t - low_{t-N}}{low_{t-N}} > 0.50 \quad \text{where } N \text{ covers the prior } 3 \text{ trading days}$$

ψ verifies the stock actually gapped. It is a data quality check, not a signal. If ψ fails, the event is bad data, not a tradeable condition. ψ is computed from a lookback window (the one justified exception to the no-windows rule) and is excluded from precision/recall analysis entirely.

---

## Live Application

The filter runs continuously on each new 1-minute bar for each eligible symbol during the 4:00 AM – 8:00 PM ET session. Bars are reconstructed from tick data in `filtered/{TICKER}_{DATE}_{MOM}/trades.parquet` by binning into 1-minute OHLCV. Dollar volume per bar is `sum(price × size)` over all trades in the bin.

**Warm-up gate:** After approximately $1/(1-\rho_{slow}) \approx 65$ bars the background priors have meaningful estimates. Before that point, require a provisional threshold of 0.75 or hold off on qualifying the event. Do not hard-fail early — require higher confidence.

**LULD halt handling:** Freeze signal state updates during detected halt windows. A halt gap is not illiquidity. Resume updating on the first bar after halt lift.

> **Split consistency:** When the setup filter is applied to the historical catalog for calibration purposes, it must be applied only to the training and validation splits defined in Phase 0.5 of [[Scanner-Hawkes-OFI Impact]]. The test split must not be touched until Phase E. The setup filter mask for the test split is computed but not used in any calibration step.

---

## Test Process

### Phase F0 — Filter Validation (Blocking)

**Step F0.1 — Build labeled set**

Label at least 200 events from the catalog. For each: PASS or FAIL using the 1-minute extended-session chart. Labeling criterion is purely structural — does the session look alive and naturally active throughout, or did it reprice and die? Do not label by trade outcome. Record which symptom was most visible (range collapse / volume drop / thinness / doji).

Use only events from the training split (defined in Phase 0.5). Do not label test split events.

**Step F0.2 — Compute Q(t) trajectories**

For each labeled event run the four signals across the full T=0 session. Record: mean $\tilde{Q}$, minimum sustained $\tilde{Q}$ over any 15-minute window, time when $\tilde{Q}$ first crosses 0.65, time when it last falls below 0.65 and does not recover, and which signal was the weakest bottleneck.

Verify thinness computation uses dollar volume throughout: $\tau_t = (high_t - low_t) \cdot P_t / dv_t$. If dollar volume is not stored in the catalog, compute it as $v_t \cdot vwap_t$ per bar.

**Step F0.3 — Separation analysis**

Plot session-mean $\tilde{Q}$ distributions for PASS and FAIL events as overlapping histograms. Target: near-zero overlap at 0.65. If overlap is significant, examine the weakest signal — adjust its floor/ceiling parameter first before touching the composite threshold.

Do not include ψ in this analysis.

Also plot $\tilde{Q}(t)$ trajectories for 5 representative PASS and 5 FAIL events. PASS events should stay above 0.65 for most of the session. FAIL events should collapse through it after the initial burst.

**Step F0.4 — Threshold validation**

Test thresholds 0.55 to 0.75 in 0.05 steps. Report precision and recall at each. Select the threshold maximizing precision subject to recall ≥ 0.60. Starting point is 0.65 — confirm on the full labeled set or adjust.

**Step F0.5 — Full catalog pass rate (training split only)**

Apply to the training split. Record: total events, events failing ψ (data integrity only), events with $\tilde{Q}$ never sustaining 0.65 for 15 minutes, final PASS count. Blocking condition: fewer than 300 PASS events — do not proceed if so.

**Step F0.6 — Qualitative spot check**

Inspect 10 random PASS and 10 random FAIL from the training split. Confirm visual session character matches the label. Log any surprises.

---

### Phase F1 — Backtest Integration

Apply filter mask to backtest universe. Re-run Phase 0 triple-AND pass rate on filtered universe. Filtered universe should produce a higher Phase 0 pass rate — bad events were failing at the Hawkes layer anyway. If Phase 0 rate drops after filtering, investigate before continuing.

---

### Phase F2 — Calibration Impact

Re-run Layer 3 regression (β_impact, perm_frac) on filtered training event set. Compare parameter estimates, R², and residual distributions before and after filtering. Expected: tighter residuals, more stable β. If β shifts > 20%, prior calibration was contaminated. Use post-filter values.

Also verify that the thinness signal on dollar-volume basis produces tighter perm_frac residuals than a share-volume formulation. If it does not, investigate dollar volume data quality for affected tiers.

---

## Calibration Log

| Date | Catalog size | Labeled sample | Q_min | Precision | Recall | PASS count | Notes |
|------|-------------|---------------|-------|-----------|--------|------------|-------|
| — | — | — | 0.65 | — | — | — | Full validation pending Phase F0 per spec above |

---

## Known Failure Modes and Mitigants

| Failure mode | Mitigant |
|-------------|----------|
| Forgetting priors unstable in first ~65 bars | Require provisional threshold of 0.75 until $1/(1-\rho_{slow})$ bars elapsed |
| Single anomalous bar (halt resume, print error) spikes thinness | $\rho_{fast}$ smoothing on composite absorbs single-bar spikes; freeze state during LULD halts |
| Liquid stock fails vol score on a quiet bar | VolScore floor $c_v = 0.30$ is deliberately low; if a thick stock fails, lower $c_v$ — it should never fail |
| Thin stock burst looks alive then collapses | Correct behavior. The filter qualifies based on what happens after the burst, not the burst itself |
| ψ unreliable for new listings < 3 days old | Skip ψ pre-filter for new listings; proceed directly to signal computation |
| Thinness undefined when dollar volume = 0 | Set $\tau_t = \mu_\tau(t^-)$ when $dv_t = 0$ |
| Dollar volume not available in catalog | Compute as $v_t \cdot vwap_t$ per bar; flag these bars in calibration log |
| Thinness comparison invalidated by price level differences | Dollar-volume formulation is scale-invariant to price level; no adjustment needed |

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| Four signals, geometric mean | All four illiquidity symptoms must be absent simultaneously; geometric mean enforces this |
| Self-calibrating per signal via forgetting background | Fixed thresholds fail across stocks with different liquidity profiles |
| Thinness on dollar volume, not share volume | Share-volume thinness is sensitive to lot size; dollar-volume thinness is scale-invariant |
| Thinness as a distinct signal | Volume alone misses the case where moderate dollar volume produces outsized price moves |
| Continuous updating, not batch fitting | Batch fitting imposes discrete session structure, delays qualification, and cannot detect mid-session state changes |
| Composite threshold 0.65 | Starting point; refine on full labeled training catalog in Phase F0 |
| ψ as data integrity check only | A genuine 50%+ gap will always pass ψ; ψ failing means bad data, not a tradeable condition |
| ψ uses lookback, not forgetting | ψ is a historical range comparison requiring a fixed past reference; the one justified exception to the no-windows rule |
| No minimum trade count hard floor | Very few trades naturally produces near-zero VolScore and elevated ThinScore, failing the composite organically |
| Test split excluded from F0 labeling | Filter validation is calibration work; the test split must remain untouched until Phase E |

---

## Related Notes

- [[Scanner-Hawkes-OFI Impact]] — parent thesis; Phase 0.5 defines the train/test split
- [[Project_Directory]] — full modular project structure; `core/filters/setup_filter.py` spec lives here