# R1-Fixed (no gate) vs R1.5 Time Gate Sweep

**Date:** 2026-06-24
**Implementation commit:** `0409332`
**Config:** `entry_mode=first_pass`, `max_entry_lag_sec=300`, `p_open=p_close=0.65`,
`roc_min=None`, gate `peak` (τ=300, tau_peak=600, C=1.5, warmup=300), val MDR≥150
(`val_mdr150_diagnostic.json`, 100 events, seed=42). Only `--t-gate-sec` varies.
Baseline = R1-Fixed p=0.65 (no time gate).

## Sweep comparison table

| T_gate | N trades | N open at check | N time_gate | N epg_close | PF | CVaR5 | Mean Hold | False gate% | True gate% |
|--------|----------|-----------------|-------------|-------------|--------|---------|-----------|-------------|------------|
| None (baseline) | 46 | — | 0 | 46 | 2.6584 | −27.74% | 1088s | — | — |
| 400 | 46 | 39 | 18 | 28 | 2.2166 | −25.93% | 774s | 33.3% | 66.7% |
| **500** | 46 | 38 | 19 | 27 | **2.8805** | **−25.42%** | 847s | **15.8%** | 84.2% |
| 600 | 46 | 37 | 19 | 27 | 2.6919 | −29.61% | 892s | 21.1% | 78.9% |

(All arms: `session_end` = 0. N open at check = baseline trades with no-gate hold > T_gate.)

## Exit reason shift

- **Baseline (None):** 46/46 `epg_window_close`, 0 time_gate.
- **T_gate=400:** 18 trades cut by the time gate (39.1%), 28 still exit via EPG close. The gate
  fires earliest and most often relative to the open population, but at the cost of winners
  (see false-gate analysis).
- **T_gate=500:** 19 time_gate (41.3%), 27 EPG close. The gate cuts almost exclusively losers
  (84.2% true) — the cleanest separation of the three arms.
- **T_gate=600:** 19 time_gate (41.3%), 27 EPG close. Similar fire count to 500 but later, so the
  cut losers have already bled further.

## False gate analysis

A *false gate* = a trade the time gate cut whose no-gate (baseline) outcome was profitable.

- **T_gate=400 — 6 false of 18 (33.3%):** MBIO 2024-06-17 (cut −4.58% vs no-gate **+24.16%**),
  BNAI (−10.74% vs +12.05%), ENVB (−8.03% vs +10.22%), SINT (−11.39% vs +10.18%),
  CXAI (−4.69% vs +5.16%), TELO (−0.87% vs +4.92%). Sacrificing MBIO (+24%) is the main reason
  the 400 arm's PF falls below baseline.
- **T_gate=500 — 3 false of 19 (15.8%):** BNAI 2024-05-31 (cut −0.60% vs **+12.05%**),
  ENVB 2024-02-29 (−8.76% vs +10.22%), CXAI 2024-04-01 (−13.85% vs +5.16%).
- **T_gate=600 — 4 false of 19 (21.1%):** ENVB (−4.38% vs +10.22%), XTLB (−0.32% vs +6.33%),
  CXAI (−1.41% vs +5.16%), GTBP (−2.08% vs +1.73%).

**Concentration:** the false gates are *not* spread evenly — they are a recurring subset of
slow-developing ("late bloomer") winners. ENVB and CXAI are false-gated at all three T_gates;
BNAI at 400 and 500. These names had long no-gate holds (~2,000–2,500s, all `epg_window_close`)
and were temporarily negative around the gate time before recovering. Raising T_gate from 400→500
removes three of the worst false gates (MBIO, SINT, BNAI-at-400 severity), which is why PF jumps
from 2.22 to 2.88. The true gates conversely rescue real tail losers: at T_gate=500, VSSYW
−15.73%→−0.56% and BNED −14.58%→−0.25% are the largest tail saves.

## Hold time impact

Mean hold drops from the baseline 1,088s to 774s (T=400), 847s (T=500), 892s (T=600) — the gate
shortens average time in market by cutting the slow non-performers. Note the non-monotonicity in
CVaR5: T=400 (−25.93%) and T=500 (−25.42%) both *improve* the tail vs baseline (−27.74%), but
T=600 (−29.61%) is *worse* than baseline. At 600s the losers that will be cut have already
deteriorated further than at 500s, so the realized cut PnL is deeper — pushing the 5% tail below
baseline. The tail benefit therefore peaks at ~500s and reverses by 600s, consistent with the
DIAG-MFE-MAE Chart 7 divergence point (~500s).

## Escalation status

| Criterion | Threshold | Observed | Result |
|-----------|-----------|----------|--------|
| Tests fail after implementation | any | 403 pass | cleared |
| Sanity A: loser not `time_gate` | any | OMH → `time_gate` @ 500.4s | cleared |
| Sanity B: short winner `time_gate` | any | LRHC → `epg_window_close` @ 324.6s | cleared |
| `n_time_gate_exits = 0` all arms | all = 0 | 18 / 19 / 19 | cleared |
| CVaR5 worse at all three arms | all worse than −27.74% | 400 & 500 better; only 600 worse | not flagged |
| `false_gate_rate > 50%` all arms | all > 50% | 33.3% / 15.8% / 21.1% | not flagged |
| `phase_r1_fixed/` modified | any | byte-identical (70 files) | cleared (T5a PASS) |

No hard stops, no flags. **T_gate=500 improves both PF (2.6584 → 2.8805, +0.222) and CVaR5
(−27.74% → −25.42%, +2.32pp) over the no-gate baseline**, with the lowest false-gate rate (15.8%)
— the first mechanism in this exit-research sequence to improve both metrics simultaneously.

## Results locations

Baseline (no gate): `backtest/results/phase_r1_fixed/`   ← UNTOUCHED (T5a verified)
R1.5 sweep:         `backtest/results/phase_r15/`
