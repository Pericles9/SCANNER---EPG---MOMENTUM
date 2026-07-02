# T5 — Test C: Spread-width entry guard

**Chart:** [spread_gap_kde.html](charts/spread_gap_kde.html) (T5a diagnostic)

## T5a — Do winners and losers differ on entry spread? NO.

| group | n | median entry spread% |
|---|---|---|
| Winners | 33 | 1.143% |
| Losers | 32 | 1.020% |

Mann–Whitney **p = 0.738**, rank-biserial effect **0.049** — no real difference. Winners'
median spread is actually marginally *wider* than losers'. The deepest tail losses had **tight**
spreads at entry (CNSP −27.4% @ 0.56%, BENF −12.5% @ 0.57%), so wide entry spread does not flag the
tail — if anything the worst trades looked liquid at entry.

## T5b / T5c — SKIPPED (escalation rule)

Per the phase escalation rule ("T5a finds no real spread difference → skip T5b/T5c, report null,
do not force a threshold"), no guard threshold is proposed and no filtered re-simulation is run.
**Null result.** A spread guard would exclude trades essentially at random with respect to outcome.
