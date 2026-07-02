# T3 — Test A: MAE-gap disaster stop

Stop level = **-9.59%** (T2's 95th-pct winner MAE — single stated level, not swept).
Recomputed from the stored MAE envelope (T1). No parquet re-pull.

**Charts:** [stop_pnl_kde.html](charts/stop_pnl_kde.html) · [stop_comparison_bar.html](charts/stop_comparison_bar.html)

## T3a — Counts

- Trades cut by the stop: **15** of 65
- **False positives (winners cut): 2** — ['BNED 2024-05-17 (+25.8%→-9.8%)', 'AMST 2024-04-24 (+9.6%→-15.7%)']
- True positives (losers cut): **13**

⚠ **ESCALATION FLAG**: false-positive count = 2 (> 1) — 2 eventual winners round-trip through -9.59% before recovering; the MAE gap is
real but not perfectly clean (T2b already flagged this).

## Metrics

| config | n | PF | WR% | mean PnL% | CVaR5% |
|---|---|----|----|-----------|--------|
| Baseline (no stop) | 65 | 1.7557 | 50.77 | 2.343 | -21.76 |
| **−9.59% stop** | 65 | **1.3871** | 47.69 | 1.368 | **-19.69** |
| T_gate=300 (ref) | 65 | 1.9141 | 44.62 | 2.369 | -14.53 |

**CVaR5 +2.07pp, WR -3.08pp, mean -0.975pp vs baseline.**

## Verdict — NEGATIVE: the MAE gap is a mirage for a fixed stop

The stop **hurts** (PF 1.7557→1.3871, mean 2.343→1.368) and barely
touches the tail (CVaR5 only +2.07pp). Of the 15 cut trades,
**10 were made worse and only 5 better** — the stop destroyed
**107.0pp** of PnL through premature cuts. Root cause: **MAE is transient.** 7
losers dipped through -9.59% intraday but *recovered* to a shallower final loss (median loser final
-4.34%); the stop converts those recoverable dips into locked-in ~-9.59% losses. It
genuinely rescues only **6** deep-tail trades — not enough to offset the
7 shallow losers + 2 winners it clips. Winners rarely draw deep (T2),
but losers frequently draw deep *and bounce* — so "how deep price goes" does not separate the two at
exit. **Do not adopt a fixed MAE-gap stop.**
