# Phase SEB — Scanner Entry Backtest Report

_Generated: 2026-06-16 19:12  Poll interval: 20s  Runner threshold: MFE(30m) ≥ 5% (UNVALIDATED)_

## Headline Gap

| Metric | Tier 0 (live) | Tier 1 (catalog) | Gap (T1−T0) |
|--------|--------------|-----------------|-------------|
| Candidates | 0 | 7585 | — |
| Entries | 0 | 990 | +990 |
| Runner rate | n/a (0/0) | 62.8% (622/990) | n/a |

> **Interpretation:** The T1−T0 gap in runner rate is the estimated selection-bias inflation from using the catalog as the universe. Tier 0 is the honest sample; Tier 1 is mechanically larger. Always read Tier 1 numbers relative to Tier 0.

## No-Entry Breakdown

### Tier 1 (catalog)

| Reason | Count |
|--------|-------|
| gate_a_mismatch | 3789 |
| sf_not_qualifying | 1596 |
| no_prev_close | 961 |
| no_30pct_cross | 166 |
| insufficient_session_trades | 83 |


## Entry Distribution by Session Bucket

### Tier 1

| Bucket | Entries | Runner Rate |
|--------|---------|-------------|
| regular_hours | 806 | 63.0% |
| pre_market | 144 | 63.2% |
| post_market | 40 | 57.5% |


## Forward Return Summary (entries only)

| Metric | Tier 0 median | Tier 1 median |
|--------|--------------|--------------|
| mfe_5m | n/a | 3.23% |
| mfe_15m | n/a | 5.62% |
| mfe_30m | n/a | 7.20% |
| ret_30m | n/a | -1.14% |
| eod_ret | n/a | -1.48% |

## Entry Slippage (bar close → first tick after)

- **Tier 1**: median=0.000%  mean=0.071%  p95=1.142%


## Setup Filter — Weakest Signal at Entry

**Tier 1**: body=412  range=397  thinness=149  volume=32


---
_Phase SEB is a read-only research harness. No live tables were modified. Runner threshold (MFE 30m ≥ 5%) is an UNVALIDATED HEURISTIC._