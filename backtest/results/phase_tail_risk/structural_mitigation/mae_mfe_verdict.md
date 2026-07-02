# T2 — MAE/MFE Gap Diagnostic

Reads `raw_trajectories.json` (T1). No new extraction. Baseline = R1-Final sym_p80, no gate.

**Charts:** [mae_gap_kde.html](charts/mae_gap_kde.html) · [mae_pnl_scatter.html](charts/mae_pnl_scatter.html)

## T2a — Winners' drawdown boundary vs tail-loser endpoints

Winner MAE (deepest drawdown reached before recovering to a winning exit), n=33:

| stat | value |
|---|---|
| median | -1.69% |
| mean | -3.04% |
| 90th-pct depth (90% of winners stayed shallower) | -6.37% |
| 95th-pct depth | -9.59% |
| 99th-pct depth | -17.26% |
| deepest single winner | -19.92% |
| winners drawing below −10% | 2/33 |
| winners drawing below −15% | 1/33 |

Tail-loser final PnL (5 worst): [-27.37, -19.12, -18.8, -15.77, -12.47] — loser final median -4.34%,
loser MAE median -8.98%.

## T2b — Verdict: does a clean, low-cost gap exist?

**Yes, a gap exists — but it is real, not perfectly clean.** 95% of winners never draw below
**-9.59%**, and the median winner only dips to -1.69%.
Tail losers, by contrast, end at −18% to −27% and pass through much deeper MAE
(median loser MAE -8.98%) on the way down. A disaster stop at the
**95th-percentile winner drawdown = -9.59%** (diagnostic-derived, not swept) would breach
**2 of 33 winners** (false cuts) and **13 of 32 losers** (true cuts).

The gap is not free: 2 winners genuinely round-trip below −10%
(the deepest to -19.92%) before recovering, so any stop shallow enough to
catch most losers will clip a few real winners. T3 tests this exact -9.59% level and reports
the false-positive count honestly.
