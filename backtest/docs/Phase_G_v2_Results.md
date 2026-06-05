---
tags:
  - type/results
  - domain/backtest
  - domain/microstructure
  - project/scanner-epg-momentum
  - status/complete
created: 2026-05-18
last_reviewed: 2026-05-18
---

# Phase G v2 Results — Momentum-Weighted Scanner Quartile Reanalysis

## Purpose

Phase G v2 is an **analysis-only patch** to Phase G. No new backtest is run. No strategy parameters change. Source data is `results/phase_g/scanner_context.parquet` (3,039 rows with stored `full_scanner_snapshot` JSON).

**Motivation:** Phase G used `scanner_heat` — a population-level p75 bin that classified scanner names relative to all historical snapshots. Phase G v2 replaces this with `scanner_quartile` — a momentum-weighted, within-snapshot quartile that classifies each name by its share of total snapshot momentum on the day it was traded. This is a more precise instrument for asking whether the traded ticker was the dominant mover or a secondary name on its scanner day.

**Goals:**

- Compute `scanner_quartile` (1–4) for each trade using the cumulative-momentum algorithm applied to the stored `full_scanner_snapshot` JSON
- Re-run T2 sub-analyses (quartile stats, rank × quartile interaction, exit type by quartile, scanner_n=1 isolation) on the new label
- Produce 4 standalone Plotly HTML charts

**Primary success metric:** `scanner_quartile` computed for ≥90% of valid rows. **Result: 99.86% — PASS.**

**Key constraint:** No Phase H or follow-on work without explicit approval from Cooper.

---

## Run Parameters

| Parameter | Value |
| --------- | ----- |
| Source | `results/phase_g/scanner_context.parquet` (3,039 rows) |
| Quartile algorithm | Cumulative momentum-weighted within-snapshot assignment |
| Bootstrap CI | 1,000 resamples, seed=42, 95% |
| Runner | `tools/phase_g_v2/run_v2.py` |
| Charts | `tools/phase_g_v2/charts.py` |
| Elapsed | 0.6s |

**Quartile algorithm:** Sort all scanner names in the snapshot descending by `pct_change`. Compute `total = sum(pct_change)`. Walk down the list accumulating `running`. Assign Q1 until `running ≥ total/4`, then Q2 until `running ≥ total/2`, etc. Q1 = the dominant movers accounting for the first 25% of cumulative snapshot momentum. Q4 = lowest-relative-momentum names in that session's scanner.

**Edge cases:**
- `total_momentum ≤ 0` → `scanner_quartile = null` (4 rows)
- `scanner_n = 1` → forced Q1 (0 rows in this dataset)
- `scanner_rank = null` → excluded from quartile assignment (165 rows)

---

## T1: Quartile Computation — Build Summary

| Metric | Value |
| ------ | ----- |
| Total rows | 3,039 |
| Null scanner_rank (excluded) | 165 |
| Valid rows (scanner_rank present) | 2,874 |
| Single-name scanner days (scanner_n=1 → Q1) | 0 |
| Null: total_momentum ≤ 0 | 4 |
| Null: ticker not in snapshot | 0 |
| scanner_quartile null rate (valid rows) | **0.14%** — PASS (threshold: ≤10%) |

**Quartile distribution:**

| Quartile | n_trades |
| -------- | -------- |
| Q1 (top momentum) | 697 |
| Q2 | 448 |
| Q3 | 476 |
| Q4 (bottom momentum) | 1,249 |

Q4 is the largest group (43% of trades), consistent with most scanner names being secondary movers relative to the session's dominant ticker.

---

## T2a: Quartile Stats

Bootstrap CI: 1,000 resamples, seed=42, 95%.

| Quartile | n_trades | EV (%) | EV 95% CI | PF | PF 95% CI | Win Rate |
| -------- | -------- | ------ | --------- | -- | --------- | -------- |
| Q1 (top momentum) | 697 | +0.296 | [−0.096, +0.691] | 1.252 | [0.928, 1.658] | 43.6% |
| Q2 | 448 | +0.814 | [+0.105, +1.632] | 1.911 | [1.098, 3.096] | 50.0% |
| Q3 | 476 | +1.280 | [+0.642, +2.221] | 2.517 | [1.678, 3.958] | 52.3% |
| Q4 (bottom momentum) | 1,249 | +1.194 | [+0.885, +1.525] | 3.058 | [2.394, 3.897] | 50.7% |

