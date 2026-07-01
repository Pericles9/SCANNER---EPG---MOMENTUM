---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-07-01
phase: EPG-Rapid-Tail-Risk
reference_config: "sym_p80 · val_r4_stratified · T_gate=500s"
---

# Phase EPG-Rapid-Tail-Risk — Diagnostic Summary

**Reference:** `val_r4_stratified.json`, p_open=p_close=0.80, `max_entry_lag_sec=500`.
n_trades = 65. **No backtest re-run** — all findings from the R1-Final per-trade output
on disk, joined to the stratified sample; tape reads (cumulative volume) are read-only
auxiliary computation. **Chart-first**: every finding links its chart. **No remediation
proposed** — that is a separate decision for Cooper (see Approval Gate).

Index of all charts: [index.html](index.html)

---

## 1 · T0 — Stratum × Session

**Reconciliation with Cooper's blended numbers: PASS (exact).**

| stratum | Cooper (blended) | This run (blended) |
|---|---|---|
| low  | PF=0.37 WR=35% n=17 | PF=0.3699 WR=35.3% n=17 |
| mid  | PF=2.03 WR=56% n=27 | PF=2.0232 WR=55.6% n=27 |
| high | PF=3.28 WR=57% n=21 | PF=3.2796 WR=57.1% n=21 |

**Charts:** [stratum_session_bar.html](charts/stratum_session_bar.html) ·
[stratum_session_kde.html](charts/stratum_session_kde.html)

**Cross-tab (6 cells):**

| cell | n | PF | WR% | mean PnL% | CVaR5% |
|---|---|---|---|---|---|
| low · RTH  | 9  | 1.17 | 67% | +0.47 | (n<10) |
| low · PRE  | 8  | **0.00** | **0%** | −6.63 | (n<10) |
| mid · RTH  | 14 | **14.36** | 86% | +6.93 | −5.90 |
| mid · PRE  | 13 | 0.47 | 23% | −2.35 | −19.12 |
| high · RTH | 10 | 1.32 | 50% | +1.41 | −15.77 |
| high · PRE | 11 | **9.16** | 64% | +10.97 | −7.98 |

**Finding — the stratum story is a session story crossed with stratum.**
The blended low-stratum PF=0.37 is **entirely a pre-market artifact**: low·RTH is fine
(PF=1.17, WR=67%), but all 8 low·PRE trades lose (PF=0.00, WR=0%, mean −6.63%). mid·RTH
is the single best cell (PF=14.36) yet mid·PRE is a loser (PF=0.47). Critically, **high·PRE
is a strong winner** (PF=9.16, WR=64%, mean +10.97%) — pre-market is *not* uniformly bad;
the drag is concentrated in **low+mid pre-market**.

---

## 2 · T1 / T2 — Tail set + pre-market bimodal split

### T1 — CVaR5 tail (n=3, mean = −21.76%)
**Chart:** [global_pnl_kde_tail.html](charts/global_pnl_kde_tail.html) · record: `tail_trades.json`

`_cvar5` matches source (`sorted_pnl[:max(1,int(0.05·n))]`; n=65 → 3 worst).

| ticker | date | stratum | session | mom% | gap% | prev_close | lag(s) | hold(s) | PnL% | halt |
|---|---|---|---|---|---|---|---|---|---|---|
| CNSP | 2024-07-11 | low  | pre  | 57.1 | 30.7 | — | — | — | **−27.37** | no |
| AEMD | 2024-06-06 | mid  | pre  | 89.1 | 31.6 | — | — | — | −19.12 | no |
| CGTX | 2023-12-14 | low  | RTH  | 55.1 | 31.7 | — | — | — | −18.80 | no |

Bottom decile (n=6) adds WLDS (high, RTH, −15.77), BENF (high, RTH, −12.47), HWH (mid, pre, −9.66).
No tail trade overlaps a halt window.

### T2a — bimodal split
**Chart:** [premarket_kde_threeway.html](charts/premarket_kde_threeway.html) · record: `premarket_mode_split.json`

2-component GMM on pre-market PnL% **converges and is preferred** (BIC 258.8 < 270.5 for 1-comp):
loser μ = **−2.92%** (σ 7.9, n=29), winner μ = **+40.59%** (σ 6.2, **n=3**), boundary ≈ +23.8%.
The winner mode is only 3 trades — shown as a rug, not a smoothed curve (small-n honesty).
This matches the phase's described modes (≈−2.68% / ≈+38%, WR≈31% pre-market).

### T2b — three-way overlap
**Chart:** [threeway_venn.html](charts/threeway_venn.html)

| overlap | count | % |
|---|---|---|
| low ∩ CVaR5 tail | 2 | 11.8% of low |
| low ∩ pre-loser  | 8 | 47.1% of low |
| CVaR5 tail ∩ pre-loser | 2 | 66.7% of tail |
| all three | 1 (CNSP) | — |

