---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-06-29
phase: DIAG-TAPE
---

# Phase DIAG-TAPE — Pre-Event Price Action: Summary

Read-only data investigation. No code changes, no fix recommendations.

## T-1 data availability

| Metric | Count |
|--------|-------|
| First-appearance events with T-1 PM data present | 36 / 37 |
| First-appearance events with T-1 PM data absent  | 1 / 37 |

### first_trade_wall_clock_et distribution (37 first-appearance events)

| Stat | Value |
|------|-------|
| Earliest | 04:00 ET |
| Median   | 04:03 ET |
| Latest   | 09:00 ET |
| Before 06:00 ET | 29 / 37 |
| Before 09:30 ET (pre-market) | 37 / 37 |

## Gap location breakdown

| Gap occurs in | N | % |
|---------------|---|---|
| UNKNOWN | 15 | 40.5% |
| T1_POSTMARKET | 13 | 35.1% |
| T_PREMARKET | 6 | 16.2% |
| OVERNIGHT_NO_TAPE | 3 | 8.1% |

Total first-appearance events: 37

### T1_POSTMARKET events (gap developed during T-1 post-market)

  - EVAX 2024-01-23: n_t1_pm=100, gap_from_t1_close=-2.3%
  - LMFA 2024-05-16: n_t1_pm=441, gap_from_t1_close=18.2%
  - MLGO 2024-03-25: n_t1_pm=19927, gap_from_t1_close=55.9%
  - ELWS 2024-02-14: n_t1_pm=5286, gap_from_t1_close=79.6%
  - SNTG 2024-07-12: n_t1_pm=21183, gap_from_t1_close=66.5%
  - AIMD 2024-01-05: n_t1_pm=344, gap_from_t1_close=9.1%
  - STOK 2024-03-26: n_t1_pm=21131, gap_from_t1_close=88.1%
  - ASNS 2024-06-05: n_t1_pm=30171, gap_from_t1_close=233.2%
  - BZFD 2024-02-22: n_t1_pm=25889, gap_from_t1_close=140.7%
  - EFOI 2024-02-21: n_t1_pm=10214, gap_from_t1_close=164.5%
  - FRGE 2024-03-25: n_t1_pm=1488, gap_from_t1_close=40.3%
  - HNST 2024-03-07: n_t1_pm=5118, gap_from_t1_close=29.4%
  - JANX 2024-02-27: n_t1_pm=8655, gap_from_t1_close=115.3%

## What this means for the strategy

Data only. No fix recommendation.

The `gap_occurs_in` distribution shows where the price gap that triggered the scanner
actually occurred relative to the available tape.

- **OVERNIGHT_NO_TAPE**: The stock was already at/above the 30% threshold at its first
  event-day trade. The move happened overnight (or very early pre-market) with no tape
  in the parquet. The scanner hit is the first available tick. The Hawkes engine sees
  zero pre-scanner history, so the anchor and gate cannot warm up before the 300s deadline.

- **T_PREMARKET**: Price rose gradually from near prev_close during T pre-market
  (04:00–09:30 ET). The scanner hit came during pre-market. Whether this gives enough
  pre-scanner trade history for anchor warmup depends on how much pre-market activity occurred.

- **T1_POSTMARKET**: The catalyst was visible in T-1 post-market trading. The scanner
  could in principle have seen this earlier in live trading, but the current design
  evaluates only the event date.

- **UNKNOWN**: Insufficient data to classify.

## Comparison with controls

The 5 traded controls had substantial pre-scanner trade history:

  - KOSS 2024-07-03: first_trade=04:00 ET, n_pre=65576, failure=TRADED
  - MNY 2024-02-20: first_trade=04:00 ET, n_pre=1424, failure=TRADED
  - PALI 2024-04-16: first_trade=05:32 ET, n_pre=15194, failure=TRADED
  - RBOT 2023-12-20: first_trade=05:03 ET, n_pre=7738, failure=TRADED
  - VS 2023-11-24: first_trade=04:08 ET, n_pre=2856, failure=TRADED

Traded controls have early first trades (pre-market or RTH open) with large n_pre-scanner
counts. Their anchors had time to fire and warm up before the scanner hit. This is in
direct contrast to first-appearance events where n_pre=0 by construction.



