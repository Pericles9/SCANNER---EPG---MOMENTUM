---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/wip
created: 2026-06-30
phase: REBUILD-VAL T1
---

# T1 — mom_pct Distribution + Candidate Cutpoints

## T1a — Column Confirmation

**Confirmed column name:** `momentum_pct`

Source file: `D:\Trading Research\data\momentum_events\filtered_events_power_law_q05.parquet`

**Definition:** `momentum_pct = (event_high / prev_close − 1) × 100` — a **percentage gain**,
not a ratio. The plan preamble describes it as "extended_session_high / prev_close (a ratio;
values like 66.62 mean the extended high was 66.62× the prior close)" — this is incorrect.
`momentum_pct = 66.62` means the extended high was **66.62% above** prev_close (i.e., the
ratio is 1.6662×, not 66.62×). Verified against parquet data: event_high=3499.51,
prev_close=1284.39 → (3499.51/1284.39−1)×100 = 172.46 = stored momentum_pct.

Folder-name encoding: the `filtered/` directories use `{TICKER}_{DATE}_{mom_pct_2dp}` where
the trailing number is the same percentage (e.g., `AAPL_2024-01-15_66.62` = 66.62% gain).

---

## T1b — Full Distribution (all 23,268 rows in momentum_events)

| Statistic | momentum_pct (% gain) |
|-----------|----------------------|
| n_events  | 23,268               |
| mean      | 2,386.00             |
| min       | 30.00                |
| p10       | 31.66                |
| p25       | 34.78                |
| p50       | 42.97                |
| p75       | 64.00                |
| p90       | 109.86               |
| p95       | 167.61               |
| p99       | 400.07               |
| max       | 53,799,900           |

- n with momentum_pct < 2.0 (near-zero movers): **0**
- n with momentum_pct > 5,000 (>51× ratio equivalent): **13**

The full table is extremely right-skewed. Median is 43% (the stock went up 43% from prev_close
to extended session high). The mean is inflated by 13 extreme outliers with multi-million
percent gains (sub-penny stocks).

---

## Candidate Pool Statistics

The candidate pool is built using the same logic as `build_val_r3.py`:
- `scanner_hit_catalog.json` records with `scanner_hit_ts_ns` present
- Val split: `2023-11-17 ≤ date < 2024-07-23`
- `list_events(min_mom=50.0)` cross-reference — excludes events without `trades.parquet`
- MDR≥200 exclusion applied (100 events removed)

**Candidate pool: 622 events**

| Statistic | momentum_pct (% gain) |
|-----------|----------------------|
| n_events  | 622                  |
| mean      | 105.76               |
| min       | 50.00                |
| p10       | 53.18                |
| p25       | 59.77                |
| p33       | 64.76                |
| p50       | 76.27                |
| p67       | 95.13                |
| p75       | 106.43               |
| p90       | 152.29               |
| p95       | 236.35               |
| p99       | 626.96               |
| max       | 1,609.37             |

Bracket summary:

| Range          | n events |
|----------------|----------|
| [50, 100)      | 437      |
| [100, 200)     | 143      |
| [200, 500)     | 32       |
| [500, 1000)    | 8        |
| [1000, ∞)      | 2        |

---

## T1c — Cutpoint Candidates

### Tercile split (p33/p67 of candidate pool)

Cutpoints: **p33 = 64.8**, **p67 = 95.1**

| Stratum | Range            | n events |
|---------|------------------|----------|
| low     | [50, 64.8)       | 204      |
| mid     | [64.8, 95.1)     | 213      |
| high    | [95.1, ∞)        | 205      |

---

### Fixed-cutpoint candidates

| Cut_A | Cut_B | n_low | n_mid | n_high | Notes                         |
|-------|-------|-------|-------|--------|-------------------------------|
| 100   | 200   | 437   | 143   | 42     | Closest to classic tier split |
| 80    | 150   | 339   | 220   | 63     | Shift down one tier           |
| 100   | 300   | 437   | 165   | 20     | Widen high stratum            |
| 75    | 200   | 306   | 274   | 42     | Lower low boundary            |
| 60    | 150   | 159   | 400   | 63     | Tighter low boundary          |

For a 50/30/20 target allocation, cutpoints must produce pool sizes ≥ 50, ≥ 30, ≥ 20
respectively to allow clean sampling (before missing-file exclusion). For 40/35/25, same logic.

---

## Hard Stop

No recommendation made. Cooper must select cutpoints and T_gate scope before T2 proceeds.
