# Phase LULD-V3c — T3: Matching / Timestamp-Anchor Audit

**Date:** 2026-06-20
**Verdict:** **CONFIRMED DEFECT.** The halt-start anchor used by the scorer sits at the
*wrong end* of the limit-state segment. This produces **0 true positives structurally**
across all 18 halt events at `dur=0` — independent of any duration threshold — and is the
root cause of the flat-near-zero recall in Phase LULD-V3b T6.

---

## Code references

**Labeler** (`core/features/luld_halt_detection.py:154`):
```python
halts.append(HaltWindow(start=seg_end, end=pd.Timestamp(halt_end), reason="luld"))
```
`HaltWindow.start` is set to **`seg_end`** — the *last in-band trade before the ≥300s gap*,
i.e. the moment trading froze. The labeler computes `seg_start` (the first tick where
`price >= upper_band`) at line 137–141 but **discards it**.

**Scorer** (`core/exits/luld_scoring.py:118-132`):
```python
fire_ts_sec = [f.timestamp_ns / 1e9 for f in fires]
halt_starts  = [h.start_sec for h in labels]      # == seg_end
...
lead = h_start - f_ts
if 0.0 <= lead <= pre_halt_window_sec and lead < best_lead:   # pre_halt_window_sec = 15.0
```
A fire counts as TP only if it lands in the 15 s *immediately before `seg_end`*.

---

## Why this guarantees 0 TP

Two facts collide:

1. **Segments are ≥15 s by construction.** The labeler only emits a halt when the in-band
   run lasts `limit_state_seconds = 15` or longer (`luld_halt_detection.py:143`). So
   `seg_end - seg_start ≥ 15 s` for every labeled halt.
2. **The exit fires during the *approach*, at or before `seg_start`.** `LuldProximityExit`
   fires when the **bid** is within `proximity_threshold` (1%) of the upper band. The bid
   reaches that zone as the price *runs up* to the band — at or before the price actually
   crosses into it (`seg_start`).

Therefore `fire_ts ≤ seg_start ≤ seg_end - 15`, so `lead = seg_end - fire_ts ≥ 15 s` for
essentially every fire — landing on or outside the 15 s TP boundary. The fire is correct and
*early*; the scorer marks it FP because the anchor is 15 s+ too late.

This also explains the **otherwise-paradoxical recall trend** in V3b T6: recall crept
0.0 → 0.125 as `dur` rose 0 → 12. A larger duration threshold *delays* the fire deeper into
the pin, nudging it closer to `seg_end`; a handful of late fires then squeak inside the 15 s
window. The duration clock was never the problem — it was moving fires toward a mis-placed
goalpost.

---

## Evidence (18 halt events, dur=0, 486 fires)

| Metric | Value |
|--------|------:|
| Total fires | 486 |
| **TP under CURRENT anchor (`seg_end`)** | **0** |
| TP under PROPOSED anchor (limit-state onset, 15 s pre-window) | 54 |
| Fires rescued by anchor change | 54 |

Worked example — **CRBP 2024-01-26**, halt `start_sec = 1706282000.9` (= `seg_end`),
limit-state onset `seg_start ≈ 1706281980.4` (segment ≈ 20 s):

| fire_ts (epoch s) | lead to seg_end | lead to seg_start | comment |
|------------------:|----------------:|------------------:|---------|
| 1706281939.2 | 61.7 s | 41.2 s | approach fire, 62 s before freeze — scored FP |
| 1706281944.1 | 56.8 s | 36.4 s | approach fire — scored FP |
| 1706281947.8 | 53.1 s | 32.7 s | approach fire — scored FP |

Every fire that *correctly anticipates* the CRBP halt by 30–60 s is scored FP, because the
TP window only opens 15 s before `seg_end`.

---

## Two distinct sub-issues

1. **Anchor (defect — fix in T5a).** The scorer must anchor on the limit-state *onset*
   (`seg_start`), the event the exit is built to anticipate — not `seg_end`, which is the
   gap/freeze moment used (correctly) elsewhere for active-timeline subtraction. Do **not**
   change `HaltWindow.start` globally; add a separate `limit_state_start_sec` field.

2. **Window semantics + width (partly parameter — flag for Cooper).** Even re-anchored to
   `seg_start` with a 15 s lead, the CRBP approach fires lead the onset by 30–60 s and stay
   FP. The correct TP region is the **whole limit-state run plus a lead**:
   `[seg_start − pre_halt_window_sec, seg_end]` — a fire anywhere in the pinned period is a
   timely exit. The proposed T5a scorer uses this window. How far before the onset an
   "approach" fire should still count (the 15 s lead vs. 60 s) is a metric-design choice;
   T5a keeps 15 s as the locked value and flags the width for Cooper.

---

## Fix (implemented in T5a)

- Labeler records `limit_state_start_sec = seg_start` alongside `start_sec`/`end_sec`.
- Scorer matches a fire as TP if `seg_start − pre_halt_window_sec ≤ fire_ts ≤ seg_end`.
- FP = fires outside every halt's `[seg_start − pre_window, seg_end]` window.
- FN = halts with no fire inside their window.
- Regression test uses the CRBP fixture (approach fires that must now score TP).
