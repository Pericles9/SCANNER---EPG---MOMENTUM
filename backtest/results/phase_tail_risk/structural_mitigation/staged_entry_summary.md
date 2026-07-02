# T4 — Test B: Staged entry

Half at entry, second half at **T=60s if gate still PASS** (fixed checkpoint, not optimized;
in the no-gate baseline gate PASS throughout ⇒ confirmation ≡ hold≥60s). Reads `raw_trajectories.json`.

**Charts:** [staged_comparison_bar.html](charts/staged_comparison_bar.html) · [staged_confirm_rate_bar.html](charts/staged_confirm_rate_bar.html)

## Metrics (1 unit intended capital per trade)

| config | n | PF | WR% | mean PnL% | CVaR5% |
|---|---|----|----|-----------|--------|
| Baseline (full size) | 65 | 1.7557 | 50.77 | 2.343 | -21.76 |
| Staged entry | 65 | 1.4742 | 49.23 | 1.538 | -24.2 |
| T_gate=300 (ref) | 65 | 1.9141 | 44.62 | 2.369 | -14.53 |

## T4a — Confirmation rates & EV cost

- Winners receiving 2nd tranche: **91%** (30/33)
- Losers receiving 2nd tranche: **94%** (30/32)
- Mean staged PnL: confirmed **+1.65%** vs unconfirmed (half-size) **+0.14%**
- Of 5 unconfirmed trades, 3 were eventual winners left at half size —
  EV given up on them ≈ **0.9pp**.

**Verdict:** Staged entry does NOT do useful selective work — winners and losers confirm at nearly the same rate, so it just uniformly halves exposure. CVaR5 -2.44pp, mean -0.805pp vs baseline.
Because losers confirm at **94%** (vs winners **91%**), the second-tranche
gate does not separate the two populations — the deep tail losers
hold >60s and get the full blended position, so the tail is reduced.
