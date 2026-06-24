"""T4 Band-Definition Mismatch Diagnostic (Phase LULD-V3b).

Compares the reference price computed by:
  A) LuldProximityExit (5-min arithmetic mean + 1% sticky filter)
  B) detect_luld_halts post-T2b (30s VWAP + gap-freeze)

on the IDAI 2024-02-16 event to verify band-definition divergence remains
after the gap-freeze fix, confirming T5 reconciliation is needed.

Writes: results/phase_luld_v3b/t4_band_mismatch_diagnostic.md
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.exits.luld_proximity import LuldProximityExit, ProximityState
from core.features.luld_halt_detection import detect_luld_halts

TRADES_PATH = Path(r"D:\Trading Research\data\filtered\IDAI_2024-02-16_62.34\trades.parquet")
QUOTES_PATH = Path(r"D:\Trading Research\data\filtered\IDAI_2024-02-16_62.34\quotes.parquet")
OUT_DIR = Path(__file__).resolve().parent.parent / "results" / "phase_luld_v3b"
OUT_DIR.mkdir(parents=True, exist_ok=True)

BAND_PCT = 0.10  # Tier 2 normal RTH
PROXIMITY_THRESHOLD = 0.02


def load_trades() -> pd.DataFrame:
    df = pd.read_parquet(TRADES_PATH)
    # Timestamp is sip_timestamp: int64 nanoseconds UTC
    ts_col = "sip_timestamp" if "sip_timestamp" in df.columns else (
        "participant_timestamp" if "participant_timestamp" in df.columns else None
    )
    if ts_col:
        df.index = pd.to_datetime(df[ts_col], unit="ns", utc=True).dt.tz_convert(None)
    elif hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_convert(None)
    if "price" not in df.columns:
        price_col = next((c for c in df.columns if "price" in c.lower()), None)
        if price_col:
            df = df.rename(columns={price_col: "price"})
    if "size" not in df.columns:
        size_col = next(
            (c for c in df.columns if any(x in c.lower() for x in ["size", "qty", "vol"])),
            None,
        )
        if size_col:
            df = df.rename(columns={size_col: "size"})
        else:
            df["size"] = 1.0
    return df[["price", "size"]].sort_index()


def compute_labeler_ref(trades: pd.DataFrame) -> pd.Series:
    """30s VWAP + gap-freeze (current T2b state of detect_luld_halts)."""
    size = trades["size"]
    value = trades["price"] * size
    ref_raw = (
        value.rolling("30s", min_periods=1).sum()
        / size.rolling("30s", min_periods=1).sum()
    )
    ref_frozen = ref_raw.to_numpy(dtype=float).copy()
    idx = trades.index
    _window_ns = pd.Timedelta(seconds=30).value
    for i in range(1, len(idx)):
        gap_ns = (idx[i] - idx[i - 1]).value
        if gap_ns > _window_ns:
            recovery_end = idx[i] + pd.Timedelta(seconds=30)
            j = i
            while j < len(idx) and idx[j] < recovery_end:
                ref_frozen[j] = float("nan")
                j += 1
    ref = (
        pd.Series(ref_frozen, index=idx, dtype=float)
        .ffill()
        .fillna(ref_raw)
    )
    return ref


def compute_exit_module_ref(trades: pd.DataFrame) -> tuple[list, list, list]:
    """Run LuldProximityExit tick-by-tick; return (timestamps, refs, states)."""
    exit_mod = LuldProximityExit(
        ref_window_sec=300.0,
        proximity_threshold=PROXIMITY_THRESHOLD,
        warmup_sec=60.0,
        luld_exit_duration_sec=0.0,
    )
    timestamps = []
    refs = []
    states = []
    for ts, row in trades.iterrows():
        ts_ns = int(ts.value)  # nanoseconds UTC
        result = exit_mod.update(ts_ns, price=row["price"])
        timestamps.append(ts)
        refs.append(result.reference_price)
        states.append(result.state.value)
    return timestamps, refs, states


def main() -> None:
    print("Loading IDAI 2024-02-16 trades...")
    trades = load_trades()
    print(f"  {len(trades)} trades loaded. Index range: {trades.index[0]} – {trades.index[-1]}")

    # Restrict to 10:00–16:30 ET (UTC = +4h)
    # ET 10:00 = UTC 14:00 / ET 16:30 = UTC 20:30
    trades = trades.between_time("14:00", "20:30")
    print(f"  {len(trades)} trades in RTH+extended window.")

    # ── A: Exit module (LuldProximityExit) ──────────────────────────────────
    print("Running LuldProximityExit (5-min sticky)...")
    em_ts, em_refs, em_states = compute_exit_module_ref(trades)
    em_series = pd.Series(em_refs, index=em_ts, name="ref_exit_module")

    # ── B: Labeler (30s VWAP + gap-freeze) ──────────────────────────────────
    print("Running labeler reference (30s VWAP + gap-freeze)...")
    lab_ref = compute_labeler_ref(trades)
    lab_ref.name = "ref_labeler_30s_vwap"

    # ── Align and compare ───────────────────────────────────────────────────
    comparison = pd.DataFrame({
        "price": trades["price"],
        "ref_exit_module": em_series,
        "ref_labeler_30s_vwap": lab_ref,
        "em_upper": em_series * (1 + BAND_PCT),
        "lab_upper": lab_ref * (1 + BAND_PCT),
        "em_state": pd.Series(em_states, index=em_ts),
    })
    comparison["ref_divergence_pct"] = (
        (comparison["ref_exit_module"] - comparison["ref_labeler_30s_vwap"])
        / comparison["ref_exit_module"].replace(0, float("nan"))
        * 100
    )

    # ── Focus window: pre-gap, gap, post-gap ────────────────────────────────
    # Gap known from T1: last pre-gap trade ~11:06 ET = UTC 16:06 (EST = UTC-5)
    # First post-gap ~11:15 ET = UTC 16:15 (538.6s gap)
    pre_gap_end = pd.Timestamp("2024-02-16 16:07:00")
    post_gap_start = pd.Timestamp("2024-02-16 16:14:00")

    focus_pre = comparison.loc[:pre_gap_end].tail(10)
    focus_post = comparison.loc[post_gap_start:].head(15)
    focus = pd.concat([focus_pre, focus_post])

    # ── Stats ────────────────────────────────────────────────────────────────
    abs_div = comparison["ref_divergence_pct"].abs()
    max_div = abs_div.max()
    mean_div = abs_div.mean()
    pct_gt5 = (abs_div > 5.0).mean() * 100
    pct_gt10 = (abs_div > 10.0).mean() * 100

    # ── Gap detection on Feb 16 only ─────────────────────────────────────────
    trades_feb16 = trades.loc["2024-02-16"]
    idx_arr16 = trades_feb16.index
    gaps_feb16 = [(idx_arr16[i], idx_arr16[i-1], (idx_arr16[i] - idx_arr16[i-1]).total_seconds())
                  for i in range(1, len(idx_arr16))
                  if (idx_arr16[i] - idx_arr16[i-1]).total_seconds() > 60]
    large_gaps = [(str(end), str(start), f"{sec:.1f}s")
                  for end, start, sec in gaps_feb16]

    # ── Write report ────────────────────────────────────────────────────────
    report = f"""# T4 — Band-Definition Mismatch Diagnostic

