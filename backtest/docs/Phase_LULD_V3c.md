# Phase LULD-V3c — Audit, Corrected Re-score, and Decision to Abandon the LULD Exit

**Date:** 2026-06-20
**Status:** COMPLETE — **DECISION: abandon the LULD proximity exit.**
**Decision owner:** Cooper
**Scope:** LULD exit permanently dropped from EPG-Rapid (C2 dropped, R4 dropped). The
quote-based LULD proximity exit research line (V3 → V3b → V3c, and LULD-REBUILD) is closed.

---

## Decision

**The LULD proximity exit is abandoned.** It will not be used as a strategy exit in
EPG-Rapid, and the quote-based LULD exit line is closed for the classic EPG runner as well.
The EPG-Rapid exit stack is **EPG PASS → FAIL only**.

The V3c code fixes (correct halt-onset anchoring, normalized liquidity penalty) and their
regression tests are **kept** in the codebase — they are correct and leave the scoring
machinery sound for any future use — but no LULD exit is wired into the live or backtest
strategy path.

---

## Why — the short version

A quote-based proximity exit can only anticipate **mechanical Limit-State halts**, where the
quote sits *at* the band for 15 seconds before the pause. But the majority of halts on these
low-float momentum names are **discretionary Straddle-State pauses**, where the NBBO has
gapped *away* from the band and the primary listing exchange declares the pause **at its own
discretion**. There is no deterministic quote (or price) precursor to key an automated exit
on. No threshold, duration, or liquidity tuning can recover that — the binding constraint is
the discretionary, non-deterministic nature of straddle halts, not any parameter we control.

---

## Why — the full chain

### 1. The audit cleared the instrumentation, so the residual signal is real

Phase V3b T6 produced numbers that looked broken (recall ≈ 0, liquidity penalty 34–45,
FP-rate ≈ 1.0). The V3c audit confirmed two genuine instrumentation defects and fixed both:

- **T3 (anchor) — fixed.** The labeler anchored each halt at `seg_end` (the freeze moment),
  and the scorer only matched fires in the 15 s before it. Because limit-state runs are ≥15 s
  by construction and the exit fires during the *run-up* (≥15 s earlier), every fire landed
  outside the window → **0 TP structurally**. Fix: the labeler now records
  `limit_state_start` (the onset) and the scorer matches the limit-state window
  `[onset − 15 s, seg_end]`.
- **T4 (liquidity penalty) — fixed.** The penalty was emitted as raw `spread_bps` (range
  [0, 526]) and subtracted directly from `recall` (≤3) and `fp_rate` (≤1), so it *was* the
  composite. Fix: normalized to `min(1, spread_bps / 100)` ∈ [0, 1]. Weights unchanged.
- **T2 (pin clock) — no defect.** 0–2 flicker transitions per segment, 83–100 % pinned. Ruled
  out as a cause.

With the instrumentation fixed, the corrected sweep gave: recall **0.0 → 0.25**, mean
liquidity penalty **34–45 → 0.18–0.31**, composite **−38 → −0.20**. The liquidity hard-stop
**cleared**; the recall hard-stop (max 0.25 < 0.70) **triggered**. Because the instrumentation
is now correct, that residual 0.25 recall is a real measurement, not an artifact.

### 2. The residual is not a window-width problem

Widening the matching lead window from 15 s to 300 s lifts recall only **0.25 → 0.31**. The
exit is not "firing a bit too early" — for most halts it is **not firing at all near the
event**.

### 3. The exit is structurally blind to most halts — and the reason is the halt type

Of the 32 labeled halts, **22 have no fire within 300 s of their window**, and **6 events
(FBYDW, XBP×2, ODVWZ×2, EDBL) produced zero fires the entire session.** The mechanical reason
is a **trade-vs-quote divergence**: the halt labeler detects a halt from **trade price ≥
band**, while the exit fires from **NBBO bid within 1 % of band**. For these names the bid
evaporates / lags far below the band during the fast move, so the quote signal never triggers.

That divergence is not a bug — it is the **microstructure signature of a Straddle-State
halt**, and it is the heart of the decision:

