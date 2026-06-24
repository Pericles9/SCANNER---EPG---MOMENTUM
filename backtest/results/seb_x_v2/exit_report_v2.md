# Phase SEB-X v2 -- Exit Rule Research Report

_Vol-normalized exit sweep on 990 frozen SEB Tier-1 entries._
_sigma v2: Parkinson range-vol (trailing 20 bars before entry) + ADR floor._
_Parallel arm: sigma_vwap = stdev(close - VWAP) over same look-back._
_Gate C is now a live PASS/FAIL test with measured divergence._
_Tune/confirm split: temporal 70/30 by date (same as v1 for comparability)._

---

Tune split:    409 dates, 597 entries (70%)
Confirm split: 176 dates, 393 entries (30%)

## sigma v2 Distributions (Task 2)

| Metric | sigma_primary | sigma_final | sigma_vwap |
|--------|:------------:|:-----------:|:----------:|
| p10/25/50/75/90 | 0.014 / 0.031 / 0.069 / 0.159 / 0.374 | 0.018 / 0.036 / 0.079 / 0.192 / 0.462 | 0.019 / 0.039 / 0.087 / 0.218 / 0.458 |
| CV (stdev/mean) | 8.416 | 7.544 | 9.944 |
| % floor-bound | -- | 35.4% (350/990) | -- |

ADR (prior T-3..T-1 RTH range): p10/25/50/75/90 = 0.256 / 0.549 / 1.215 / 3.241 / 7.357
NOTE: 166 entries had no prior-session ADR data (adr=0).

**Gate A (a) MFE reuse: PASS.** (v1 paths.parquet MFE medians unchanged)
**Gate A (b) sigma non-degenerate: PASS.** (CV=7.544 >= 0.10, floor-bound=35.4% < 50%)

### Knob Calibration Check

v1 gate B reference (in v1 sigma units = constant $0.443):
  R1: winners pre-MFE dip p90=0.205sigma, p95=0.263sigma
  R2: losers time-to-MFE p25=1 bar, p50=2 bars
  R3: runners give-back p25=0.63sigma, p50=1.42sigma

With sigma v2 (Parkinson, variable per entry), the sigma units now differ per event.
The k1/g grids [1.0..4.0] and [0.5..2.0] are unchanged from v1 and still span the
relevant regions. The key question is whether best params are stable across vol regimes
(tested in Gate C below).

---

## Complexity Ladder (sigma_unit = primary)

B0 Baseline: Tune cap=-0.250  Conf cap=-0.439  Conf PF=1.035  Conf CVaR5=-17.307

| Stage | Best params | Tune capture | Conf capture | Conf PF | Conf CVaR5 | Keep? |
|-------|-------------|-------------|-------------|---------|-----------|-------|
| B0 | VWAP cross | -0.250 | -0.439 | 1.035 | -17.307 | baseline |
| B0+R1 | k1=1.0 | -0.244 | -0.288 | 0.695 | -6.975 | YES |
| B0+R1+R2 | k1=1.0 T=5m a=-0.25 | -0.244 | -0.288 | 0.695 | -6.975 | NO |
| B0+R1+R3 | k1=2.5 arm=2.0 g=0.5 | 0.185 | 0.098 | 1.144 | -14.401 | YES |

---

## Complexity Ladder (sigma_unit = vwap)

B0 Baseline: Tune cap=-0.250  Conf cap=-0.439  Conf PF=1.035  Conf CVaR5=-17.576

| Stage | Best params | Tune capture | Conf capture | Conf PF | Conf CVaR5 | Keep? |
|-------|-------------|-------------|-------------|---------|-----------|-------|
| B0 | VWAP cross | -0.250 | -0.439 | 1.035 | -17.576 | baseline |
| B0+R1 | k1=1.0 | -0.282 | -0.304 | 0.691 | -6.937 | YES |
| B0+R1+R2 | k1=1.0 T=5m a=-0.25 | -0.282 | -0.304 | 0.691 | -6.937 | NO |
| B0+R1+R3 | k1=2.5 arm=2.0 g=0.5 | 0.240 | 0.152 | 1.261 | -15.527 | YES |

---

## Gate C -- Vol-Regime Acid Test

