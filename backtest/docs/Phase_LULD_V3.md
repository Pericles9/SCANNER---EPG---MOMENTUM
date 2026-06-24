---
tags:
  - type/results
  - domain/backtest
  - domain/microstructure
  - project/src-core
  - status/wip
created: 2026-06-19
last_reviewed: 2026-06-19
---

# Phase LULD-V3 Results

## Objective

Add a pin+duration clock to the LULD proximity exit and evaluate signal quality
against ground-truth halt labels. Prior phases (LULD-REBUILD T4/T5) established
that quote-based proximity at `threshold=0.010` improves overall PF vs the spread-
multiple approach, but fired without any requirement that the condition be sustained.
V3 asks: does requiring N seconds of continuous proximity before firing improve
signal purity?

**V3 adds:**
- `luld_exit_duration_sec` parameter: proximity must be sustained for this many
  seconds before `EXIT_HALT` is triggered; clock resets if bid leaves zone.
- Confusion-matrix scoring against `detect_luld_halts()` ground truth.
- Composite objective: `w_recall*recall - w_fp*fp_rate - w_liq*mean_liq_penalty`.

---

## Lineage

| Version | Signal | Reference | Notes |
|---------|--------|-----------|-------|
| V1 | Spread-multiple band | Phase E/F config | Symmetric then asymmetric LULD |
| V2 | Quote-based NBB bid + sticky ref | LULD-REBUILD T4/T5 | Immediate fire, no duration |
| V3 | Quote-based + pin+duration clock | This phase | Requires sustained proximity |

V2 winner (threshold=0.005, overall PF=2.381 on 100-event val sample) is the
baseline that V3 must beat on the full-val runner to be actionable.

---

## Implementation

### Files Changed

| File | Change |
|------|--------|
| `core/exits/luld_proximity.py` | Added `luld_exit_duration_sec` param + `_pin_start_ns` state; pin+duration clock in update loop |
| `core/exits/luld_scoring.py` | New module: FireEvent, HaltLabel, EventScore, score_fires(), aggregate_scores() |
| `backtest/runner.py` | Wired `--luld-exit-duration` CLI arg + `luld_exit_duration_sec` config key |
| `scripts/luld_v3_label.py` | T2: detect halt labels for 100-event val sample |
| `scripts/luld_v3_replay.py` | T4/T5: full-session LULD replay + confusion-matrix scoring |
| `config/phase_luld_v3.json` | New config; does not modify phase_f.json or phase_luld_rebuild.json |

### Pin+Duration Clock Logic

```
if fires AND in_limit_state:            # already confirmed halt → EXIT_HALT immediately
    state = EXIT_HALT
elif fires AND duration <= 0:           # V2 behaviour: fire immediately
    state = EXIT_HALT
elif fires AND duration > 0:
    if pin_start is None: pin_start = now
    elapsed = now - pin_start
    if elapsed >= duration:
        state = EXIT_HALT               # sustained → fire
    else:
        state = SAFE                    # not yet sustained → suppress
elif not fires:
    pin_start = None                    # reset clock
    state = SAFE
```

The `_in_limit_state` flag carries across ticks so a sustained halt is not
re-gated after the first fire.

### Scoring Objective

```
composite = w_recall * recall - w_fp * fp_rate - w_liq * mean_liq_penalty
```

Defaults: `w_recall=3.0`, `w_fp=1.0`, `w_liq=1.0`, `position_value_usd=1000.0`.

TP = fire within `pre_halt_window_sec=15s` before a halt start.
FP = fire with no halt within 15s after it.
FN = halt with no preceding fire within 15s.
Matching is greedy 1-to-1 (earliest eligible fire per halt, each fire used once).

---

## T2 — Halt Label Generation

**Script:** `scripts/luld_v3_label.py`
**Output:** `results/phase_luld_v3/halt_labels.json`

Parameters: `band_tier=tier2`, `limit_state_seconds=15`, `halt_gap_seconds=300`.

| Metric | Value |
|--------|-------|
| Events processed | 100 |
| Events with halts | 1 |
| Total halt windows | 1 |
| Halt event | IDAI 2024-02-16 |
| Halt window (UTC) | 16:15:27 – 16:25:28 (~11:15–11:25 AM ET) |

**Finding — halt sparsity:** Only 1 halt in 100 scanner events. This is expected:
Tier 2 LULD bands are 10% wide; scanner stocks gap 30–50% but must then trade into
the band during RTH to trigger a halt. The 300-second gap requirement further limits
detection to genuine exchange halts, not brief limit-state touches.

With a single halt label the scoring metrics are degenerate: any configuration
that misses IDAI scores recall=0.0, and any configuration that catches it scores
recall=1.0. Precision and FP-rate are the only discriminating metrics.

---

## T4 — Baseline Scoring (V2 Behaviour, duration=0)

**Config:** `proximity_threshold=0.010`, `luld_exit_duration_sec=0.0`

