# T1 — IDAI Band Diagnostic

**Date:** 2026-06-19
**Event:** IDAI 2024-02-16, halt start 2024-02-16 11:15:27 ET (UTC 16:15:27)

---

## ESCALATION — Finding Contradicts Expected Fix Direction

**Stop condition:** T1 evidence shows the exit module's INACTIVE state is the cause of the miss,
not a band computation mismatch. The planned T2 fix (change labeler to 5-min sticky reference)
would eliminate the one existing halt label, reducing val-sample halt count from 1 → 0.
Awaiting instruction before proceeding to T2.

---

## What Was Expected vs. What Was Found

**Phase V3a spec anticipated:** The exit module (5-min sticky reference) and the labeler
(30-second VWAP) compute different upper bands from the same price series. At the IDAI
halt moment, the labeler's band was breached but the exit module's band was not.

**What actually happened:** The exit module was `INACTIVE` (reference price = 0.0,
upper band = 0.0) at the halt time. There was no band comparison to make — the module
had no band at all.

---

## Root Cause: Pre-Halt Trading Gap

IDAI (a thinly-traded penny stock) had 132 trading gaps > 60 seconds throughout the day,
many exceeding 5 minutes. The gap immediately before the halt:

```
11:06:04 ET  →  11:15:03 ET    gap = 538.6 seconds (~9 minutes)
```

This gap is longer than the exit module's 5-minute buffer window (`ref_window_sec=300.0`).
All trades in the buffer were aged out during the gap. When trading resumed at 11:15:03,
the buffer contained only the single fresh trade, making `oldest_ts ≈ current_ts` and
triggering the 60-second warmup condition.

**The halt began at 11:15:27 ET — only 24 seconds after trading resumed.** Warmup requires
60 continuous seconds of data. The module was in INACTIVE state at exactly the halt moment.

---

## State Transitions Around the Halt

| Time (ET) | State | ref price | upper band | Note |
|---|---|---|---|---|
| 11:06:01 | SAFE | 1.4757 | 1.62 | Active before gap |
| [gap 538.6s] | — | — | — | Buffer aged out |
| 11:15:03 | INACTIVE | 0.0000 | 0.0000 | First trade after gap; warmup reset |
| 11:15:27 | INACTIVE | 0.0000 | 0.0000 | **Halt begins here** |
| 11:26:28 | SAFE | 2.0288 | 2.23 | Post-halt recovery; ref jumped to higher price |

At 11:15:27 (halt moment): `state=INACTIVE`, `ref=0.0000`, `upper=0.0000`.

---

## Band Comparison: Not Applicable

The T1 objective was to compare both bands side-by-side at the halt moment. This comparison
cannot be made because the exit module had no band. The labeler's 30-second VWAP was active
(VWAP adapts quickly after gaps), but the exit module's band was undefined.

| Module | Reference Logic | Band at Halt |
|--------|----------------|--------------|
| `LuldProximityExit` | 5-min rolling mean + 1% sticky | 0.0 (INACTIVE) |
| `detect_luld_halts` | 30-second VWAP | ~1.72 (active) |
| Trade price at halt | — | 1.73 |

The labeler's VWAP band (~1.72) was breached by the 1.73 trade price at 11:15:04.
The exit module had no band at all.

---

## Architecture Reference Check (§4)

Per `docs/LULD_Halt_Architecture.md` §4:
> *"Reference price: arithmetic mean of all eligible reported transactions over the prior
> 5-minute rolling window, as published by the SIP."*
> *"If no eligible trades in 5 min: The previous reference price stays in effect."*

The SIP spec says the reference price **stays in effect** during trading gaps — it does not
reset. The exit module's current behavior (aging out the buffer during gaps) contradicts this:
when a gap exceeds the buffer window, the module goes INACTIVE instead of freezing the last
known reference price.

The 30-second VWAP labeler, despite being less accurate for reference price definition, does
not have this gap-reset problem — it simply computes from whatever recent trades exist.

---

## Implication for the Planned T2 Fix

T2 planned to change the labeler's reference from 30s VWAP to 5-min sticky (to match the
exit module). If this change is made as specified:

1. The labeler would also use a 5-minute buffer
2. After the 538.6-second pre-halt gap, the labeler's buffer would also age out
3. The labeler would also be INACTIVE at 11:15 AM (no band to breach)
4. The IDAI halt would NOT be detected by the corrected labeler
5. Halt count: **1 → 0**
6. T3 hard stop triggers: "halt count still ≤1 after fix"

The planned T2 fix would make the situation strictly worse for the val-100 sample.

---

## Possible Paths Forward

Three paths, all require explicit approval before implementation:

**Path A — Fix the exit module's gap behavior (per SIP spec §4)**
Modify `LuldProximityExit` to freeze the last known reference price during gaps rather
than aging it out. When the buffer ages out completely, keep `_published_ref` at its
last valid value instead of resetting to 0.0. This matches the SIP spec ("if no eligible
trades in 5 min, the previous reference price stays in effect"). The exit module would
then be SAFE/active at 11:15 AM, holding a reference from the last pre-gap trade at
11:06 AM (ref≈1.4757, upper≈1.62). With price at 1.73 at the halt, bid_proximity_pct
would be negative — well within the 1% threshold — and EXIT_HALT would fire.
Also apply the same fix to the labeler (aligning both).
**Risk:** changes the exit module's behavior, which may affect PF results on the full val run.

**Path B — Run full val set (1,228 events) to find more halt labels**
The 100-event sample has only 1 halt. The full val set would likely contain multiple halts,
some where the exit module was active (continuous trading through the halt window). This
would make recall measurable without changing any code.
**Risk:** computationally expensive; still a diagnostic, not a fix.

**Path C — Proceed with labeler fix (T2 as planned) accepting halt count drop**
Fix the labeler to use 5-min sticky reference, knowing IDAI will no longer be detected.
Accept that val-100 will have 0 halt labels. Proceed to full val (Path B) for recall measurement.
**Risk:** hard stop at T3. Must be explicitly approved to override T3 escalation criterion.

---

## Data Note

The `load_trades()` loader returns a single-day dataset for IDAI 2024-02-16 (8,106 trades,
08:00 ET to 19:59 ET including pre/post market). The multi-day parquet file spans Feb 13–22;
the loader correctly filters to Feb 16 only. The gap pattern documented above is from the
Feb 16 trading session.

IDAI's trade frequency on Feb 16 (a penny stock with 62.34% gap): 132 gaps > 60s across
the session, many exceeding 5 minutes. This is atypical vs. the higher-cap scanner events
in the val sample and represents an extreme case for the buffer-aging behavior.
