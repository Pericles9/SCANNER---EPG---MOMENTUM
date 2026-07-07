# Phase VAL-FULL — Summary

EPG-Rapid full-pool confirmation of the locked val_r4 p=0.80 config on a larger, independent held-out pool. No config changes; confirmation, not re-tuning.

## Locked config (unchanged)

- entry_mode=`first_pass`, p_open=p_close=`0.8`, max_entry_lag_sec=`500.0`, t_gate_sec=`None` (no time gate), LULD/EXIT_D off.
- Exit stack observed: ['epg_window_close', 'session_end'] (drift: NONE).

## T2 — Full-pool headline

| n_trades | PF | WR% | mean PnL% | median PnL% | CVaR5% |
|---:|---:|---:|---:|---:|---:|
| 311 | 1.0188 | 36.98 | 0.0834 | -1.0216 | -22.1742 |

Pool = 522 events. Chart: `charts/pnl_kde_full.html`

## T3 — Session breakdown

| Session | n | PF | WR% | mean% | CVaR5% |
|---|---:|---:|---:|---:|---:|
| RTH | 151 | 0.8295 | 36.42 | -0.6539 | -22.9598 |
| Pre-Market | 150 | 1.2634 | 38.0 | 1.2265 | -18.239 |
| Post-Market | 10 | 0.4213 | 30.0 | -5.9292 | -32.1267 |

**T3a verdict — RTH/pre PF gap: INVERTS — the val_r4 RTH>pre edge reverses; RTH PF collapses 2.5163->0.8295 (below 1.0) while pre-market holds (1.2951->1.2634).** val_r4 RTH 2.5163 / PRE 1.2951 (Δ1.221); val-full RTH 0.8295 / PRE 1.2634 (Δ-0.434). Charts: `charts/session_kde.html`, `charts/session_bar.html`

## T4 — Stratum × session

| Stratum | Session | n | PF | WR% | CVaR5% |
|---|---|---:|---:|---:|---:|
| low | RTH | 54 | 0.9834 | 38.89 | -15.328 |
| low | Pre-Market | 28 | 0.5734 | 35.71 | -16.4589 |
| mid | RTH | 54 | 0.55 | 29.63 | -21.2102 |
| mid | Pre-Market | 49 | 0.7502 | 28.57 | -15.245 |
| high | RTH | 43 | 1.0829 | 41.86 | -31.8278 |
| high | Pre-Market | 73 | 1.7783 | 45.21 | -20.9212 |

Blended per stratum (full pool):
- **low**: val-full PF=0.7457 (n=88) vs val_r4 PF=0.3699 (n=17)
- **mid**: val-full PF=0.6239 (n=106) vs val_r4 PF=2.0232 (n=27)
- **high**: val-full PF=1.4412 (n=117) vs val_r4 PF=3.2796 (n=21)

**T4a verdict — low-stratum underperformance: HOLDS — low stratum remains a loser (PF<1) at full n** (val_r4 low PF=0.3699 n=17; val-full low PF=0.7457 n=88).

Broader stratum picture (full pool): only **high** clears PF>=1.0. Both low (PF=0.7457) and mid (PF=0.6239) are net losers; high (PF=1.4412) carries the pool, driven by high·pre-market (PF=1.7783). Chart: `charts/stratum_session_bar.html`

## T5 — Generalization verdict

**DEGRADED (>30% relative PF drop — flag)** — PF relative change -42.0%. See `comparison_summary.md`, chart `charts/valr4_vs_valfull_bar.html`.

## Escalation check

| Condition | Threshold | Result |
|---|---|---|
| Pool size after exclusions | < 300 | 522 events — OK |
| Exit reason other than epg_window_close/session_end | any | none — OK |
| Full-pool PF vs val_r4 PF | >30% relative | -42.0% — FLAG |
| Test-split boundary in pool | any | none (runner assert_split_valid passed) — OK |

## Output files

- `pool_definition.md` — T1 pool size, exclusions, missing-file log
- `full_pool_results.json` — T2 headline + run_config + per-trade
- `per_trade_val_full.json` — T6 downstream per-trade (enriched w/ stratum)
- `charts/pnl_kde_full.html` — T2 full-pool PnL KDE
- `charts/session_kde.html` — T3 RTH vs pre KDE
- `charts/session_bar.html` — T3 PF/WR/CVaR5 bars
- `charts/stratum_session_bar.html` — T4 6-cell cross-tab
- `charts/valr4_vs_valfull_bar.html` — T5 comparison bars
- `comparison_summary.md` — T5 verdict
- `findings.json` — machine-readable findings
- `summary.md` — this file