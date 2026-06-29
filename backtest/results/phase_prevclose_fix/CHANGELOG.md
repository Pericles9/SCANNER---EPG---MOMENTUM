# FIX-PREVCLOSE — prev_close Source 3 Cutoff Correction

## Bug
`_try_prior_trades_parquet` in `backtest/data/loaders/prev_close.py` uses a 20:00 ET cutoff
when finding the prior-day close price. For events with post-market earnings moves,
this returns the post-market high rather than the official 4pm close. The inflated
prev_close raises the 30% scanner threshold, causing events to be missed or given
wrong scanner hit times in the backtest.

## Evidence
DIAG-TAPE (phase_diag_tape/): 13 first-appearance events showed gap_occurs_in=T1_POSTMARKET —
price moved materially in T-1 post-market before the event day. Their prev_close was wrong.

## Fix
File: `backtest/data/loaders/prev_close.py`, function `_try_prior_trades_parquet`
Change:

```python
# Before:
cutoff_ns = midnight_utc + 20 * 3600 * NS_PER_SECOND + et_offset_ns
# After:
cutoff_ns = midnight_utc + 16 * 3600 * NS_PER_SECOND + et_offset_ns
```

## Downstream impact
Scanner hit catalog must be rebuilt (wrong thresholds throughout).
val_r3_stratified.json must be rebuilt (event universe and scanner hit times change).
phase_r1_fixed/, phase_r15/, phase_r3/ results are invalidated — archived, not deleted.

## Sparse warmup note
ANCHOR_LATE / WARMUP_AT_DEADLINE failures (true overnight gap-ups with no pre-scanner tape)
are a separate structural issue. Not fixed in this phase. Entry audit in T9 measures residual.
