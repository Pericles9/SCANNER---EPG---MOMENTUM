# Phase CPD-DIAG — CUSUM k12_h8 Diagnostic Charts

**Date:** 2026-06-09
**Status:** Complete — 15 charts generated, 0 errors. Diagnostic only (no backtest changes). Awaiting Cooper review of the charts before any follow-on phase.
**Purpose:** Visually inspect why the best CUSUM config (`k=12, h=8`, best sweep CVaR5 = −30.55%) produces deep tail losses.

---

## Method

15 events drawn from the 100-event val sample with `numpy.random.default_rng(7)`, no replacement. The **exact** k12_h8 signal is reconstructed (not a dynamic-background variant):

- **WJI_background ≡ 1.0** (Gate-1 Option A) → `WJI_log = log(WJI)`. This is what the gate actually consumed; a dynamic background would show a different signal and misrepresent the diagnostic.
- Axis = halt-adjusted **active seconds since T_event** (same as CPD-1).
- Per-tick `WJI`, `WJI_log`, `S_up`, gate state, entries/exits captured from a live replay of the k12_h8 gate.
- **PELT** (offline, `rbf`, `pen = log(n)·σ_log²`, cap 10 CPs) on the resampled WJI_log trace.

Generator: [tools/phase_cpd/diag_charts.py](../../tools/phase_cpd/diag_charts.py).

### T2a — WARMUP centering (DESCRIPTIVE, not a hard stop)
Per Cooper, T2a is reported, not enforced: only **1/15** events have |mean WJI_log| < 0.5·σ_log during warmup. This is expected — the warmup window is the ignition surge (mean WJI_log ≈ +1.0 to +2.4), not rest (established in CPD-0). Not a failure.

---

## Results (k=12, h=8)

Year distribution of the 15: **2023: 2, 2024: 13** (the val sample only spans 2023–2024).

| ticker | date | n | PnL% | worst% | tail | cps | σ_log | warmup mean | warmup std |
|---|---|---|---|---|---|---|---|---|---|
| AUUD | 2024-04-16 | 1 | −22.2 | −22.2 | **TAIL EVENT** | 9 | 0.166 | 1.06 | 0.17 |
| BYND | 2024-02-28 | 3 | +0.7 | −9.5 | OK | 10 | 0.234 | 1.27 | 0.23 |
| CING | 2023-12-28 | 2 | +1.7 | +0.6 | OK | 10 | 0.108 | 1.27 | 0.11 |
| HOLO | 2024-02-08 | 0 | 0.0 | 0.0 | OK | 10 | 0.302 | 2.18 | 0.30 |
| LGVN | 2024-04-08 | 4 | −57.9 | −47.0 | **TAIL EVENT** | 7 | 0.192 | 2.38 | 0.19 |
| LGVN | 2024-06-12 | 1 | +109.8 | +109.8 | OK | 6 | 0.184 | 2.04 | 0.18 |
| MDIA | 2024-06-11 | 0 | 0.0 | 0.0 | OK | 7 | 0.170 | 1.15 | 0.17 |
| MLGO | 2024-06-27 | 2 | −4.0 | −3.2 | OK | 9 | 0.159 | 1.56 | 0.16 |
| MURA | 2023-11-17 | 1 | −1.5 | −1.5 | OK | 9 | 0.191 | 1.07 | 0.19 |
| NCPL | 2024-05-24 | 0 | 0.0 | 0.0 | OK | 9 | 0.192 | 0.92 | 0.19 |
| NIVF | 2024-04-26 | 1 | −0.3 | −0.3 | OK | 7 | 0.088 | 1.39 | 0.09 |
| SINT | 2024-05-15 | 1 | +47.2 | +47.2 | OK | 10 | 0.180 | 1.19 | 0.18 |
| STRC | 2024-02-27 | 0 | 0.0 | 0.0 | OK | 7 | 0.533 | 1.59 | 0.53 |
| VERO | 2024-06-07 | 0 | 0.0 | 0.0 | OK | 8 | 0.398 | 0.10 | 0.40 |
| XHG | 2024-05-31 | 0 | 0.0 | 0.0 | OK | 7 | 0.174 | 0.76 | 0.17 |

### Observations
- **6/15 events produce zero trades** — k12_h8 is highly restrictive; many events never accumulate enough evidence to open.
- **2/15 are TAIL EVENTs** (AUUD −22.2%, LGVN-04-08 −47.0% worst / −57.9% total). These few events drive the −30% CVaR5 of the aggregate sweep.
- A handful of large single-trade winners (SINT +47%, LGVN-06-12 +110%) keep PF > 1 despite the tails.
- PELT finds 6–10 changepoints per event (penalty cap), confirming rich regime structure consistent with CPD-0's ~14 segments/event.

---

## Chart key (per-event 5-panel HTML)

All panels share an x-axis of **active seconds since T_event** (x=0 = anchor). **Yellow band** = WARMUP [0,300s]; **light-green bands** = CUSUM PASS windows.

| Panel | Content |
|---|---|
| 1 — Price | 1-min (60-active-sec) candlesticks. Green ▲ = entry; ▼ = exit (green=win, red=loss). Black dashed = T_event. |
| 2 — WJI | blue `WJI(t)`; grey-dashed flat line at 1.0 = `WJI_background` (≡1.0); orange-dashed verticals = PELT changepoints. |
| 3 — log(WJI/bg) | blue `WJI_log(t)`; grey-dashed at 0 = background; light-blue dotted at ±σ_log; orange-dashed CPs. |
| 4 — CUSUM S_up | purple `S_up(t)`; red-dashed at y=8 = `h` (PASS when above); grey-dotted at 0 = floor (FAIL on drain). |
| 5 — Gate state | step line, 0=FAIL / 1=WARMUP / 2=PASS. |

Title: `TICKER DATE | k=12 h=8 | n_trades=N | PnL=X% | CVaR5 contribution: LABEL | halts=H`.
**Reading flow:** S_up (P4) crosses h → state turns PASS (P5) → green shading → ▲ entry (P1); S_up drains to 0 → FAIL → ▼ exit. Comparing exit timing vs the price collapse in P1 is the point — it shows whether S_up drained fast enough to exit before the round-trip (the tail driver).

Open the sortable launcher at `charts/index.html`.

---

## Artifacts (gitignored — local only)
`charts/{TICKER}_{DATE}.html` (15), `charts/index.html`, `selected_events.json`, `signal_stats.json`, `pelt_changepoints.json`. Regenerate via `python -m tools.phase_cpd.diag_charts`.
