# Phase R1.5 — Time Gate Implementation

## Motivation

DIAG-MFE-MAE Chart 7 (mean open P&L ± IQR bands, p=0.65 arm) showed the winner and loser
P&L bands overlap for roughly the first ~500 seconds after entry and only diverge cleanly
after that point. Before the divergence the two classes occupy the same P&L space, so no
*price* stop can separate them: the DIAG-MFE-MAE stop simulation confirmed this — every
candidate price stop produced either a false-stop rate > 25% (Option 1 buffers: 60–84%;
Option 2 X≤0.40: 36–72%) or a PF Δ worse than −0.90 (the single PF-improving level, Option 1
buffer=0.00, stopped 84% of winners). A *time* gate exploits the divergence point instead:
if a trade has not established positive open P&L by T_gate seconds, cut it; otherwise EPG
governs the winner for the rest of the hold. The geometry that motivates this: 81% of losing
trades reached their MFE *before* their MAE (they pop early, then bleed down), and losers'
worst tick lands late (mean t_mae_relative 0.59) while winners dip early then run (mean
t_mfe_relative 0.57).

## Mechanism

Single check per trade. At the first tick where `t_since_entry >= T_gate`, the gate reads
open P&L = `(price[i] − entry_price) / entry_price`:
- If `open_pnl < 0.0` → exit immediately at the current tick (`exit_reason = "time_gate"`).
- If `open_pnl >= 0.0` → the check is marked done and never runs again; EPG window close
  governs the remainder of the hold (including the same tick).

Once the check has run (whether or not it fired), `t_gate_checked` is `True` and the branch
is skipped for all later ticks of that trade.

## Implementation

File: `backtest/runner_rapid.py`
Location: inside the `elif in_position:` block, as the first conditional *before* the EPG
window close check.
New variable: `t_gate_sec` (float | None) — loaded from `args` at worker init alongside
`max_entry_lag_sec`; `None` = disabled.
New variable: `t_gate_checked` (bool) — initialized `False` in the per-event state section
(alongside `in_position`, `closed_today`, `entry_t_sec`), before the entry/exit loop.
New exit reason string: `"time_gate"`.
Parameter: `--t-gate-sec` CLI arg, `type=float`, `default=None`; threaded into the work item
as `"t_gate_sec"`.

The time-gate exit books a trade record with the **same fields and structure** as the EPG
window close exit (no separate booking path). Per spec, the time gate uses the **current
tick** price/timestamp (`td.prices[i]`, `td.t_sec[i]`) for the fill — not the next-tick fill
the EPG exit uses — because the gate decision and the fill are the same observation. It sets
`in_position = False` and `break`s the tick loop (re-entry is disabled, so nothing further can
happen for the event).

Exit stack priority while in position: (1) time gate → (2) EPG window close → (3) session end.
The time gate check is evaluated before the EPG close check on the same tick.

`open_pnl_at_gate_check` is not stored in the runner record (to keep the trade-dict schema
uniform); for a `time_gate` exit it equals `pnl_pct / 100` by construction (the fill is the
check tick), and is reconstructed in post-processing for `false_gate_analysis.json`.

## Backward compatibility

When `t_gate_sec is None` (the default), the new `if` is short-circuited on its first clause
and no code path changes. R1-Fixed results are fully reproducible.

## Prior results

`backtest/results/phase_r1_fixed/` — UNTOUCHED (baseline for false/true-gate joins).
New results: `backtest/results/phase_r15/`.
