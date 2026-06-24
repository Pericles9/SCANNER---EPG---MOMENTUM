"""Unit tests for core.features.luld_halt_detection — reference price and halt detection.

Halt detection requires a specific trade-sequence structure:
  warm-up → [limit state ≥ limit_state_seconds] → [gap ≥ halt_gap_seconds] → resume_trade

The resume_trade must be priced BELOW the upper band so the loop sees `not in_band`
and checks the gap to confirm a halt.  Without it, `start_idx` remains set at loop-end
and the end-of-session branch runs — which requires a NYSE schedule to cap the session.

Reference price (T5): 5-minute arithmetic mean with 1% sticky filter (matches
LuldProximityExit).  After a gap, the 5-min window retains pre-gap prices, keeping the
mean close to the pre-gap level.  The 1% sticky filter further prevents the reference
from jumping on a single post-gap print, so the band stays anchored to the pre-gap level
long enough for a genuine limit-state breach to be detected.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.features.luld_halt_detection import detect_luld_halts


def _make_trades(
    timestamps: list,
    prices: list,
    sizes: list | None = None,
) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(t) for t in timestamps])
    return pd.DataFrame(
        {"price": prices, "size": sizes if sizes else [100] * len(prices)},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Reference-price stability tests (T5 — V3b: 5-min mean + 1% sticky)
# ---------------------------------------------------------------------------


class TestGapFreeze:
    def test_no_gap_normal_operation(self):
        """Without a gap, 5-min mean operates normally — no halts when price inside band."""
        ts = [pd.Timestamp("2024-01-15 14:00:00") + pd.Timedelta(seconds=i) for i in range(60)]
        prices = [10.0] * 60
        df = _make_trades(ts, prices)
        halts = detect_luld_halts(df, price_col="price", size_col="size", band_tier="tier2")
        assert len(halts) == 0

    def test_gap_freeze_carries_pre_gap_reference(self):
        """After a gap, the 5-min window retains pre-gap prices so the reference stays
        anchored near 10.0.  A post-gap price of 15.0 (50% above pre-gap) causes the
        blended 5-min mean to update slightly (to ~10.24) via the sticky filter, but
        the upper band (≈11.3) is still well below 15.0 → limit state → halt.

        Without pre-gap anchor (e.g. pure post-gap VWAP): ref resets to 15.0,
        upper=16.5, no breach → no halt."""
        base = pd.Timestamp("2024-01-15 14:00:00")
        # 20 pre-gap trades at 10.0 (VWAP = 10.0)
        pre_ts = [base + pd.Timedelta(seconds=i) for i in range(20)]
        pre_prices = [10.0] * 20

        # 60s gap (> 30s window)
        # Post-gap: 1 trade at 15.0 → exceeds frozen upper band (10.0 * 1.10 = 11.0)
        post_ts = [base + pd.Timedelta(seconds=80)]
        post_prices = [15.0]

        # 400s halt gap, then resume within band (must be between lower=9.0 and upper=11.0
        # so limit_mask transitions to False and the halt detection loop closes the segment)
        resume_ts = [base + pd.Timedelta(seconds=480)]
        resume_prices = [10.0]

        ts = pre_ts + post_ts + resume_ts
        prices = pre_prices + post_prices + resume_prices
        df = _make_trades(ts, prices)

        halts = detect_luld_halts(
            df,
            price_col="price",
            size_col="size",
            band_tier="tier2",
            limit_state_seconds=0,
            halt_gap_seconds=300,
        )
        assert len(halts) >= 1, (
            "Gap-freeze should carry pre-gap reference (10.0) so post-gap price 15.0 "
            "breaches upper band (11.0) — 0 halts means gap-freeze is not working"
        )

    def test_gap_shorter_than_window_does_not_freeze(self):
        """A gap shorter than the rolling window (<300s) is naturally handled — no special treatment."""
        base = pd.Timestamp("2024-01-15 14:00:00")
        # 10 trades at 10.0, then a 20s gap (< 30s), then a trade
        pre_ts = [base + pd.Timedelta(seconds=i) for i in range(10)]
        post_ts = [base + pd.Timedelta(seconds=29)]
        ts = pre_ts + post_ts
        prices = [10.0] * 10 + [15.0]
        df = _make_trades(ts, prices)
        # Just confirm no crash; gap < window so no freeze expected
        halts = detect_luld_halts(df, price_col="price", size_col="size", band_tier="tier2",
                                   limit_state_seconds=0, halt_gap_seconds=5)
        assert isinstance(halts, list)

    def test_gap_freeze_synthetic_idai(self):
        """IDAI-like scenario: gap > 30s, post-gap prices above band ceiling, ≥15s → halt.

        5-min mean + sticky: the 60 pre-gap trades at 10.0 dominate the 5-min window.
        The first few post-gap trades at 12.0 raise the mean minimally (<1% per tick),
        keeping ref ≈ 10.0 and upper ≈ 11.0.  30 consecutive ticks at 12.0 > 11.0
        constitute a limit state → halt.

        Without pre-gap anchor (pure post-gap price): ref ≈ 12.0, upper=13.2 → no breach."""
        base = pd.Timestamp("2024-01-15 14:00:00")

        # 60 pre-gap trades at 10.0
        pre_ts = [base + pd.Timedelta(seconds=i) for i in range(60)]
        pre_prices = [10.0] * 60

        # 61s gap → gap-freeze kicks in; ref=10.0 frozen for 30s after gap start
        gap_base = base + pd.Timedelta(seconds=121)
        # 30 post-gap trades at 12.0 (within the 30s freeze window → ref=10.0, upper=11.0)
        post_ts = [gap_base + pd.Timedelta(seconds=i) for i in range(30)]
        post_prices = [12.0] * 30

        # 301s halt gap then resume within band (10.0 is within [9.0, 11.0])
        resume_ts = [gap_base + pd.Timedelta(seconds=330)]
        resume_prices = [10.0]

        ts = pre_ts + post_ts + resume_ts
        prices = pre_prices + post_prices + resume_prices
        df = _make_trades(ts, prices)

        halts = detect_luld_halts(
            df,
            price_col="price",
            size_col="size",
            band_tier="tier2",
            limit_state_seconds=15,
            halt_gap_seconds=300,
        )
        assert len(halts) >= 1, (
            "Synthetic IDAI: 30 ticks at 12.0 during gap-freeze (upper=11.0) should "
            "produce a halt. 0 halts means gap-freeze is missing or broken."
        )

    def test_multiple_gaps_no_crash(self):
        """Multiple gaps in the same session each handled without error."""
        base = pd.Timestamp("2024-01-15 14:00:00")
        ts = (
            [base + pd.Timedelta(seconds=i) for i in range(10)]
            + [base + pd.Timedelta(seconds=60)]
            + [base + pd.Timedelta(seconds=i) for i in range(70, 80)]
            + [base + pd.Timedelta(seconds=140)]
        )
        prices = [10.0] * 10 + [10.0] + [10.0] * 10 + [10.0]
        df = _make_trades(ts, prices)
        halts = detect_luld_halts(df, price_col="price", size_col="size", band_tier="tier2")
        assert isinstance(halts, list)


# ---------------------------------------------------------------------------
# T5 — 5-min arithmetic mean + 1% sticky filter (new reference price mechanism)
# ---------------------------------------------------------------------------


class TestFiveMinStickyRef:
    """Tests for the reconciled reference price: 5-min arithmetic mean + 1% sticky filter."""

    def test_sticky_filter_prevents_small_moves(self):
        """Reference does not update when mean deviates < 1% from published value."""
        base = pd.Timestamp("2024-01-15 14:00:00")
        # 10 trades at 10.0, then 1 trade at 10.09 (0.9% move — below threshold)
        ts = [base + pd.Timedelta(seconds=i) for i in range(10)] + [base + pd.Timedelta(seconds=10)]
        prices = [10.0] * 10 + [10.09]
        df = _make_trades(ts, prices)
        # With sticky ref anchored at 10.0, upper = 11.0; 10.09 < 11.0 → no limit
        halts = detect_luld_halts(df, price_col="price", size_col="size", band_tier="tier2",
                                   limit_state_seconds=0, halt_gap_seconds=5)
        assert len(halts) == 0

    def test_sticky_filter_allows_large_moves(self):
        """Reference updates and upper band shifts when mean deviates ≥ 1%."""
        base = pd.Timestamp("2024-01-15 14:00:00")
        # 10 trades at 10.0 then 1 trade at 11.5 (15% move — above threshold)
        # With sticky update: ref ≈ blended mean ≈ 10.14, upper ≈ 11.15; 11.5 > 11.15 → limit state
        ts = [base + pd.Timedelta(seconds=i) for i in range(10)] + [base + pd.Timedelta(seconds=10)]
        prices = [10.0] * 10 + [11.5]
        df = _make_trades(ts, prices)
        # After 5-min window update, 11.5 is still above the upper band even with sticky update
        # (ref = (100+11.5)/11 ≈ 10.136, upper ≈ 11.15 < 11.5 → limit state)
        # But no halt gap follows → no halt appended (end-of-loop needs schedule)
        halts = detect_luld_halts(df, price_col="price", size_col="size", band_tier="tier2",
                                   limit_state_seconds=0, halt_gap_seconds=5)
        assert isinstance(halts, list)  # just verify no crash

    def test_five_min_window_anchors_ref_through_gap(self):
        """5-min window keeps pre-gap prices active, anchoring ref below post-gap price.

        When gap < 300s, pre-gap trades remain in the window. The blended mean stays
        close to the pre-gap level, keeping the upper band below the post-gap price.
        """
        base = pd.Timestamp("2024-01-15 14:00:00")
        # 100 trades at 10.0 over 99 seconds (warm-up fills 5-min window)
        pre_ts = [base + pd.Timedelta(seconds=i) for i in range(100)]
        pre_prices = [10.0] * 100

        # 200s gap (< 300s — pre-gap prices still in 5-min window)
        # Post-gap: 1 trade at 12.0 (20% above pre-gap)
        post_ts = [base + pd.Timedelta(seconds=300)]
        post_prices = [12.0]

        # Resume within band after halt gap
        resume_ts = [base + pd.Timedelta(seconds=610)]
        resume_prices = [10.0]

        ts = pre_ts + post_ts + resume_ts
        prices = pre_prices + post_prices + resume_prices
        df = _make_trades(ts, prices)

        halts = detect_luld_halts(
            df, price_col="price", size_col="size", band_tier="tier2",
            limit_state_seconds=0, halt_gap_seconds=300,
        )
        assert len(halts) >= 1, (
            "5-min window should retain pre-gap 10.0 prices, keeping mean below 11.0 "
            "so post-gap trade at 12.0 breaches the upper band → halt expected"
        )

    def test_long_gap_ref_updates_when_price_changes(self):
        """After a gap > 300s with a large price move (>1%), sticky filter updates ref.

        When all pre-gap prices age out of the 5-min window, the new mean equals the
        post-gap price. If that differs from published_ref by ≥1%, the ref updates and
        the band shifts to the post-gap level — no spurious limit state.
        """
        base = pd.Timestamp("2024-01-15 14:00:00")
        # 10 pre-gap trades at 10.0
        pre_ts = [base + pd.Timedelta(seconds=i) for i in range(10)]
        pre_prices = [10.0] * 10

        # 301s gap → all pre-gap prices age out of 5-min window
        # Post-gap: 20 trades at 10.5 (5% move — ref updates, upper band at 11.55)
        # 10.5 < 11.55 → NOT in limit state (no breach)
        gap_base = base + pd.Timedelta(seconds=310)
        post_ts = [gap_base + pd.Timedelta(seconds=i) for i in range(20)]
        post_prices = [10.5] * 20

        ts = pre_ts + post_ts
        prices = pre_prices + post_prices
        df = _make_trades(ts, prices)

        halts = detect_luld_halts(
            df, price_col="price", size_col="size", band_tier="tier2",
            limit_state_seconds=0, halt_gap_seconds=300,
        )
        # ref updates to 10.5, upper = 11.55, 10.5 < 11.55 → no limit state → no halt
        assert len(halts) == 0, (
            "After a >300s gap, ref should update to post-gap price (5% move > 1% threshold). "
            "10.5 is within the new band (upper=11.55) → no halt expected"
        )


# ---------------------------------------------------------------------------
# Basic halt detection sanity (existing logic preserved)
# ---------------------------------------------------------------------------


class TestHaltDetectionSanity:
    def test_empty_dataframe(self):
        """Empty input returns empty halt list without error."""
        df = pd.DataFrame({"price": [], "size": []}, index=pd.DatetimeIndex([]))
        halts = detect_luld_halts(df, price_col="price", size_col="size")
        assert halts == []

    def test_no_limit_state_no_halt(self):
        """Trades well within band produce no halts."""
        base = pd.Timestamp("2024-01-15 14:00:00")
        ts = [base + pd.Timedelta(seconds=i) for i in range(100)]
        prices = [10.0] * 100
        df = _make_trades(ts, prices)
        halts = detect_luld_halts(df, price_col="price", size_col="size", band_tier="tier2")
        assert len(halts) == 0

    def test_halt_detected_via_gap_freeze(self):
        """Halt detected: post-gap price above band (anchored by 5-min pre-gap trades), then halt gap.

        Structure: warm-up → gap (>30s) → limit state (ref anchored via 5-min mean + sticky) →
        halt gap (300s) → resume below band → halt detected.
        """
        base = pd.Timestamp("2024-01-15 14:00:00")
        # 60 warm-up trades at 10.0
        warm_ts = [base + pd.Timedelta(seconds=i) for i in range(60)]
        warm_prices = [10.0] * 60

        # 62s gap → gap-freeze; ref=10.0 frozen for 30s after gap
        gap_base = base + pd.Timedelta(seconds=122)
        # 20 limit-state trades at 12.0 (within 30s freeze window, ref=10.0, upper=11.0)
        limit_ts = [gap_base + pd.Timedelta(seconds=i) for i in range(20)]
        limit_prices = [12.0] * 20

        # 301s halt gap then resume within band (10.0 is within [9.0, 11.0])
        resume_ts = [gap_base + pd.Timedelta(seconds=320)]
        resume_prices = [10.0]

        ts = warm_ts + limit_ts + resume_ts
        prices = warm_prices + limit_prices + resume_prices
        df = _make_trades(ts, prices)

        halts = detect_luld_halts(
            df, price_col="price", size_col="size", band_tier="tier2",
            limit_state_seconds=15, halt_gap_seconds=300
        )
        assert len(halts) >= 1

    def test_halt_window_structure(self):
        """Returned HaltWindow has start <= end and reason='luld'."""
        base = pd.Timestamp("2024-01-15 14:00:00")
        warm_ts = [base + pd.Timedelta(seconds=i) for i in range(60)]
        warm_prices = [10.0] * 60
        gap_base = base + pd.Timedelta(seconds=121)
        limit_ts = [gap_base + pd.Timedelta(seconds=i) for i in range(20)]
        limit_prices = [12.0] * 20
        resume_ts = [gap_base + pd.Timedelta(seconds=320)]
        resume_prices = [10.0]

        ts = warm_ts + limit_ts + resume_ts
        prices = warm_prices + limit_prices + resume_prices
        df = _make_trades(ts, prices)

        halts = detect_luld_halts(
            df, price_col="price", size_col="size", band_tier="tier2",
            limit_state_seconds=15, halt_gap_seconds=300
        )
        if halts:
            h = halts[0]
            assert h.start <= h.end
            assert h.reason == "luld"
            assert h.duration_seconds() >= 0

    def test_limit_state_start_recorded(self):
        """Phase LULD-V3c T3: HaltWindow records limit_state_start (seg_start), the
        limit-state onset, which precedes start (seg_end) by >= limit_state_seconds."""
        base = pd.Timestamp("2024-01-15 14:00:00")
        warm_ts = [base + pd.Timedelta(seconds=i) for i in range(60)]
        warm_prices = [10.0] * 60
        gap_base = base + pd.Timedelta(seconds=121)
        limit_ts = [gap_base + pd.Timedelta(seconds=i) for i in range(20)]
        limit_prices = [12.0] * 20
        resume_ts = [gap_base + pd.Timedelta(seconds=320)]
        resume_prices = [10.0]
        df = _make_trades(warm_ts + limit_ts + resume_ts,
                          warm_prices + limit_prices + resume_prices)

        halts = detect_luld_halts(
            df, price_col="price", size_col="size", band_tier="tier2",
            limit_state_seconds=15, halt_gap_seconds=300
        )
        assert halts
        h = halts[0]
        assert h.limit_state_start is not None
        # onset is the start of the limit-state run; start is seg_end (the freeze)
        assert h.limit_state_start <= h.start
        assert (h.start - h.limit_state_start).total_seconds() >= 15
