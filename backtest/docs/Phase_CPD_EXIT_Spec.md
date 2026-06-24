# Phase CPD-EXIT — Exit Stack Redesign & Directional Filter

**Status:** Complete (no full val run — no actionable improvement found)  
**Date:** 2026-06-10 — 2026-06-11  
**Prerequisite:** BOCPD sweep winner selected and approved (Phase CPD-BOCPD)  
**Objective:** Replace the late `epg_window_close` exit with a signal-based trailing stop and add a price momentum directional filter to block entries into bearish bursts.

---

## 1. Problem Statement

The BOCPD gate produces good entries — sharp, right at the start of every burst. But exits are
dominated by `epg_window_close`, which waits for the full PASS→FAIL transition. By the time the
gate declares the regime over, the burst has long since exhausted and the trade is a loser.

**A fixed TP/SL would be profitable on this setup.** That is the key diagnostic: the entry timing
is correct, the exit timing is the entire problem.

A secondary failure mode exists: WJI tracks participation with some directionality. During a
violent sell wave, `λ_V` spikes and `λ_buy_slow` hasn't decayed yet — so WJI reads "strong
participation" while price is collapsing 50% in minutes. The signal is structurally blind to
direction in this scenario.

---

## 2. Why Not EXIT_D

EXIT_D (`I(t) = λ_sell / (λ_buy + λ_sell) > theta` for `tau_min` continuous seconds) was
the prior solution to this problem. It has two known structural issues here:

1. **Too narrow:** Requires *sustained* sell imbalance. Fast burst exhaustion without a strong
   sell takeover never crosses `theta` persistently enough — the trade bleeds to window close.

2. **Lee-Ready noise:** `λ_buy` and `λ_sell` are built on per-trade side classification.
   Lee-Ready accuracy in this codebase is known to be imperfect, which means `I(t)` inherits
   that noise. A threshold on a noisy signal must be conservative or it fires on garbage.

EXIT_D remains in the exit stack as a backstop but is **not** the primary exit for in-burst
trades under this design.

---

## 3. Exit Mechanism — WJI Trailing Stop

### 3.1 Concept

At entry, record `WJI_peak_since_entry = WJI(t_entry)`. After entry, update this peak each
tick:

```
WJI_peak_since_entry = max(WJI_peak_since_entry, WJI(t))
```

Exit fires immediately when:

```
WJI(t) < p_exit × WJI_peak_since_entry
```

No confirmation window. Single tick below the floor triggers exit.

### 3.2 Key Design Points

**Per-trade peak, not the gate's global peak.** The gate peak is a pre-entry construct — it
is already elevated at the moment of entry. A new peak anchored at entry time is required.
If the gate peak were used, `p_exit × gate_peak` could fire immediately on entry.

**`p_exit` is the only free parameter for a given signal variant.** Two signal variants
are swept in parallel:

- **Raw WJI:** `WJI(t) < p_exit × WJI_peak_since_entry`
- **Log WJI:** `log(WJI(t)) < p_exit × log(WJI_peak_since_entry)`

The log variant compresses the right tail and may produce more stable thresholds across
events with different burst magnitudes. Both are swept over the same `p_exit` grid.

**`p_exit` sweep grid: `{0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9}`** — every decile.
This is a wide first pass. A tighter range will be identified from the results before any
follow-on phase. Too tight (low `p_exit`) = shaken out on within-burst noise. Too loose
(high `p_exit`) = same lag as current window close.

**This is the primary exit for in-burst trades.** It replaces `epg_window_close` as the
dominant mechanism. Window close remains as a backstop for anything that slips through.

### 3.3 Position in Exit Stack

```
1. WJI trailing stop (raw or log variant)   ← new primary exit
2. Price momentum drop                       ← directional safety (see §4)
3. LULD upper band                           ← retained (PF=13–17; catches parabolic exhaustion)
4. EPG window close                          ← final backstop
```

EXIT_D is **permanently disabled** in this design. The Lee-Ready noise makes it mostly
noise at this strategy's burst timescales. Code retained but not in the active stack.

First signal to fire wins.

---

## 4. Directional Filter — Price Momentum Drop

### 4.1 Problem

WJI can remain elevated or increase during violent sell waves. This produces entries (or
held positions) into 50%+ selloffs. The signal does not see direction, only participation
magnitude.

### 4.2 Why Not Buy Imbalance (I_buy)

The natural directional signal would be `I_buy = λ_buy / (λ_buy + λ_sell)`. This was
rejected for two reasons:

