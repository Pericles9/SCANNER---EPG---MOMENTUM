---
tags:
  - type/results
  - domain/backtest
  - domain/signal
  - project/scanner-epg-momentum
  - status/revisit
created: 2026-06-08
last_reviewed: 2026-06-08
linked_code: "[[core/epg/gate_variants.py]]"
---

# WJISlowEMAGate — Phase WJI-SlowEMA Results

## Motivation

The WJI-OPT `RunningMaxGate` uses a monotonically non-decreasing running peak as the
participation reference. This peak never decays — once the session's WJI maximum is
established, all subsequent entry thresholds are anchored to it. The hypothesis here was
that a **slow EMA of WJI** would be a more adaptive reference: it tracks the current
momentum level rather than locking in a historical peak, potentially producing better
calibrated entries during the sustained-acceleration phase of a momentum move.

## Gate Mechanics

### Signal

Pre-computed WJI passed in by the caller (identical to WJI-OPT):

```
WJI(t) = norm_λ_V(t)^α × norm_λ_buy_slow(t)^(1-α)
```

with `α=0.50`, `τ_V=180s`, `β_slow=0.01` (consistent with WJI-OPT best config).

### Slow EMA reference

```
WJI_slow(t) = WJI_slow(t-dt) × exp(-ln2·dt/τ_slow) + WJI(t) × (1 - exp(-ln2·dt/τ_slow))
```