**Answer to Q4 — three related but distinct populations, not one.** The CVaR5 tail (3) is a
small extreme subset. The pre-market loser mode (29) is the broad loss engine and is **mostly
not low-stratum** (only 8 of 29 are low — it spans mid+high pre-market too). Low stratum (17)
overlaps ~half the pre-loser set. They intersect at CNSP but are not the same thing.

---

## 3 · T3 — Entry-time-knowable contrast

Full KDE matrix (6 features × 3 comparisons): [t3b_continuous_kde/](charts/t3b_continuous_kde/) ·
categorical bars: [t3a_categorical_bars/](charts/t3a_categorical_bars/).

**T3c verdict — the CVaR5 tail is not separable at entry.** Against "tail vs rest", every
entry-knowable continuous feature overlaps almost completely (rank-biserial effect < 0.25;
no MWU p < 0.05). gap% shows a nominal effect (0.61) but on n=3 with p=0.077 — not usable.
This is a **clean null**, consistent with the two prior anti-selective entry-filter attempts.

Where any entry-time signal exists, it is weak and about *session/liquidity*, not the tail:
- **Low vs mid/high:** low-stratum names have a thinner pre-entry tape — trades in 60s pre-entry
  median 222 vs 756 (MWU p=0.023, eff=0.375). gap% marginally higher in low (30.7 vs 30.2,
  p=0.041, eff=0.34, tiny).
- **Pre loser vs winner:** winners had heavier recent activity (mean size 60s pre-entry 478 vs
  162, p=0.050; count 1100 vs 428, p=0.082) — but winner n=3, so treat as suggestive only.

### T3d — is gap% a proxy for stratum?
**Chart:** [t3d_gap_mom_scatter.html](charts/t3d_gap_mom_scatter.html)

**No.** `gap_pct_at_hit` vs `mom_pct`: Pearson r = **0.029** (p=0.82), Spearman ρ = **−0.303**.
The only entry-knowable candidate proxy is *slightly negatively* rank-correlated with the
retrospective stratum axis. **No entry-time feature is a usable proxy for stratum.**

---

## 4 · T3e — Cumulative volume at entry (Cooper's inverted-U) — HEADLINE

Charts in order: [t3e_volume_kde_by_stratum.html](charts/t3e_volume_kde_by_stratum.html) ·
[t3e_volume_bins_bar.html](charts/t3e_volume_bins_bar.html) ·
[t3e_volume_mom_scatter.html](charts/t3e_volume_mom_scatter.html) ·
**[t3e_volume_pnl_scatter.html](charts/t3e_volume_pnl_scatter.html) (Chart 4, headline)**

Cumulative $volume (Σ price·size) from 04:00 ET session start through entry, quintile-binned:

| quintile | $vol range | n | PF | WR% | mean PnL% |
|---|---|---|---|---|---|
| Q1 (lowest) | <$0.35M | 13 | **3.41** | 69% | +3.27 |
| Q2 | $0.36–0.64M | 13 | 0.96 | 38% | −0.19 |
| Q3 | $0.72–1.96M | 13 | 1.23 | 31% | +0.81 |
| Q4 | $2.2–5.9M | 13 | 2.58 | 62% | +5.41 |
| Q5 (highest) | >$7.5M | 13 | 1.89 | 54% | +2.41 |

**Cooper's inverted-U hypothesis is NOT supported — the shape is the opposite in the middle.**
The relationship is non-monotone and closest to **U-shaped**: the *middle* quintiles (Q2–Q3) are
the **weakest** (PF≈1.0), while the **lowest**-volume quintile is the **strongest** (Q1 PF=3.41,
WR=69%). An inverted-U would put the peak in the middle; here the middle is the trough. Volume vs
mom (ρ≈weak) and the faceted volume-vs-PnL LOWESS (Chart 4) show no clean within-stratum monotone
predictor. **Cumulative volume-at-entry is not a reliable outcome predictor and does not follow
the inverted-U.** (Caveat: 5 bins × n=13 — the U is noisy; the firm conclusion is the *rejection*
of inverted-U, not endorsement of a U.)

---

## 5 · T4 — Post-entry features

**T4a — RTH-open crossing:** [t4a_rth_crossing_bar.html](charts/t4a_rth_crossing_bar.html).
**Non-event: only 1 of 65 trades' hold windows crosses 09:30 ET.** Holds are short (median ≈600s)
and pre-market trades exit well before the open. This feature differentiates nothing here.

**T4a2 — post-entry volume trajectory (0–300s):**
[t4a2_volume_trajectory.html](charts/t4a2_volume_trajectory.html). Median cum $vol at +300s:
low $232k vs mid/high $1.56M — the stratum gap simply re-expresses liquidity. Pre-winner ($762k)
vs pre-loser ($815k) are **indistinguishable** — post-entry volume *rate* does not separate the
pre-market modes (winner n=3).

