# T1 — Gap-Freeze Bug Diagnostic

**Date:** 2026-06-19
**Phase:** LULD-V3b T1
**Events examined:** IDAI 2024-02-16 (the one halt in the 100-event val sample)

---

## Summary

Both modules have gap-related reference-price problems, but via different mechanisms:

| Module | Mechanism | Goes INACTIVE? | Per SIP §4? |
|--------|-----------|---------------|-------------|
| `LuldProximityExit` | Streaming deque evicted on gap > 300s → warmup reset | **Yes** | **No — bug** |
| `detect_luld_halts` | Vectorized 30s rolling VWAP adapts to post-gap prices in ≤30s | No | **No — different bug** |

---

## Module 1: LuldProximityExit (`core/exits/luld_proximity.py`)

### Precise bug mechanism

The relevant code path (lines 209–227):

```python
self._buffer.append((timestamp_ns, price))
cutoff = timestamp_ns - self._ref_window_ns          # now - 300s
while self._buffer and self._buffer[0][0] < cutoff:
    self._buffer.popleft()                           # evict all pre-gap entries

oldest_ts = self._buffer[0][0]                      # = timestamp_ns (fresh trade only)
if (timestamp_ns - oldest_ts) < self._warmup_ns:   # ~0 < 60s → True
    self._in_limit_state = False
    self._pin_start_ns = None
    return ProximityResult(
        state=ProximityState.INACTIVE,
        reference_price=0.0,                        # ← published_ref ignored
        upper_band=0.0,
        ...
    )
```

Step-by-step for a gap > 300s:
1. The pre-gap trade at 11:06:04 ET leaves `_published_ref ≈ 1.4757` and `_buffer` populated.
2. No trades arrive for 538.6 seconds. Nothing happens during the gap (streaming module).
3. First post-gap trade arrives at 11:15:03 ET.
4. `self._buffer.append(...)` adds the new trade.
5. `cutoff = 11:15:03 - 300s = 11:10:03`. All pre-gap buffer entries (latest: 11:06:04) are older than 11:10:03 → **all evicted**.
6. Buffer now contains exactly 1 entry: the 11:15:03 trade.
7. `oldest_ts = 11:15:03`. `timestamp_ns - oldest_ts ≈ 0 < 60s` → warmup condition triggers.
8. Returns `INACTIVE` with `reference_price=0.0, upper_band=0.0`.
9. **`_published_ref` still holds `1.4757` in memory** — it was never reset. But the early return prevents it from being used.
10. The halt begins at 11:15:27 ET — 24 seconds after the gap ended, still during the 60-second warmup. The module is blind.

**Key insight:** `_published_ref` survives the gap (never explicitly zeroed). The warmup gate at step 7 is what blocks it from being used. The fix is to condition the warmup gate on whether `_published_ref` has ever been set.

**Escalation check — T1:** The module does NOT already correctly persist the reference price. The check (`in_warmup and self._published_ref == 0.0`) would fix this. The fix is needed.

---

## Module 2: detect_luld_halts (`core/features/luld_halt_detection.py`)

### Precise mechanism (not an INACTIVE bug — different issue)

The labeler is vectorized (batch computation, not streaming). The key line (line 101):

```python
ref_price = (value.rolling("30s", min_periods=1).sum() /
             size_series.rolling("30s", min_periods=1).sum()).ffill()
```

Behavior during a gap > 30s:
1. The dataframe has no rows during the gap (only trade timestamps are indexed).
2. At the first post-gap trade (11:15:03 ET), the rolling 30s window looks back from 11:15:03 to 11:14:33 — finding **no pre-gap trades** (gap was 11:06 → 11:15).
3. With `min_periods=1`, the window is satisfied by the current trade itself: `ref_price = trade_price` at that tick.
4. No INACTIVE state, no warmup. The module just starts fresh.
5. The `.ffill()` only fills NaN values, which never occur (min_periods=1 prevents them). It has no effect here.

**How IDAI was detected despite the gap:** Post-gap prices started lower and ramped up. The first post-gap trade established a reference. As prices rose above the band ceiling (10% above that reference), the limit state was triggered. This was coincidental — the labeler happened to catch the halt because the initial post-gap price produced a band that subsequent trades exceeded.

**The reference-persistence problem (same SIP §4 violation, different form):** Per SIP §4, the reference price should stay at the pre-gap value (1.4757 from 11:06 ET) through the gap. Instead, the 30s VWAP reference resets to the post-gap price level within 30 seconds of trading resuming. The labeler violates SIP §4 through a fast reset, not through going INACTIVE.

**Escalation check — T1:** The labeler does NOT correctly persist the reference price through gaps. Its detection of IDAI was via a different mechanism (post-gap price trajectory) not through correct gap-freeze behavior. The fix is needed.

---

## IDAI Sequence: Exit Module State Transitions (with values)

From the V3a T1 diagnostic:

| Time (ET) | Event | Exit Module State | ref | upper |
|---|---|---|---|---|
| 11:06:01 | Last pre-gap trade | SAFE | 1.4757 | 1.62 |
| 11:06:04 → 11:15:03 | Gap (538.6s > 300s buffer) | — | `_published_ref=1.4757` in memory | — |
| 11:15:03 | First post-gap trade | **INACTIVE** | 0.0 (returned) | 0.0 |
| 11:15:27 | Halt begins | **INACTIVE** | 0.0 | 0.0 |
| 11:26:28 | Post-halt recovery | SAFE | 2.0288 | 2.23 |

At 11:15:03: buffer has 1 entry, `oldest_ts ≈ timestamp_ns`, warmup condition fires.
At 11:15:27 (halt): module still in warmup (only 24s elapsed, need 60s).

---

## What the Fix Achieves

### LuldProximityExit (T2a):
- Condition the warmup gate on `_published_ref == 0.0` (cold start) only.
- When `_published_ref > 0.0` and the buffer is sparse (gap-induced), skip the INACTIVE return.
- Use the persisted `_published_ref` (1.4757) immediately.
- At 11:15:03: reference = 1.4757, upper = 1.617. With IDAI trading at ~1.73+, bid_proximity_pct would be negative → **EXIT_HALT fires immediately**.

### detect_luld_halts (T2b):
- After computing the 30s rolling VWAP, detect gaps > 30s and mark all timestamps in the recovery window as NaN.
- Forward-fill to carry the pre-gap reference through the recovery window.
- At 11:15:03: pre-gap ref (~1.47) carried forward, upper ≈ 1.617. Prices at 1.73 → limit state triggered immediately.
- This enables sustained limit-state detection for the 15-second threshold.

---

## Why the Two Fixes Differ

The two modules have different architectures (streaming vs. vectorized), so the fixes cannot share exact implementation:

| Aspect | LuldProximityExit fix | detect_luld_halts fix |
|--------|----------------------|----------------------|
| Architecture | Streaming, tick-by-tick | Vectorized, batch |
| Fix mechanism | Skip warmup gate when `_published_ref > 0.0` | Mark post-gap rows NaN + ffill |
| Reference used | `_published_ref` (already persisted in memory) | Last pre-gap rolling VWAP value (ffilled) |
| Shared code | No (different data models) | Conceptually shared: both implement "carry last valid reference through gaps" |

Shared concept, separate implementations. "Sharing code where practical" per spec = not practical here.

---

## No Escalation

Neither module correctly persists the reference price through gaps. Both need fixes. Proceeding to T2.