| Metric | Value |
|--------|-------|
| n_fires | 888 |
| n_halts | 1 |
| TP | 0 |
| FP | 888 |
| FN | 1 |
| recall | 0.0000 |
| precision | 0.0000 |
| fp_rate | 1.0000 |
| mean_liq_penalty (bps) | 36.26 |
| composite | −34.18 |

**Events with ≥1 fire:** 59/100
**Fire distribution:** range 0–63, median 2 per event
**Top fire producers:** GXAI 2024-03-13 (63), SXTC 2024-04-16 (60), CETY 2024-02-14 (58)

**Critical finding — band computation mismatch:**
IDAI itself generated 0 fires. The halt labeler (`detect_luld_halts`) uses a
rolling 30-second VWAP as the reference price (matching how SIP computes real LULD
bands). `LuldProximityExit` uses a 5-minute sticky reference. These produce different
bands; the module never placed IDAI's bid within 1% of its computed band, so the one
real halt was completely invisible to the proximity signal.

Implication: **recall is structurally 0 for all duration configs in this sweep.**
The 888 FP fires come from events where stocks happened to reach within 1% of the
module's band (a high-momentum move into the computed ceiling), but the band is not
calibrated to the actual SIP halt threshold.

---

## T5 — Duration Sweep

**Config:** `proximity_threshold=0.010`, duration ∈ {2, 4, 6, 8, 10, 12} seconds

| duration (s) | n_fires | TP | FP | FN | recall | fp_rate | mean_liq_penalty | composite |
|---|---|---|---|---|---|---|---|---|
| 0 (T4 baseline) | 888 | 0 | 888 | 1 | 0.0000 | 1.0000 | 36.26 | −34.18 |
| 2 | 178 | 0 | 178 | 1 | 0.0000 | 1.0000 | 34.82 | −15.22 |
| 4 | 132 | 0 | 132 | 1 | 0.0000 | 1.0000 | 38.42 | −17.97 |
| 6 | 105 | 0 | 105 | 1 | 0.0000 | 1.0000 | 29.16 | −13.14 |
| 8 | 81 | 0 | 81 | 1 | 0.0000 | 1.0000 | 26.75 | −8.33 |
| 10 | 75 | 0 | 75 | 1 | 0.0000 | 1.0000 | 20.62 | −6.38 |
| 12 | 67 | 0 | 67 | 1 | 0.0000 | 1.0000 | 19.08 | −6.33 |

All configs: recall=0.0000, fp_rate=1.0000 (IDAI never fires from proximity module).

**Fire reduction:** 888 → 178 at dur=2 (−80% in one step), then diminishing:
2→4: −26%, 4→6: −21%, 6→8: −23%, 8→10: −7%, 10→12: −11%.

**Mean liq penalty non-monotone at dur=4 (38.42 bps):** artifact of which events
survive the 4-second filter — the surviving fires happen to be in more illiquid
conditions. This is a small-sample effect (100 events, only 59 with fires).

**Composite plateau:** dur=10 (−6.38) and dur=12 (−6.33) are nearly identical,
indicating diminishing returns beyond 10 seconds.

---

## T6 — Winner Selection (BLOCKED: Cooper must select)

Given that recall=0 for all configs (band mismatch means the one halt is
structurally invisible), winner selection must be made on FP suppression alone:

**Criterion for T6 selection:**
> Choose the smallest `luld_exit_duration_sec` where the decline in n_fires
> plateaus — i.e., the marginal FP reduction from adding more seconds becomes
> small. If further FP reduction comes at the cost of delayed exit on genuine
> halt events (unknown from this sample), prefer the shorter duration.

Cooper must select a winner from the T5 table before T6 per-event charts are built.

---

## Escalation — Band Computation Misalignment

The core finding of V3 scoring: the module's 5-minute sticky reference does not
approximate the SIP rolling 30-second VWAP used to compute real LULD halt levels.
This means:

1. The module may fire near **the module's band** (a 10% ceiling above where the
   stock was trading 5 minutes ago) without the stock being near an actual halt.
2. The module misses **actual halts** where SIP bands tighten dynamically during
   a fast move.

**V3 does not fix this misalignment.** The duration clock reduces spurious fires
from brief band touches but does not bring the reference price closer to SIP.

**Future consideration (not a current phase):** Replace the sticky reference with
a rolling 30-second VWAP reference to align the module's band with actual LULD
trigger levels. This is a meaningful re-engineering of `LuldProximityExit` and
requires its own phase.

---

## Open Questions (T6 blocked)

1. **Cooper winner selection:** Which `luld_exit_duration_sec` from T5 sweep?
2. **Full-val runner comparison:** Does the selected duration improve on V2 best
   (PF=2.381) when run through `backtest.runner`?
3. **Band misalignment fix:** Is it worth a dedicated phase to replace the sticky
   reference with a rolling 30s VWAP?

---

## Related

- [[Phase_LULD_REBUILD_Results]] — V2 proximity threshold sweep (T4/T5 PF results)
- `core/exits/luld_proximity.py` — module with pin+duration clock
- `core/exits/luld_scoring.py` — confusion-matrix scoring module
- `core/features/luld_halt_detection.py` — halt labeler using 30s VWAP bands
