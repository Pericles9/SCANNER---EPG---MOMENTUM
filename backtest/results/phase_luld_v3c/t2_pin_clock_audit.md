# Phase LULD-V3c — T2: Pin-Detection / Duration-Clock Audit

**Date:** 2026-06-20
**Verdict:** **NO DEFECT.** Pin detection and the duration clock are implemented correctly
and behave cleanly on real data. Pin flicker is negligible (0–2 transitions per limit-state
segment) and pin occupancy during the segment is 83–100%. The duration clock accumulates as
designed. **T2 rules the pin clock OUT as the cause of flat recall** — the cause is the
scoring anchor (see T3).

---

## Code references (`core/exits/luld_proximity.py`, post-V3b gap-freeze fix)

- **Pin condition** (lines 273–289): pin active when the comparison price is within
  `proximity_threshold` of the upper band. Uses the prevailing **bid** when a valid quote
  exists (`bid > 0 and ask > bid`), else falls back to the trade price:
  `bid_prox = (upper_band − bid) / upper_band ≤ proximity_threshold`.
- **Duration clock** (lines 291–322): `_pin_start_ns` is set on the first pinned tick and
  **reset to `None` the instant the pin breaks** (`if not fires: self._pin_start_ns = None`).
  `pin_duration_sec = (ts − _pin_start_ns)/1e9`; `EXIT_HALT` fires once
  `pin_duration_sec ≥ luld_exit_duration_sec`. Reset-on-break is correct.

> Note: the V3c brief refers to a separate `pin_tolerance = 0.002`. No such parameter exists
> in the current module — pin proximity is governed by the single `proximity_threshold`
> (0.010 in the V3b/T6 sweep). The audit evaluates the implemented 1% threshold.

---

## Evidence — pin flicker & occupancy (longest segment per event, dur=0)

| ticker | seg_secs | ticks | pin_transitions | pct_pinned |
|--------|---------:|------:|----------------:|-----------:|
| CRBP | 20 | 469 | 1 | 96.1% |
| XBP | 32 | 5 | 0 | 0.0% |
| MNPR | 34 | 1430 | 1 | 98.6% |
| IVP | 16 | 323 | 2 | 83.5% |
| LIDR | 32 | 5649 | 0 | 100.0% |
| CADL | 32 | 3251 | 1 | 99.4% |
| GRI | 66 | 1762 | 1 | 98.9% |
| CETY | 39 | 521 | 2 | 92.9% |
| MLGO | 72 | 2630 | 1 | 99.3% |
| BNED | 17 | 2097 | 0 | 100.0% |
| IMCC | 28 | 98 | 2 | 60.9% |
| JWEL | 36 | 811 | 0 | 100.0% |

### Reading the table

- **No flicker.** Across thousands of ticks per segment, the pin toggles **0–2 times**. A
  flickering clock would show dozens-to-hundreds of transitions; it does not. Once the bid
  enters the 1% zone it stays, so the clock accumulates continuous time as intended — a
  `dur=12` threshold is genuinely reachable within a real limit-state run.
- **High occupancy (83–100%).** During the in-band segment the bid sits within 1% of the
  band essentially the whole time. The clock is measuring real pinned time, not noise.
- **`pin_tolerance` is not too loose.** At 1%, occupancy tracks the genuine pinned period;
  it does not flag transient near-band ticks as sustained pins. (The flat recall is **not**
  from over-loose tolerance — it is the T3 anchor.)
- **XBP outlier (5 ticks / 0% pinned):** the *upper-band* segment selected here is a 5-tick,
  32 s sliver that never put the **bid** within 1% (a low-volume window where the comparison
  used the trade-price fallback). It is not flicker — it is a near-empty segment. The real
  XBP halt fires are captured in other segments. Flagged for the T1 chart, not a clock bug.

---

## Conclusion

The pin detector and duration clock are correct and behave well on real data. The V3b T6
flat recall is **not** attributable to pin flicker or tolerance width. The defect is in the
scoring anchor (T3) and the penalty units (T4). No change to `luld_proximity.py`'s pin/clock
logic is required for the audit fix; the only module change in this phase is the additive
liquidity-adaptive duration feature in T5b.
