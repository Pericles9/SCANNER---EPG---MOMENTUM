# Phase R0 — MDR≥150 Diagnostic Baseline

**Date:** 2026-06-22
**Sample:** 100 events from `data/val_mdr150_diagnostic.json` (MDR≥150, confirmed scanner hit, val split, seed=0)
**Exit stack:** EPG window close only. EXIT_D off. LULD off.
**Scanner floor:** active. All entries at or after `t_scanner_hit_sec`.

---

## T1 — Unit Tests

378 tests passed. 0 failures.

---

## T2 — Entry Path Audit

`entry_eligible()` absent from `first_pass` and `rising_edge` paths. Hard assertion at
`runner_rapid.py:714–717` enforces this at runtime.

---

## T3 — Parity Check

84 events processed (16 skipped — no_t_event / missing data), 1 pre-existing data error
(ATLN 2024-06-26: missing quotes.parquet). Parity mode delegates directly to
`runner._process_event` — output is identical to classic runner by construction.

---

## T4 — Baseline (rising-edge first-PASS, MDR≥150)

| Metric | Value |
|---|---|
| n_trades | 93 |
| events processed | 96 (4 skipped: no_t_event) |
| profit_factor | **3.1999** |
| win_rate | 49.46% |
| mean_pnl_pct | 9.42% |
| median_pnl_pct | 0.0% |
| CVaR5 | −32.46% |
| mean_hold_sec | 936s |
| median_hold_sec | 754s |
| mean_entry_lag_from_scanner | 2901s |
| median_entry_lag_from_scanner | 658s |
| p90_entry_lag_from_scanner | 8613s |
| gate PASS at scanner hit | 29.2% |
| exit reasons | epg_window_close: 100% |

---

## T5 — Rapid (first-PASS, MDR≥150)

| Metric | Value | vs T4 baseline |
|---|---|---|
| n_trades | 96 | +3 |
| events processed | 96 (4 skipped) | = |
| profit_factor | **2.97** | −0.23 |
| win_rate | 55.21% | +5.8pp |
| mean_pnl_pct | 10.14% | +0.72pp |
| median_pnl_pct | 2.18% | +2.18pp |
| CVaR5 | −34.91% | −2.45pp |
| mean_hold_sec | 1090s | +154s |
| median_hold_sec | 898s | +144s |
| mean_entry_lag_from_scanner | 2181s | −720s |
| median_entry_lag_from_scanner | 306s | −352s |
| p90_entry_lag_from_scanner | 7380s | −1233s |
| gate PASS at scanner hit | 29.2% | = |
| exit reasons | epg_window_close: 100% | = |

**No escalation.** PF=2.97 >> 1.00 threshold.

---

## T6 — Pre-Scanner Audit

T4: 0 negative lags (93/93 trades ≥ 0).
T5: 0 negative lags (96/96 trades ≥ 0).
**Scanner floor intact.**

---

## T7 — Entry Lag Distribution (T5 rapid)

`t_entry_sec − t_scanner_hit_sec`, n=96

| Percentile | Lag |
|---|---|
| p0 | 0s |
| p5 | 0s |
| p10 | 0s |
| p25 | 0s |
| p50 | 306s |
| p75 | 719s |
| p90 | 7380s |
| p95 | 14665s |
| p99 | 20631s |
| p100 | 30451s |
| mean | 2181s |

- **36% of entries within 60s** (gate already PASS at scanner hit → near-instant entry)
- **48% within 300s** (5 min)
- **21% beyond 1800s** (30 min)
- **17% beyond 3600s** (1 hour)

The distribution is strongly bimodal: a near-instant cluster (~36%) where the gate is
already PASS at scanner hit, and a long-tail cluster where the gate opens late.

**Cooper sets `max_entry_lag_sec` before R1.** Starting point from spec: 180s.
Profit stats per threshold (cumulative: all trades with lag ≤ threshold):

| `max_entry_lag_sec` | n | PF | win% | mean PnL | median PnL | CVaR5 | median lag |
|---|---|---|---|---|---|---|---|
| 60s | 35 | 2.838 | 54.3% | 10.03% | 1.73% | −31.34% | 0s |
| 180s | 38 | 2.661 | 52.6% | 9.25% | 1.54% | −31.34% | 0s |
| 300s | 46 | 2.811 | 56.5% | 9.06% | 2.22% | **−27.74%** | 0s |
| 600s | 70 | 2.236 | 52.9% | 6.93% | 1.24% | −32.15% | 92s |
| 1800s | 76 | 2.331 | 52.6% | 7.13% | 1.24% | −32.15% | 206s |
| 3600s | 80 | 2.658 | 53.8% | 9.33% | 1.54% | −34.91% | 264s |
| no filter | 96 | **2.970** | **55.2%** | **10.14%** | **2.18%** | −34.91% | 306s |

**Non-monotonic pattern:** PF is not "smaller threshold = better." The 300–600s band
(24 additional trades) is the worst incremental group — adding it drops PF from 2.81 to
2.24. The >3600s group (16 trades, the very late entries) is the best incremental group
— including them lifts PF from 2.66 to 2.97. Implications:
- A tight filter (≤300s) gives the best CVaR5 (−27.7%) and a good PF (2.81).
- No filter gives the best PF (2.97) and best win% — the late entries are profitable.
- The 600–1800s band is the weak zone; a threshold in that range is the worst choice.

---

## Escalation

No escalation triggered. PF=2.97 > 1.00.

---

## Output Files

| File | Location |
|---|---|
| Baseline metrics | `results/phase_r0_mdr150/baseline_mdr150/run_summary.json` |
| Baseline trades | `results/phase_r0_mdr150/baseline_mdr150/per_trade.json` |
| Rapid metrics | `results/phase_r0_mdr150/rapid_mdr150/run_summary.json` |
| Rapid trades | `results/phase_r0_mdr150/rapid_mdr150/per_trade.json` |
| Parity output | `results/phase_r0_mdr150/parity/run_summary.json` |

Old invalidated results (pre-scanner-floor-fix): `results/phase_r0/` — do not use.
