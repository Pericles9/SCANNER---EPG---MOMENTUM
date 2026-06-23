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
Capture rates at common thresholds:

| `max_entry_lag_sec` | Trades kept | Trades filtered |
|---|---|---|
| 60s | ~36% (35) | ~64% (61) |
| 180s | ~45% (43) | ~55% (53) |
| 300s | ~48% (46) | ~52% (50) |
| 1800s | ~79% (76) | ~21% (20) |
| 3600s | ~83% (80) | ~17% (16) |

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