| Halt type | LULD condition | Predictable from quotes? | In our sample |
|-----------|----------------|--------------------------|---------------|
| **Limit-State pause** | NBO (up) / NBB (down) sits *at* the band for 15 s, then a pause is **automatic** | **Yes** — the quote is pinned at the band; a proximity exit can anticipate it | ~8 / 32 (the ones we catch) |
| **Straddle-State pause** | NBB is *below* the lower band / NBO is *above* the upper band — the band is *straddled* by (gapped away from) the NBBO; the listing exchange **may** declare a pause **at its discretion** | **No** — the defining feature is that the quote is *not* at the band, and the trigger is a human/venue discretionary call, not a price condition | ~22–24 / 32 (the ones we miss) |

A quote-proximity exit is, by construction, a **Limit-State detector**. It can only see the
halts where the quote pins to the band. Straddle-State pauses are exactly the case where the
quote has gapped past the band (so there is nothing at the band to detect) **and** the pause
is declared at the exchange's discretion (so there is no deterministic precursor at all). On
these low-float momentum names the discretionary straddle halts dominate, which is why recall
caps near 0.25 and cannot be tuned upward.

### 4. Switching the signal source would not rescue it

One might try keying the exit off **trade price ≥ band** instead of the bid (to match the
labeler). That removes the trade-vs-quote divergence but not the core problem:

- A trade printing through the band is the *same instant* the straddle/limit state begins —
  there is little-to-no lead time to exit before the pause.
- The pause itself is still **discretionary** in the straddle case; predicting *whether* a
  given band-straddle becomes a halt is not a deterministic function of price.

So even the best-case signal-source swap converts "blind" into "too late and still
non-deterministic." The juice isn't there.

---

## What this means downstream

- **EPG-Rapid:** exit stack is **EPG PASS → FAIL only.** C2 (LULD exit) dropped; R4 (no LULD
  module) dropped. Phase sequence: C1 ✅ → C3 → C4 → R0 → R1 → R2 → R3 → R5.
- **Classic EPG runner:** the open LULD items (LULD-V3b T6 winner selection, LULD-REBUILD T6
  charts, V3c Part B liquidity-adaptive tiers, V3c T1 charts) are **closed — do not pursue.**
  The Phase F config retains LULD upper-band in the legacy lineage; this decision means no
  further investment in the quote-based LULD exit, not a forced removal from frozen Phase F
  artifacts.
- **Code:** V3c fixes retained (`core/features/luld_halt_detection.py` — `limit_state_start`;
  `core/exits/luld_scoring.py` — onset-window matching + normalized penalty). 381 tests pass.
  These are correct and harmless to keep; they document the right way to score a halt exit if
  the question is ever reopened with a different signal.

---

## Evidence trail

| File | Contents |
|------|----------|
| `results/phase_luld_v3c/t2_pin_clock_audit.md` | Pin/clock audit — no defect |
| `results/phase_luld_v3c/t3_matching_audit.md` | Anchor defect, confirmed + fixed |
| `results/phase_luld_v3c/t4_liquidity_formula_audit.md` | Liquidity-penalty units defect, confirmed + fixed |
| `results/phase_luld_v3c/t5a_corrected_sweep.md` | Corrected sweep, before/after vs V3b T6, hard-stop disposition |
| `results/phase_luld_v3c/t5a_fn_diagnostic.txt` | FN diagnostic: lead-width sweep + per-halt nearest-fire offsets |
| `results/phase_luld_v3c/audit_evidence.json` | Raw audit rows (T2/T3/T4) |
| `scripts/luld_v3c_{audit,t5a,fn_diag}.py` | Reproduction scripts |

---

## Decision summary

The LULD proximity exit cannot work because it is a Limit-State detector applied to a
population dominated by discretionary Straddle-State halts. The instrumentation has been
fixed and verified; the residual ~25 % recall is real and unfixable by tuning. **The exit is
abandoned.** EPG-Rapid proceeds with EPG PASS → FAIL as its sole strategy exit.