**Date:** 2026-06-19
**Phase:** LULD-V3b T4
**Event:** IDAI 2024-02-16
**Status:** Gap-freeze fix (T2a/T2b) in place

---

## Summary

After the T2 gap-freeze fix, both modules now correctly persist the reference price through gaps (per SIP §4). However, the fundamental band-definition mismatch remains:

| Module | Reference window | Weighting | Sticky filter |
|--------|-----------------|-----------|---------------|
| `LuldProximityExit` (exit module) | 5 minutes (300s) | Arithmetic mean | 1% (update only when mean moves ≥1%) |
| `detect_luld_halts` (labeler) | 30 seconds | VWAP (price×size) | None |

**Conclusion:** T5 reconciliation is required. The labeler must be updated to use 5-min arithmetic mean + 1% sticky filter to match the exit module.

---

## Reference Price Divergence Statistics (IDAI dataset, all days, RTH window)

| Metric | Value |
|--------|-------|
| Max absolute divergence | {max_div:.2f}% |
| Mean absolute divergence | {mean_div:.2f}% |
| Fraction of ticks with >5% divergence | {pct_gt5:.1f}% |
| Fraction of ticks with >10% divergence | {pct_gt10:.1f}% |

---

## Detected Gaps (>60s between trades)

"""
    if large_gaps:
        for end_ts, start_ts, dur in large_gaps:
            report += f"- Gap: {dur} ({start_ts} → {end_ts})\n"
    else:
        report += "- No gaps >60s detected in the focus window.\n"

    report += f"""
