# Phase LULD-V3c — T5a Corrected Sweep (before/after vs V3b T6)

**Date:** 2026-06-20
**Fixes applied:** T3 limit-state-window anchor + T4 normalized liquidity penalty (TARGET_SPREAD_BPS=100). Weights unchanged (3.0/1.0/1.0).
**Total halts (unchanged from V3b T5):** 32

## *** HARD STOP (T5a re-validation) ***

  - max_recall=0.2500 < 0.7

Per spec: audit missed something OR the signal is genuine. Do NOT proceed to Part B. Report to Cooper.

## Corrected sweep (V3c scorer)

| dur | n_fires | tp | fp | fn | recall | fp_rate | mean_liq_pen | composite |
|----:|--------:|---:|---:|---:|-------:|--------:|-------------:|----------:|
|   0 |    1387 |  7 | 1370 | 25 | 0.2188 | 0.9877 |       0.3124 |   -0.6586 |
|   2 |     256 |  8 |  248 | 24 | 0.2500 | 0.9688 |       0.2658 |   -0.3852 |
|   4 |     171 |  8 |  163 | 24 | 0.2500 | 0.9532 |       0.2672 |   -0.3569 |
|   6 |     142 |  8 |  134 | 24 | 0.2500 | 0.9437 |       0.1779 |   -0.2834 |
|   8 |     113 |  8 |  105 | 24 | 0.2500 | 0.9292 |       0.2127 |   -0.2409 |
|  10 |      96 |  8 |   87 | 24 | 0.2500 | 0.9062 |       0.2264 |   -0.2276 |
|  12 |      84 |  8 |   75 | 24 | 0.2500 | 0.8929 |       0.1885 |   -0.1969 |

## Before/after (recall & TP)

| dur | V3b recall | V3c recall | V3b tp | V3c tp | V3b liq(bps) | V3c liq(norm) |
|----:|-----------:|-----------:|-------:|-------:|-------------:|--------------:|
|   0 | 0.0000 | 0.2188 |  0 |  7 | 44.750 | 0.3124 |
|   2 | 0.0312 | 0.2500 |  1 |  8 | 41.174 | 0.2658 |
|   4 | 0.0312 | 0.2500 |  1 |  8 | 43.162 | 0.2672 |
|   6 | 0.0625 | 0.2500 |  2 |  8 | 34.589 | 0.1779 |
|   8 | 0.0625 | 0.2500 |  2 |  8 | 42.370 | 0.2127 |
|  10 | 0.0625 | 0.2500 |  2 |  8 | 45.431 | 0.2264 |
|  12 | 0.1250 | 0.2500 |  4 |  8 | 41.243 | 0.1885 |

## Defects confirmed and fixed (T5a Part A)

| Audit | Defect | Status | Effect of fix |
|-------|--------|--------|---------------|
| T3 | Halt anchored at `seg_end`; scorer only matched the 15 s before it → 0 TP structurally | **Fixed** — labeler records `limit_state_start`; scorer matches the limit-state window `[onset − 15 s, seg_end]` | TP 0–4 → 7–8; recall 0.0 → 0.25 |
| T4 | Liquidity penalty was raw `spread_bps` (range [0, 526]), dominating the composite | **Fixed** — normalized `min(1, spread_bps / 100)` to [0, 1] | mean_liq 34–45 → 0.18–0.31; composite −38 → −0.20 |
| T2 | (pin clock) | No defect found | — |

Regression tests added (381 pass total): `TestLimitStateAnchor` (4), liquidity
normalization/cap/override (3), `test_limit_state_start_recorded` (labeler). The 3 pre-existing
tests that encoded the buggy raw-bps / seg_end behaviour were updated to the corrected contract.

## Why recall is hard-stopped at 0.25 — FN diagnostic (genuine, not a 3rd bug)

Full output: `t5a_fn_diagnostic.txt`. Two findings:

**1. Widening the pre-onset lead window does not recover recall.**

| pre-onset lead | tp | fn | recall | fp_rate |
|---------------:|---:|---:|-------:|--------:|
| 15 s | 8 | 24 | 0.2500 | 0.9437 |
| 30 s | 9 | 23 | 0.2812 | 0.9296 |
| 60 s | 9 | 23 | 0.2812 | 0.9296 |
| 120 s | 9 | 23 | 0.2812 | 0.9225 |
| 300 s | 10 | 22 | 0.3125 | 0.9014 |

Even a 300 s lead lifts recall only to 0.31. The matching window is **not** the binding
constraint.

**2. 22 of 32 halts have no fire within 300 s of their limit-state window** — and six
halt-events (FBYDW, XBP×2, ODVWZ×2, EDBL) produce **zero fires in the entire session**.

**Root cause — trade-vs-quote divergence.** The labeler detects halts on **trade price ≥
upper band**; the exit fires on **NBBO bid within 1 % of the upper band**. For these thin,
low-float names the bid evaporates / lags far below during the limit-up move, so the
quote-based exit signal never triggers for ~70 % of the trade-based halts. This is a
different axis from the V3b band-*definition* reconciliation (both now use the same 5-min
sticky reference); it is a signal-source mismatch, not a band or scoring bug.

## Hard-stop disposition

- **T4 liquidity hard-stop: CLEARED** (min mean_liq = 0.178 ≤ 0.5).
- **T3 recall hard-stop: TRIGGERED** (max recall = 0.2500 < 0.70), and the FN diagnostic
  shows it is genuine — the quote-based exit is structurally blind to most trade-based halts.

Per the T5a escalation table, **do not proceed to Part B (liquidity-adaptive tiers)**. The
binding constraint is the entry signal source (bid-vs-band), not the duration threshold or
the liquidity penalty. No recommendation offered; awaiting Cooper.

---

**Cooper reviews before any Part B (liquidity-adaptive tiers) or follow-on phase.**