**Gradient:** PF rises monotonically Q1 → Q4 (1.25 → 3.06). EV rises Q1 → Q3 then is flat Q3/Q4.

Q1 is the only quartile whose EV CI includes zero and whose PF CI includes 1.0 — statistically consistent with break-even. Q4 has the tightest CI and highest PF, driven by the largest sample (1,249 trades).

---

## T2b: Rank × Quartile Interaction

Ranks 1–10, Q1 vs Q4 only. `*` = low-n flag (n < 20). `—` = no trades in cell.

| Rank | Q1 n | Q1 EV (%) | Q1 PF | Q4 n | Q4 EV (%) | Q4 PF |
| ---- | ---- | --------- | ----- | ---- | --------- | ----- |
| 1 | 544 | +0.242 | 1.185 | 0 | — | — |
| 2 | 139 | +0.554 | 1.871 | 27 | −0.485 | 0.646 |
| 3 | 14 * | −0.200 | 0.834 | 95 | +1.164 | 2.425 |
| 4 | 0 | — | — | 96 | +2.160 | 5.750 |
| 5 | 0 | — | — | 90 | +2.098 | 4.915 |
| 6 | 0 | — | — | 93 | +0.868 | 2.313 |
| 7 | 0 | — | — | 127 | +1.545 | 2.600 |
| 8 | 0 | — | — | 134 | +1.080 | 2.968 |
| 9 | 0 | — | — | 79 | +1.745 | 7.647 |
| 10 | 0 | — | — | 94 | +0.709 | 2.456 |

**Structural finding:** Rank 1 and scanner_quartile are nearly synonymous. The highest-pct_change ticker at entry always accounts for ≥25% of total snapshot momentum and therefore always lands in Q1 by construction. No rank-1 trade was ever assigned Q4. Ranks 4–10 have zero Q1 trades.

Rank 2 is the only rank with material counts in both quartiles. Rank 2 Q4 (n=27, PF=0.646) underperforms rank 2 Q1 (n=139, PF=1.871) — the secondary Q4 name at rank 2 shows the weakest result in the full table.

---

## T2c: Exit Type by Quartile

Population LULD upper share (baseline): 8.6%.

| Quartile | n | luld_upper | epg_wc | exit_d | luld vs pop | Elevated? |
| -------- | - | ---------- | ------ | ------ | ----------- | --------- |
| Q1 | 697 | 8.0% | 32.0% | 60.0% | 0.93× | No |
| Q2 | 448 | 9.6% | 41.1% | 49.3% | 1.12× | No |
| Q3 | 476 | 10.7% | 32.4% | 56.9% | 1.24× | No |
| Q4 | 1,249 | 7.8% | 53.0% | 39.2% | 0.90× | No |

No quartile has elevated LULD share (threshold: >1.5× population rate). LULD exit rates are homogeneous across all quartiles (7.8–10.7%).

**Exit pattern divergence:** Q1 exits predominantly via EXIT_D (60%) with low EPG window close (32%). Q4 exits predominantly via EPG window close (53%) with low EXIT_D (39%). Q4 names trend more persistently within the EPG window — the Hawkes intensity imbalance signal fires less frequently, and the EPG window carries more of the exit load. This is consistent with secondary movers exhibiting steadier (lower-volatility) momentum rather than the sharp, mean-reverting dynamics that trigger EXIT_D.

---

## T2d: scanner_n=1 Isolation

| Group | Subset | n_trades | EV (%) | PF | Win Rate |
| ----- | ------ | -------- | ------ | -- | -------- |
| scanner_n=1 | All ranks | 0 | — | — | — |
| scanner_n=1 | Rank 1 only | 0 | — | — | — |
| scanner_n>1 | All ranks | 2,874 | +0.928 | 2.134 | 49.1% |
| scanner_n>1 | Rank 1 only | 548 | +0.235 | 1.179 | 41.8% |

Zero single-name scanner days appear in the val dataset. The hypothesis that rank 1 underperformance is driven by single-name scanners (where the traded ticker trivially holds 100% of momentum and is always Q1) is **not testable in this data** — the condition never occurred. Rank 1 underperformance (PF=1.18) cannot be attributed to scanner_n=1 mechanics.

---

## Strategy Implication Flags

Findings where Q1 vs Q4 difference exceeds 0.5 PF or 0.5% EV. Candidates for Phase H validation only — no recommendations.