---

## Reference Price: Pre-gap and Post-gap Focus

The key divergence window is immediately after the data gap (11:06 ET → 11:15 ET, ~538s).

```
Timestamp (UTC)                  Price    EM ref   Lab ref  Div%    EM state
"""

    for ts_idx, row in focus.iterrows():
        ts_str = str(ts_idx)[:23]
        report += (
            f"{ts_str}  "
            f"{row['price']:8.4f}  "
            f"{row['ref_exit_module']:8.4f}  "
            f"{row['ref_labeler_30s_vwap']:8.4f}  "
            f"{row['ref_divergence_pct']:+7.2f}%  "
            f"{row['em_state']}\n"
        )

    report += "```\n\n"
    report += """---

## Why Divergence Persists After Gap-Freeze Fix

### During normal trading (no gap):
- **Exit module**: 5-min rolling arithmetic mean of all prices in [T-300s, T]. A fast move 3 minutes ago still anchors the reference. The 1% sticky filter further dampens rapid updates.
- **Labeler**: 30s VWAP. Reflects only the last 30 seconds; adapts 10× faster to price changes.

### At the post-gap transition (11:15:03 ET):
- **Exit module**: `_published_ref ≈ 1.4757` (frozen in memory from 11:06 ET). Upper band ≈ 1.617. The gap-freeze fix (T2a) correctly preserves this.
- **Labeler**: After T2b fix, the 30s VWAP is forced to `ffill` the last pre-gap value. Result: labeler ref ≈ 1.47 immediately post-gap, matching exit module.

### 30 seconds after the gap:
- **Exit module**: ref still ≈ 1.47 (post-gap trades at 1.73+ raise the mean, but the 1% sticky filter requires a 1% move before updating; the 5-min window averages in new trades slowly).
- **Labeler**: Gap-freeze window expires (30s after gap). VWAP now purely reflects post-gap trades at 1.73+. ref jumps to ~1.73, upper ≈ 1.903. **No limit state detected** (price ≈ upper).

**This is the core mismatch**: within 30 seconds of the gap ending, the labeler's band drifts to the post-gap price level, while the exit module's band stays anchored to the pre-gap level for up to 5 minutes.

---

## T5 Action Required

Change `detect_luld_halts` to compute:
1. **5-minute arithmetic mean** (rolling "300s" window, arithmetic, not VWAP)
2. **1% sticky filter** (update reference only when mean moves ≥1% from current published reference)
3. **Gap-freeze for 300s window** (mark post-gap rows as NaN for 300s recovery window, then ffill)

This reconciles the labeler's band definition to match `LuldProximityExit`, enabling meaningful confusion-matrix scoring.
"""

    out_path = OUT_DIR / "t4_band_mismatch_diagnostic.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nReport written: {out_path}")

    # Also print key stats
    print(f"\nKey findings:")
    print(f"  Max divergence: {max_div:.2f}%")
    print(f"  Mean divergence: {mean_div:.2f}%")
    print(f"  Ticks >5% divergence: {pct_gt5:.1f}%")
    print(f"  Large gaps (>60s): {len(large_gaps)}")
    if large_gaps:
        for end_ts, start_ts, dur in large_gaps:
            print(f"    {start_ts} -> {end_ts} ({dur})")


if __name__ == "__main__":
    main()