**T4b — hold duration:** [t4b_hold_duration_kde__premode.html](charts/t4b_hold_duration_kde__premode.html)
· [__lowstrat](charts/t4b_hold_duration_kde__lowstrat.html) · [__cvar5tail](charts/t4b_hold_duration_kde__cvar5tail.html).
Winners hold longer (pre-winner median 1239s vs loser 501s; mid/high 763s vs low 365s).

**T4d verdict.** The visible post-entry separations (winners hold longer, liquid names trade more)
are **largely endogenous** — the EPG PASS→FAIL exit *keeps* winners open by construction, and
high-stratum names are simply more liquid. Neither entry-time (T3) nor post-entry (T4) features
give a clean, independent flag for the CVaR5 tail. The one robust, structural fact is **T0**:
low+mid **pre-market** is the loss engine.

---

## 6 · T5 — Stability across the p sweep

**Chart:** [t5_stability_line.html](charts/t5_stability_line.html)

| p | CVaR5 tail n | CVaR5% | tail ∩ low |
|---|---|---|---|
| 0.65 | 3 | −25.25 | 33% |
| 0.70 | 3 | −18.20 | 67% |
| 0.75 | 3 | −21.00 | 67% |
| 0.80 | 3 | −21.76 | 67% |
| 0.85 | 3 | −20.56 | 33% |
| 0.90 | 3 | −20.60 | 33% |

Tail-set size is constant (n=3; it tracks int(0.05·n)). The tail's composition is only partly
low-stratum (33–67% overlap) and does not stabilize onto low stratum as the gate tightens — the
extreme tail is a shifting mix, not a fixed low-stratum population.

---

## 7 · Index (T6)

[index.html](index.html) — organized by task, with a **sortable** per-event table (Stratum /
Session / PnL / Sets all sortable) for the T6a union set. **T6a per-event 4-panel gate charts
(n=32 union: CVaR5 tail ∪ bottom decile ∪ pre-market loser mode) are reused from the existing
p80 event-chart set** (`../phase_r1_final/event_charts_sym_p80/charts/`) and linked, not
duplicated — each is ~5.7 MB and regenerating/copying would waste ~180 MB against a nearly-full
disk. All 32 union charts verified present.

---

## 8 · Escalation Check

| Condition | Threshold | Result |
|---|---|---|
| Required field missing from per-trade output | any | **CLEARED** — all fields present or joinable from sample; cumulative volume read from tape (read-only, not a re-run) |
| T0 cross-tab does not reconcile with Cooper | any | **CLEARED** — reconciles exactly |
| CVaR5 tail < 3 trades | — | **CLEARED** — n=3 |
| `_cvar5` implementation ≠ stated def | any | **CLEARED** — matches `runner_rapid.py:881-883` |
| Pre-market bimodal fit non-convergent | — | **CLEARED** — 2-comp GMM converges, BIC-preferred |
| Finding without required chart | any | **CLEARED** — every finding chart-linked |

No hard stops triggered.

---

## 9 · Output Files

| File | Status |
|---|---|
| charts/stratum_session_bar.html | ✅ |
| charts/stratum_session_kde.html | ✅ |
| charts/global_pnl_kde_tail.html | ✅ |
| charts/premarket_kde_threeway.html | ✅ |
| charts/threeway_venn.html | ✅ |
| charts/t3a_categorical_bars/*.html (8) | ✅ |
| charts/t3b_continuous_kde/*.html (18) | ✅ |
| charts/t3d_gap_mom_scatter.html | ✅ |
| charts/t3e_volume_kde_by_stratum.html | ✅ |
| charts/t3e_volume_bins_bar.html | ✅ |
| charts/t3e_volume_mom_scatter.html | ✅ |
| charts/t3e_volume_pnl_scatter.html | ✅ |
| charts/t4a_rth_crossing_bar.html | ✅ |
| charts/t4a2_volume_trajectory.html | ✅ |
| charts/t4b_hold_duration_kde__*.html (3) | ✅ |
| charts/t5_stability_line.html | ✅ |
| event_charts (union, n=32) | ✅ linked to existing p80 set |
| index.html | ✅ |
| tail_trades.json | ✅ |
| premarket_mode_split.json | ✅ |
| summary.md | ✅ |

---

## Approval Gate

No remediation options are proposed. Do not begin any remediation phase (universe filter,
entry filter, position sizing/scaling, RTH-only / session filter, LULD restoration) until
Cooper reviews this diagnosis and approves both the diagnosis and a chosen mechanism.

**Reproduce:** `scripts/tail_risk_charts.py` (T0–T5), `scripts/tail_risk_index.py` (T6b),
`scripts/tail_risk_lib.py` (shared data layer). Run with `PYTHONUTF8=1`.