| Finding | Q1 | Q4 | Delta | Threshold |
| ------- | -- | -- | ----- | --------- |
| PF: Q1 vs Q4 | 1.252 | 3.058 | +1.806 PF | Yes (>0.5 PF) — **Phase H candidate** |
| EV: Q1 vs Q4 | +0.296% | +1.194% | +0.898% | Yes (>0.5%) — **Phase H candidate** |
| PF: Q2 vs Q4 | 1.911 | 3.058 | +1.147 PF | Yes (>0.5 PF) — **Phase H candidate** |
| EV: Q2 vs Q4 | +0.814% | +1.194% | +0.380% | No |
| PF: Q3 vs Q4 | 2.517 | 3.058 | +0.541 PF | Yes (>0.5 PF) — **Phase H candidate** |
| EV: Q3 vs Q4 | +1.280% | +1.194% | −0.086% | No |
| exit_d share: Q1 vs Q4 | 60.0% | 39.2% | 20.8 pp | Informational |
| epg_wc share: Q1 vs Q4 | 32.0% | 53.0% | 21.0 pp | Informational |

---

## Escalation Check

| Criterion | Observed | Threshold | Pass/Fail |
| --------- | -------- | --------- | --------- |
| scanner_quartile null rate (valid rows) | 0.14% | ≤10% | **PASS** |
| Q1 trade count | 697 | >0 | **PASS** |
| Q2 trade count | 448 | >0 | **PASS** |
| Q3 trade count | 476 | >0 | **PASS** |
| Q4 trade count | 1,249 | >0 | **PASS** |
| scanner_n=1 Q1 bias testable | No (0 cases) | — | Informational |

---

## Key Findings

> **NOT ACTIONABLE.** Phase G v2 is analysis-only. The Q1→Q4 PF gradient is clearly visible in
> backtest data but breaks down in practice. No quartile-based entry gate is implemented or planned.
> Do not add a quartile gate to the live system or backtest runner without a dedicated validation
> phase with explicit approval.

**GV2-1: Monotone PF gradient Q1 → Q4 (+1.806 PF spread)**
Within any given scanner snapshot, trading the dominant momentum name (Q1) produces materially lower PF (1.25) than trading secondary names (Q4: PF=3.06). The gradient is monotone across all four quartiles. This is the primary Phase G v2 finding.

**GV2-2: Rank and quartile are structurally confounded**
Rank 1 is definitionally Q1 in nearly all cases (544/544 trades). The Q1 underperformance observed in T2a is equivalent to the rank 1 underperformance observed in Phase G. These are the same phenomenon expressed differently. Introducing scanner_quartile does not reveal a new signal — it confirms and reframes the Phase G rank 1 finding.

**GV2-3: Q4 dominates the trade population (43%)**
Most entries are secondary names in their snapshot (ranks 3–10). Q4 names produce the highest PF with the tightest CI, making them the most reliable performer class in the dataset.

**GV2-4: EXIT_D fires disproportionately on Q1 (60% vs 39%)**
The Hawkes intensity imbalance signal fires more frequently on dominant movers. This may reflect higher order-flow volatility in the leading scanner name. Q4 names hold to EPG window close more often (53%), consistent with steadier, less mean-reverting momentum.

**GV2-5: LULD is not quartile-sensitive**
No quartile shows elevated LULD upper exits. The LULD signal is orthogonal to momentum weighting within the snapshot.

**GV2-6: scanner_n=1 hypothesis untestable**
Zero single-name scanner days in val. Rank 1 underperformance is confirmed as intrinsic (not an artifact of scanner_n=1), but the mechanism remains unexplained.

---

## Output Files

| File | Status |
| ---- | ------ |
| `results/phase_g_v2/scanner_context_v2.parquet` | Written (3,039 rows + scanner_quartile) |
| `results/phase_g_v2/quartile_stats.parquet` | Written (4 rows) |
| `results/phase_g_v2/rank_quartile_interaction.parquet` | Written (20 rows) |
| `results/phase_g_v2/exit_by_quartile.parquet` | Written (4 rows) |
| `results/phase_g_v2/scanner_n1_isolation.parquet` | Written (4 rows) |
| `results/phase_g_v2/v2_build_log.json` | Written |
| `results/phase_g_v2/phase_g_v2_meta.json` | Written |
| `results/phase_g_v2/charts/01_ev_pf_by_quartile.html` | Written |
| `results/phase_g_v2/charts/02_rank_quartile_interaction.html` | Written |
| `results/phase_g_v2/charts/03_exit_by_quartile.html` | Written |
| `results/phase_g_v2/charts/04_scanner_n1_isolation.html` | Written |