- Lee-Ready signing accuracy is poor — `I_buy` inherits per-trade noise directly
- EXIT_D history shows this threshold requires conservatism that makes it too narrow

### 4.3 Chosen Mechanism

**Short-window price momentum:**

```
momentum(t) = (price(t) - price(t - N_seconds)) / price(t - N_seconds)
```

**`N = 120 seconds` (fixed, not swept).**

Exit fires when:

```
momentum(t) < -momentum_threshold
```

This catches the *velocity* of a sell-off, not just the cumulative damage. A 3% drop in 120
seconds is a meaningfully different signal from a 3% drift over 30 minutes.

### 4.4 Properties

- **Signing-independent.** Pure price arithmetic — no Lee-Ready dependency.
- **Noise-resistant.** 120-second window smooths tick noise without being too slow to catch
  fast reversals.
- **Catches a different failure mode than the WJI trailing stop.** The trailing stop responds
  to participation fading (WJI drops). The momentum filter responds to price collapsing while
  WJI stays elevated. These are complementary, not redundant.

### 4.5 Free Parameter

`momentum_threshold` — the minimum negative 120s return (as a percentage) that triggers
exit.

**Sweep grid: `{0.02, 0.04, 0.06, 0.08, 0.10, 0.12, 0.14, 0.16, 0.18, 0.20}`** — every
even multiple of 2% from 2% to 20%.

No session filter. The same threshold applies pre-market, RTH, and post-market. Pre-market
thinness is a known property of the event universe and is not treated as a special case here.

---

## 5. Combined Exit Logic

Both mechanisms are **exit-only** — they fire from a live position. The BOCPD gate handles
entry qualification. Neither mechanism is an entry filter.

On any tick while in position, check in order:

```
1. WJI(t) < p_exit × WJI_peak_since_entry                → EXIT (wji_trailing_stop)
   [or: log(WJI(t)) < p_exit × log(WJI_peak_since_entry) for log variant]
2. momentum_120s(t) < -momentum_threshold                 → EXIT (momentum_drop)
3. price within 2% of LULD upper band (RTH only)          → EXIT (luld_upper)
4. EPG PASS → FAIL / INACTIVE                             → EXIT (epg_window_close)
```

EXIT_D is not in the stack. Code remains but `exit_d.enabled = false` is the permanent
config for this phase forward.

---

## 6. Open Design Questions

These are **not resolved** and require Cooper decision before implementation:

1. **Sweep structure.** The full joint sweep is `9 p_exit values × 2 WJI variants × 10
   momentum thresholds = 180 configs`. That's large but not unreasonable on this hardware.
   Alternative: stage it — sweep WJI trailing stop alone first (18 configs), pick a winner,
   then sweep momentum threshold on top (10 configs). Staged keeps each mechanism's
   contribution separable and is easier to debug. Joint is cleaner statistically.

2. **Log WJI edge case.** `log(WJI)` is undefined if `WJI ≤ 0`. WJI is strictly positive
   by construction (`sqrt(vol_ratio × I_buy)` with floor at EPS), but confirm the floor
   is enforced before implementing the log variant. If `WJI_peak_since_entry` is very close
   to zero at entry (cold start or thin event), `log(WJI_peak_since_entry)` could be a large
   negative number and the threshold would behave unexpectedly.

---

## 6b. Note — Profit Protection Exit (deferred)

A third exit mechanism was discussed but is not specced yet. The failure mode it targets:
trades that reach +20–30% and give back most of the gain, ending around +7%.

**Concept:** once price is up ≥8% from entry, activate a tight price trailing stop that
sells into buying pressure rather than waiting for exhaustion. Quote-side bid depth would
be the liquidity condition — exit fires when the trailing stop triggers *and* bid depth
is healthy enough to absorb the sell.

**Why deferred:** Quote-level parameter intuition is unreliable without seeing the data.
The right next step is a dedicated diagnostic chart phase on the worst offenders — charts
showing price high-water mark, bid/ask size, and spread simultaneously over the reversal
window — then eyeball what the quote picture looks like near the peak vs the eventual exit.
No parameters can be set responsibly before that review.

**Come back to this after CPD-EXIT Phase 1–3 are complete.**

---

## 7. What Is NOT In This Spec

- **Directional entry filter.** Discussed but not included. The gap-up scanner already
  provides strong directional prior (≥30% gap). Entries are already good per the BOCPD
  charts. Adding a directional entry filter risks reducing trade count without fixing the
  exit problem.

- **Quote-side imbalance (bid/ask size ratio).** More reliable than Lee-Ready but adds
  infrastructure cost. Deferred unless momentum filter proves insufficient.