`dt` is **halt-adjusted active seconds** (not wall-clock) — see [Halt Detection](#halt-detection).

Initialised to the **first non-zero WJI value** seen after `activate()` is called, to avoid
a zero-denominator at the first post-warmup state check.

### State machine

```
FAIL → PASS : WJI(t) >= p_open  × WJI_slow(t)
PASS → FAIL : WJI(t) <  p_close × WJI_slow(t)
Dead zone   : p_close × WJI_slow ≤ WJI < p_open × WJI_slow  →  hold current state
```

No peak ratchet. The reference adapts downward when momentum decelerates.

### Key difference from RunningMaxGate

| Property | RunningMaxGate | WJISlowEMAGate |
|----------|---------------|----------------|
| Reference | running maximum (non-decreasing) | slow EMA (bidirectional) |
| Threshold after peak | only rises | decays with signal |
| Exit trigger | WJI drops below p_close × peak | WJI drops below p_close × EMA |
| Stale peak risk | Yes — locked to session high | No — adapts to current level |
| EMA chasing risk | N/A | Yes — reference follows signal down |

### Interface

```python
WJISlowEMAGate(
    tau_slow: float,        # EMA half-life in active seconds
    p_open: float,          # FAIL→PASS threshold multiplier
    p_close: float,         # PASS→FAIL threshold multiplier; must satisfy p_close < p_open
    warmup_seconds: float,  # default 300s (matches EPG warmup)
)

gate.activate(t_event_active_sec: float)  # active-seconds timestamp of T_event
gate.update(wji: float, dt_active: float) -> GateState
gate.wji_slow        # current EMA value
gate.threshold_open  # p_open  × wji_slow
gate.threshold_close # p_close × wji_slow
```

Source: `core/epg/gate_variants.py` — `WJISlowEMAGate` class (appended in Phase WJI-SlowEMA).

## Halt Detection

**T1 audit finding:** The Hawkes replay (`runner.py`), WJI signal computation
(`tools/phase_wji_opt/common.py`), and `epg_replay.py` all use wall-clock dt throughout.
`core/features/luld_halt_detection.py` is fully implemented but was not wired into any
existing replay path prior to this phase.

**T1b: wired into the new sweep runner only.** The `wji_slow_ema_worker` in
`tools/phase_wji_slow_ema/common.py` calls `prepare_active_trades(include_extended=True,
halt_gap_seconds=300)` per event and passes `active_seconds` as the dt source for both
WJI signal EMAs and the gate's `dt_active` parameter.

**T1e comparability note:** The WJI-OPT baseline (PF=1.1881) was computed without
halt-adjusted time. Across the 100-event val sample used here, 0 events had detected halts
(no LULD-style gaps ≥5 min during RTH trading). The two approaches produced numerically
identical results on this sample; the infrastructure is in place for live use cases where
halts occur.

## T3 Sweep — 25-Config Grid

**Sample:** 100-event val (seed=42), same as WJI-OPT baseline.  
**Gate close is the sole exit** — no EXIT_D, LULD proximity, or watermark.  
**SF entry gate active** (consistent with WJI-OPT baseline).

Grid parameters:

| Parameter | Values |
|-----------|--------|
| `tau_slow` | 300, 600, 900, 1200, 1800 s |
| `p_open` | 0.70, 0.75, 0.80, 0.85, 0.90 |
| `p_close` | 0.55 (fixed) |
| Total configs | 25 |

### Results (sorted by PF)

| config_id | τ_slow | p_open | PF | CVaR5 | n_trades | beats baseline PF |
|-----------|--------|--------|-----|-------|----------|-------------------|
| t300_po75 | 300 | 0.75 | **1.2219** | −20.20% | 1,375 | ✓ |
| t300_po80 | 300 | 0.80 | 1.2111 | −20.75% | 1,285 | ✓ |
| t300_po70 | 300 | 0.70 | 1.2091 | −19.66% | 1,478 | ✓ |
| t300_po85 | 300 | 0.85 | 1.1975 | −20.97% | 1,224 | ✓ |
| t300_po90 | 300 | 0.90 | 1.1551 | −21.57% | 1,171 | ✗ |
| t600_po70 | 600 | 0.70 | 1.1416 | −17.55% | 1,758 | ✗ |
| t1800_po70 | 1800 | 0.70 | 1.1388 | −17.60% | 1,597 | ✗ |
| t600_po80 | 600 | 0.80 | 1.1213 | −18.59% | 1,455 | ✗ |
| t1200_po70 | 1200 | 0.70 | 1.1204 | −16.90% | 1,715 | ✗ |
| t600_po75 | 600 | 0.75 | 1.1195 | −18.19% | 1,577 | ✗ |
| t900_po70 | 900 | 0.70 | 1.1193 | −16.79% | 1,764 | ✗ |
| t1800_po80 | 1800 | 0.80 | 1.1035 | −19.09% | 1,285 | ✗ |
| t1200_po75 | 1200 | 0.75 | 1.1027 | −17.52% | 1,541 | ✗ |
| t1800_po75 | 1800 | 0.75 | 1.0953 | −18.83% | 1,418 | ✗ |
| t1800_po90 | 1800 | 0.90 | 1.0937 | −20.37% | 1,092 | ✗ |
| t900_po75 | 900 | 0.75 | 1.0854 | −17.43% | 1,568 | ✗ |
| t1200_po80 | 1200 | 0.80 | 1.0804 | −18.27% | 1,383 | ✗ |
| t1200_po90 | 1200 | 0.90 | 1.0751 | −19.51% | 1,160 | ✗ |
| t1800_po85 | 1800 | 0.85 | 1.0750 | −20.09% | 1,174 | ✗ |
| t600_po85 | 600 | 0.85 | 1.0691 | −19.12% | 1,352 | ✗ |
| t1200_po85 | 1200 | 0.85 | 1.0600 | −19.21% | 1,265 | ✗ |
| t900_po80 | 900 | 0.80 | 1.0575 | −18.04% | 1,423 | ✗ |
| t600_po90 | 600 | 0.90 | 1.0522 | −19.39% | 1,262 | ✗ |
| t900_po85 | 900 | 0.85 | 1.0368 | −18.81% | 1,315 | ✗ |
| t900_po90 | 900 | 0.90 | 1.0289 | −19.65% | 1,222 | ✗ |

**Baseline (WJI-OPT p065_single):** PF=1.1881, CVaR5=−9.16%, n=2,134

**CVaR5 range: −16.79% to −21.57% across all configs. Zero configs meet CVaR5 ≥ −10%.**

T3b escalation fired. Phase stopped at T3.

### Stagnation diagnostic (T3c)

Per-event PASS↔FAIL transition counts ranged from ~57 to 113 transitions/event (smoke test
on MURA 2023-11-17). All configs well above the 8/event flag threshold. The gate cycles on
every tick-scale WJI fluctuation around the EMA reference, which moves too slowly to track
the signal. This is structural: with inter-trade dt of ~1s and τ_slow ≥ 300s, the EMA moves
<0.25% per second — far too slow to prevent threshold crossings from WJI noise.

## Why It Failed

**The EMA reference is adaptive in the wrong direction.** During a strong momentum move,
WJI peaks and then decelerates. The slow EMA chases the WJI level downward, which means:

1. `WJI_slow` tracks the decelerating signal → thresholds `p_open × WJI_slow` and
   `p_close × WJI_slow` both fall with it.
2. Gate stays PASS even as absolute WJI drops, because the ratio `WJI/WJI_slow` is
   maintained near 1.0 during deceleration.
3. Result: positions held through deceleration and reversal, producing deep tail losses.

The running-max gate (`RunningMaxGate`) avoids this because the peak never falls — once
momentum decelerates below the peak threshold, the gate exits. The EMA reference is
fundamentally inferior for detecting momentum exhaustion.

**CVaR5 comparison:**

| Gate | CVaR5 (best config) | Mechanism |
|------|--------------------|-|
| RunningMaxGate (WJI-OPT) | −9.16% | Peak locks in; decelerating WJI exits quickly |
| WJISlowEMAGate | −16.79% | Peak decays; deceleration keeps ratio near 1.0 |

## Potential Follow-On Work (not approved)

Three directions were noted at the T3b escalation:

1. **Tighten p_close** (e.g., 0.70–0.80) — narrows the dead zone, forces faster exits.
   Does not address the root cause (EMA chasing downward).

2. **EMA with floor at `WJI_peak × floor_pct`** — `WJI_slow = max(ema, peak × floor)`.
   Prevents the reference from chasing the signal down; retains slow-adapting property
   during accumulation. Hybrid of RunningMaxGate and WJISlowEMAGate.

3. **Abandon this gate variant** — `RunningMaxGate` is simpler and demonstrably better.
   Document as `status/abandoned`.

Decision pending Cooper's review.

## Files

| File | Description |
|------|-------------|
| `core/epg/gate_variants.py` | `WJISlowEMAGate` class |
| `tests/test_gate_variants.py` | 5 unit tests (T2c, appended) |
| `tests/test_halt_detection.py` | T1d halt detection unit tests |
| `tools/phase_wji_slow_ema/common.py` | Per-event worker with halt detection |
| `tools/phase_wji_slow_ema/t3_sweep.py` | 25-config sweep runner |
| `results/phase_wji_slow_ema/t3_sweep.json` | Full T3 results |