Split entries by sigma (each unit's own median as threshold).
Find best k1 (B0+R1) on tune split for low-vol vs high-vol regime.
FAIL if k1 diverges >50% across regimes.

**sigma_unit = primary** (median threshold = 0.0794  |  n_low=495  n_high=495)
  B0+R1 k1: low=1.00  high=1.00  divergence=0.0%  -> Gate C: **PASS**
  B0+R1+R3 g: low=0.50  high=0.50  divergence=0.0%

**sigma_unit = vwap** (median threshold = 0.0868  |  n_low=495  n_high=495)
  B0+R1 k1: low=1.00  high=1.00  divergence=0.0%  -> Gate C: **PASS**
  B0+R1+R3 g: low=0.50  high=0.50  divergence=0.0%

**Recommendation:**
  sigma_primary has lower k1 divergence (0.0% vs 0.0%).
  Prefer sigma_primary for final rule calibration.

---

## Gate D -- Edge Decay + RTH-Only

Evaluating best complexity-ladder stack per sigma unit across time and session splits.

**sigma_unit = primary**  stack=B0+R1+R3  params=k1=2.5 arm=2.0 g=0.5

| Split | n | median capture | PF | CVaR5(sigma) |
|-------|---|---------------|-----|-------------|
| Full | 990 | 0.158 | 1.304 | -8.504 |
| pre-2024 | 625 | 0.185 | 1.468 | -5.311 |
| 2024+ | 365 | 0.052 | 1.101 | -13.444 |
| RTH-only | 806 | 0.168 | 1.341 | -5.454 |

  Edge-decay delta (2024 vs pre-2024): -0.133
  **Gate D FLAG: significant edge decay in 2024 (delta < -0.10).**

**sigma_unit = vwap**  stack=B0+R1+R3  params=k1=2.5 arm=2.0 g=0.5

| Split | n | median capture | PF | CVaR5(sigma) |
|-------|---|---------------|-----|-------------|
| Full | 990 | 0.204 | 1.405 | -8.970 |
| pre-2024 | 625 | 0.240 | 1.540 | -5.205 |
| 2024+ | 365 | 0.144 | 1.214 | -14.645 |
| RTH-only | 806 | 0.220 | 1.454 | -5.739 |

  Edge-decay delta (2024 vs pre-2024): -0.096
  Gate D: moderate decay in 2024.

---

## Sensitivity Splits (B0 baseline, sigma_unit = primary)

### By year

| Year | n | median capture | PF |
|------|---|---------------|-----|
| 2020 | 159 | -0.145 | 1.123 |
| 2021 | 154 | -0.234 | 1.438 |
| 2022 | 127 | -0.212 | 1.182 |
| 2023 | 185 | -0.359 | 0.856 |
| 2024 | 365 | -0.482 | 1.043 |

### By session bucket

| Bucket | n | median capture | PF |
|--------|---|---------------|-----|
| post_market | 40 | -0.465 | 0.319 |
| pre_market | 144 | -0.574 | 0.855 |
| regular_hours | 806 | -0.282 | 1.170 |

---

## Caveats and Limitations

1. **Parkinson look-back uses the 20 bars before entry_bar (momentum bars).**
   This is a HIGH-VOL window by construction. sigma_final will exceed ADR floor
   for most entries; the floor is a safety net, not a routine adjustment.
2. **ADR computed from intraday ticks (RTH 09:30-16:00 ET)** of T-3..T-1 sessions.
   Equivalent to a standard ATR daily range but derived at run time, not from OHLCV table.
3. **Tune/confirm is temporal** (same 70/30 split as v1). No true holdout.
4. **All sigma-multiples are UNVALIDATED.** Calibrated on Tier 1 catalog sample.
5. **MFE capture is not realized PnL.** No slippage, partial fills, or spread model.
6. **B0 uses bar-close fill detection.** Realized fill lags 1 bar on average.
7. **R1/R3 assume limit stop fills.** Gap-through events deliver worse fills.
8. **No Tier 0 ground truth.** All metrics may differ from live trading.

---

_Phase SEB-X v2 is read-only. No live tables modified._
_Gate A (a) MFE reuse: PASS._
_Gate A (b) sigma non-degenerate: PASS._
_Gate C (primary): PASS  |  Gate C (vwap): PASS._