- **Fixed TP/SL.** Discussed as a diagnostic benchmark only. Not the mechanism to ship —
  fixed levels can't adapt to different gap magnitudes and session types.

- **BOCPD symmetric exit (S_down).** Discussed. Not selected because: (a) BOCPD already
  had exit lag in the charts; (b) WJI trailing stop is simpler and directly addresses the
  observed failure mode.

- **EXIT_D.** Permanently disabled. Lee-Ready noise makes it mostly noise at burst
  timescales. Not revisited in this phase.

---

## 8. Prerequisite Before Implementation

**BOCPD sweep winner must be selected and approved first.** This spec assumes the BOCPD
gate is the entry mechanism. The exit sweep must run on top of the confirmed winner config,
not a provisional one.

---

## 9. Next Steps (pending approval)

1. Review BOCPD charts — confirm WJI trailing stop would have fired earlier than window
   close on the losing trades; confirm momentum drop would have caught the violent reversal
   events.
2. Decide on sweep structure (joint vs. staged) and parameter ranges.
3. Build agent prompt per Agent_Prompt_Standard.md.
4. Run sweep against BOCPD winner config on 100-event val sample.
5. Per-event charts comparing exit mechanism behavior (WJI trailing stop fire vs. where
   window close would have fired).

---

## 10. Results (2026-06-11)

**Entry gate fixed throughout:** BOCPD winner (lambda_h=0.01, p_enter=0.60). 100-event val sample, 687 trades per config.

### Sub-Phase 1 — TP/SL benchmark

| config | TP% | SL% | PF | CVaR5 | EV |
|--------|-----|-----|----|-------|----|
| tp5_sl5 | 5 | 5 | **1.399** | **−9.14%** | **0.505** |
| tp10_sl5 | 10 | 5 | 1.388 | −9.27% | 0.469 |
| tp15_sl5 | 15 | 5 | 1.359 | −9.33% | 0.394 |
| tp5_sl3 | 5 | 3 | 1.232 | −5.28% | 0.244 |

SL=5% is the sweet spot. SL=3 overcuts trades; SL=10 deepens CVaR5. EPG window close dominates (50–75% of exits); TP acts as a tail-capture backstop. This is the benchmark to beat.

### Sub-Phase 2a — WJI trailing stop

**HARD STOP.** Best CVaR5=−16.83% (raw_pe20, PF=1.396) < −15% threshold. Charts not generated.
18 configs (raw + log, p_exit 0.1–0.9). Loose stops don't fire enough; tight stops shake out winners early. Log variant consistently trails raw.

### Sub-Phase 2b — 120s momentum drop

| config | threshold | PF | CVaR5 | EV |
|--------|-----------|-----|-------|----|
| mom6 | −6% | 1.142 | −12.60% | 0.220 |
| mom10 | −10% | 1.125 | −16.78% | 0.239 |
| mom4 | −4% | 0.965 | −11.23% | −0.048 |

Best mom6 is below Sub-1 on all metrics. Mid-range thresholds (4–10%) perform best; below 4% overcuts, above 12% rarely fires. EPG window close still dominates (65% of exits at mom6).

### Sub-Phase 3 — Combined WJI trailing + momentum drop

20 configs (4 WJI p_exit × 5 momentum thresholds, raw WJI only).

| config | wji_pe | mom% | PF | CVaR5 | EV |
|--------|--------|------|----|-------|----|
| pe10_mo6 | 0.1 | 6 | 1.128 | −12.60% | 0.199 |
| pe10_mo10 | 0.1 | 10 | 1.114 | −16.59% | 0.216 |
| pe20_mo10 | 0.2 | 10 | 1.098 | −15.49% | 0.177 |

Strictly worse than either mechanism alone. At pe=0.1, WJI fires only 0.7% of trades — the combination is effectively just momentum drop with a redundant WJI layer. Combining does not improve tails meaningfully vs the individual mechanisms.

### Phase conclusion

No exit mechanism or combination beats Sub-1 TP/SL (tp5_sl5: PF=1.399, CVaR5=−9.14%, EV=0.505). Full val run skipped — no actionable improvement found. The BOCPD entry gate is the binding constraint, not the exit timing. The original hypothesis ("exits are the entire problem") was partially confirmed — TP/SL does improve substantially over raw window close — but none of the signal-based exits (WJI trailing, momentum drop) improve further on top of that.

**Output:** `results/phase_cpd_exit/sub1_tp_sl/`, `sub2a_wji_trailing/`, `sub2b_momentum_drop/`, `sub3_combined/`
