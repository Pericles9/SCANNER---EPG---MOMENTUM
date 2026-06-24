# T1 Status Report — Phase LULD-V3 Reconciliation

**Date:** 2026-06-19

## What is actually in the repo

### Module: `backtest/core/exits/luld_proximity.py`

**Version present:** Phase LULD-REBUILD (neither V1 nor V2-as-spec-describes)

| Property | V1 (old) | V2 per spec | What's actually in repo |
|----------|----------|-------------|--------------------------|
| Signal basis | `n_spread_multiple × bid-ask spread` distance to band | Quote-based bid proximity to upper band, sustained for `luld_exit_duration_sec` | Quote-based bid proximity to upper band, **immediate fire** (no duration clock) |
| Free parameter | `n_spread_multiple` (integer spread multiples) | `luld_exit_duration_sec` (seconds at pin) | `proximity_threshold` (fraction of upper band, e.g. 0.02) |
| Duration clock | No | Yes | **No** |
| Reference price | Rolling mean, no stickiness | Sticky 1% SIP approximation | Sticky 1% SIP approximation ✓ |
| Lower band | Present | Permanently disabled | Permanently disabled ✓ |

The current module fires `EXIT_HALT` on the **first trade tick** where `(upper_band - bid) / upper_band ≤ proximity_threshold`. There is no `luld_exit_duration_sec` parameter, no pin duration tracking, and no concept of "sustained" proximity before firing.

### Config: `config/phase_luld_rebuild.json`

```json
"luld": {
  "proximity_threshold": 0.02,
  "ref_window_sec": 300.0,
  "warmup_sec": 60.0,
  "lower_band_enabled": false
}
```

No `luld_exit_duration_sec` key exists anywhere in any config file.

### Results present: `results/phase_luld_rebuild/`

- `t4_baseline_summary.json` — proximity_threshold=0.02, 100-event val (seed=42): PF=2.2766, luld_upper n=39, PF=146.80
- `t5_thresh{0.005,0.010,0.015,0.030,0.040}_summary.json` — sweep of `proximity_threshold` over [0.005–0.040]
- Best overall PF: thresh=0.005, PF=2.3809 (no winner selected by Cooper)

## The gap this creates for Phase LULD-V3

The V3 spec's T5 sweep is `luld_exit_duration_sec` over [2, 4, 6, 8, 10, 12]. This parameter does not exist in the current module. The T4 "re-run V2 config through the new scorer" would use a config key that has no effect.

**Two interpretations of V2:**

1. **V2 = Phase LULD-REBUILD as implemented** (proximity-only, no duration). T5 would then sweep `proximity_threshold` (already done in Phase LULD-REBUILD T5), or the spec's duration sweep would require implementing the duration clock first as new work.

2. **V2 = proximity + duration clock** (as spec describes). This was never built. Before T2 is run, the duration clock mechanism needs to be added to the module.

## Hard stop

Per T1 escalation rule: "If V2 is not actually present in the repo, stop and report; do not silently re-implement it as part of this phase."

**What is confirmed present:**
- Quote-based sticky-ref proximity module (Phase LULD-REBUILD)
- T5 sweep over `proximity_threshold`, no winner selected
- No `luld_exit_duration_sec` anywhere in the codebase

**What is NOT present:**
- Pin+duration clock mechanism
- `luld_exit_duration_sec` parameter
- Any V2 config or results directory

**Awaiting instruction before proceeding to T2.**
