# Phase VAL-FULL � T1 Pool Definition

**Pool file:** `backtest/data/val_full.json`  
**Total pool size (natural distribution): 522 events**

## Universe construction

Candidate universe = same as `build_val_r4.py`:
- Source: `scanner_hit_catalog.json`, confirmed scanner hit (`scanner_hit_ts_ns` not null)
- Val split only: date in `[2023-11-17, 2024-07-23)` � test split (>= 2024-07-23) excluded by range
- `mom_pct >= 50`, `trades.parquet` present, `prev_close` & `scanner_hit_price` present
- MDR>=200 diagnostic sample excluded (matches val_r4's universe)
- **NOT stratified, NOT sampled** � every qualifying event is included

## Exclusion / skip accounting

| Reason | Count |
|---|---:|
| val_r4 events excluded (held-out check) | 100 |
| MDR>=200 diagnostic events excluded | 100 |
| catalog: no scanner hit | 5609 |
| catalog: date outside val split | 64 |
| missing prev_close / scanner_hit_price | 0 |
| mom_pct < 50 (post-lookup) | 0 |
| **scanner hit in val range but trades.parquet MISSING** | 0 |

Expected sanity check: val_r4 excluded = 100 (should be 100 � all of val_r4 came from this pool).
Candidate pool that val_r4 sampled from = VAL-FULL (522) + val_r4 (100) = 622.

## Stratum distribution (natural, unbalanced)

| Stratum | Range (mom_pct) | n | % |
|---|---|---:|---:|
| low | [50, 64.76) | 174 | 33.3% |
| mid | [64.76, 95.13) | 173 | 33.1% |
| high | [95.13, inf) | 175 | 33.5% |

## Escalation check

Pool >= 300 (no escalation).

## Run-time processing outcome (T2 run)

Of the 522 pooled events, the runner processed **491**; the remainder could not
produce a tradeable result and are effectively excluded from full-pool metrics:

| Outcome | Count | Note |
|---|---:|---|
| Processed (produced 0+ trades) | 491 | |
| Skipped | 24 | `no_t_event` / `insufficient_trades` / `insufficient_quotes` — same skip reasons as val_r4's build |
| Errored | 7 | **missing `quotes.parquet`** (SKYE, SMTK, BTTC, ALMU, ATLN, PSIX + 1) — a missing-file issue discovered at run time |

Effective traded sample: **311 trades from 491 processed events**. Runtime ~88 min
(8 workers). `assert_split_valid` passed (no test-split leakage).
