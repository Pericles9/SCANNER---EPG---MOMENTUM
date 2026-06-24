# Phase SEB-X -- Exit Rule Research Report

_Vol-normalized exit sweep on 990 frozen SEB Tier-1 entries._
_All thresholds in sigma-units. Raw-percent values shown as derived consequences only._
_Tune/confirm split: temporal, 70/30 by date._

---

Tune split: 409 dates, 597 entries (70%)
Confirm split: 176 dates, 393 entries (30%)

## B0 Baseline (VWAP Cross, zero new params)

| Split | n | median capture(30m) | PF | CVaR5(sigma) |
|-------|---|--------------------|----|-------------|
| Full  | 990 | -0.333 | 1.080 | -0.441 |
| Tune  | 597 | -0.250 | 1.117 | -0.408 |
| Conf  | 393 | -0.439 | 1.035 | -0.476 |

## sigma-TP/SL Baseline (2-param reference)

Best TPSL: k_tp=2.0sigma  k_sl=2.0sigma
| Split | n | median capture(30m) | PF | CVaR5(sigma) |
|-------|---|--------------------|----|-------------|
| Full  | 990 | 0.025 | 1.153 | -0.635 |
| Conf  | 393 | -0.020 | 1.103 | -0.671 |

## Complexity Ladder (B0 -> +R1 -> +R2 -> +R3)

Each rule is KEPT only if confirm-split beats the prior stage without it.
Capture = pnl / MFE(30m). Higher = better retention of available move.

| Stage | Best params (sigma) | Tune capture | Conf capture | Conf PF | Conf CVaR5 | Keep? |
|-------|---------------------|-------------|-------------|---------|-----------|-------|
| B0 (baseline) | VWAP cross | -0.250 | -0.439 | 1.035 | -0.476 | baseline |
| B0+R1 | k1=3.0 | -0.250 | -0.494 | 1.018 | -0.468 | NO |
| B0+R1+R2 | k1=3.0, T_dead=5m, a=-0.25 | -0.250 | -0.494 | 1.018 | -0.468 | NO |
| B0+R1+R3 | k1=2.5, arm=1.0, g=0.5 | 0.147 | -0.166 | 0.977 | -0.452 | YES |
| B0+R1+R2+R3 | k1=2.5, T_dead=10m, a=0.00, arm=1.0, g=0.5 | 0.147 | -0.166 | 0.977 | -0.452 | NO |

---

## Headline Finding: sigma Degeneration (99.8% Fallback)

988/990 entries (99.8%) use the global-median sigma fallback.
Effective sigma = 0.4433 (constant) for all these entries.

**Root cause:** `armed_bar == entry_bar` for nearly all entries. The armed window
is 1 bar (below the 3-bar minimum), so per-event ATR cannot be computed.

**Consequence:** sigma-normalized thresholds (k1*sigma, g*sigma, arm*sigma) are
effectively fixed dollar amounts, not per-event vol adjustments. The normalization
goal (make k portable across events with different vol) was NOT achieved.

**Recommended fix for SEB-X v2:** Use trailing 14-bar ATR ending at entry_bar
(look-back, not armed window). This always provides a meaningful window.
Until then, treat all sigma-multiples in this report as dollar-denominated knobs
(e.g., k1=2.5 means stop $1.11 below entry for an average entry).

---

## Gate C -- Vol-Regime Acid Test

Split entries by sigma (primary ATR). Low = below median, High = above median.
FAIL if best k1/g diverge >50% across regimes (sigma-normalization broken).

**B0+R1**: low-vol best k1=nan  high-vol best k1=2.50
  divergence=nan%  Gate C: **INCONCLUSIVE**

**B0+R1+R3**: low-vol best k1=nan  high-vol best k1=2.50
  divergence=nan%  Gate C: **INCONCLUSIVE**

**HEADLINE FINDING: Gate C INCONCLUSIVE.**
sigma is identical for 99.8% of entries (all using the global-median fallback).
Low-vol vs high-vol split is degenerate -- cannot test the normalization hypothesis.
Root cause: armed_bar == entry_bar for nearly all entries (1-bar window < 3-bar minimum).
Fix: use a fixed look-back ATR (e.g., trailing 14 bars before entry) instead of the
armed-window ATR. Do NOT label this run as confirming sigma-portability.

---

## Sensitivity Splits

### By session bucket

| Bucket | n | median capture | PF |
|--------|---|---------------|-----|
| regular_hours | 910 | 0.129 | 1.123 |
| pre_market | 290 | -0.165 | 0.911 |
| post_market | 106 | -0.403 | 0.732 |

### By event-day vs off-day

| Event day | n | median capture | PF |
|-----------|---|---------------|-----|
| event_day | 990 | 0.101 | 1.095 |
| off_day | 0 | nan | nan |

### By year

| Year | n | median capture | PF |
|------|---|---------------|-----|
| 2020 | 159 | 0.143 | 1.063 |
| 2021 | 154 | 0.229 | 1.473 |
| 2022 | 127 | 0.160 | 1.220 |
| 2023 | 185 | 0.069 | 1.200 |
| 2024 | 365 | -0.211 | 0.946 |

---

## Caveats and Limitations

1. **Tune/confirm split is temporal (not random).** With Tier 0 empty, this is the
   minimum defense. Results are indicative only -- no holdout has been used.
2. **All sigma-multiples in this report are UNVALIDATED HEURISTICS.** They are calibrated
   to the Tier 1 catalog sample and may not generalize to live trading.
3. **MFE capture is not realized PnL.** Fill slippage, partial fills, and spread are
   not modeled. Assume ~0.5-2% slippage on entry; exit slippage varies by rule.
4. **B0 (VWAP cross) uses bar-close detection.** Realized fill will lag 1 bar on average.
5. **R1/R3 assume limit stop fills.** Gap-through scenarios deliver worse fills.
6. **No Tier 0 ground truth.** Runner rate, capture, and slippage estimates may all
   differ materially from live trading due to catalog selection bias.

---

_Phase SEB-X is read-only. No live tables modified._
_Gate A (MFE reproduction): PASS._
_Gate C (vol-regime split): INCONCLUSIVE -- see Headline Finding above._
