"""Unit tests for core.exits.luld_proximity.

Synthetic timestamps built from ET wall-clock times so DST handling is exercised
through the same `zoneinfo` path as production code.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.exits.luld_proximity import (
    LuldProximityExit,
    ProximityState,
)


_ET = ZoneInfo("America/New_York")
_NS = 1_000_000_000


def et_to_ns(year: int, month: int, day: int,
             hour: int, minute: int, second: int = 0,
             micro: int = 0) -> int:
    """Convert ET wall-clock to unix nanoseconds."""
    dt = datetime(year, month, day, hour, minute, second, micro, tzinfo=_ET)
    # int(timestamp() * 1e9) loses precision past microseconds but is fine
    return int(dt.timestamp() * _NS) + (micro * 1000 % _NS) - (micro * 1000)


class TestLuldProximityExit:
    def _feed_warmup(self, exit_obj: LuldProximityExit,
                     start_ns: int, price: float,
                     duration_sec: int = 65,
                     interval_sec: int = 1) -> int:
        """Feed `duration_sec` of steady-price ticks at 1Hz starting at start_ns.

        Returns the final timestamp_ns fed.
        """
        last_ts = start_ns
        for i in range(0, duration_sec, interval_sec):
            last_ts = start_ns + i * _NS
            exit_obj.update(last_ts, price)
        return last_ts

    # ── 1. Pre-market is INACTIVE ─────────────────────────────────────

    def test_inactive_pre_market(self):
        """Tick at 08:00 ET returns INACTIVE regardless of price."""
        exit_obj = LuldProximityExit()
        ts = et_to_ns(2024, 6, 17, 8, 0, 0)
        result = exit_obj.update(ts, 10.0)
        assert result.state == ProximityState.INACTIVE
        assert result.reference_price is None
        assert result.proximity_pct is None
        assert result.band_pct == 0.0

    # ── 2. Post-market is INACTIVE ────────────────────────────────────

    def test_inactive_post_market(self):
        """Tick at 17:00 ET returns INACTIVE."""
        exit_obj = LuldProximityExit()
        ts = et_to_ns(2024, 6, 17, 17, 0, 0)
        result = exit_obj.update(ts, 10.0)
        assert result.state == ProximityState.INACTIVE

    # ── 3. Warmup window returns INACTIVE ─────────────────────────────

    def test_warmup_period(self):
        """First 60s of in-RTH data returns INACTIVE."""
        exit_obj = LuldProximityExit(warmup_sec=60.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        # First 60 seconds of ticks: should all be INACTIVE
        for i in range(0, 59):
            ts = start + i * _NS
            result = exit_obj.update(ts, 10.0)
            assert result.state == ProximityState.INACTIVE, (
                f"tick at +{i}s should be INACTIVE during warmup"
            )

    # ── 4. Normal-hours band 0.10 ─────────────────────────────────────

    def test_normal_band_regular_hours(self):
        """At 11:00 ET with steady prices, band_pct=0.10 and lower_band = ref * 0.90."""
        exit_obj = LuldProximityExit()
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        # One more tick at the same price after warmup
        result = exit_obj.update(last_ts + _NS, 10.0)
        assert result.band_pct == pytest.approx(0.10)
        assert result.reference_price == pytest.approx(10.0, abs=1e-6)
        assert result.lower_band == pytest.approx(9.0, abs=1e-6)
        assert result.upper_band == pytest.approx(11.0, abs=1e-6)
        assert result.state == ProximityState.SAFE

    # ── 5. Doubled band at the open ───────────────────────────────────

    def test_doubled_band_opening(self):
        """At 09:35 ET, band_pct=0.20."""
        exit_obj = LuldProximityExit()
        # Warm up starting before 09:30 won't help (INACTIVE);
        # start the warmup run inside RTH at 09:30:00 and probe at 09:35
        start = et_to_ns(2024, 6, 17, 9, 30, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        # 09:32 is inside the doubled-AM window (09:30-09:45)
        probe_ts = et_to_ns(2024, 6, 17, 9, 35, 0)
        result = exit_obj.update(probe_ts, 10.0)
        assert result.band_pct == pytest.approx(0.20)
        assert result.lower_band == pytest.approx(8.0, abs=1e-6)

    # ── 6. Doubled band at the close ──────────────────────────────────

    def test_doubled_band_closing(self):
        """At 15:45 ET, band_pct=0.20."""
        exit_obj = LuldProximityExit()
        # 15:35 is the start of the doubled-PM window; warm up starting at
        # 15:40 (still doubled) so the 5-minute ref window is fully in scope
        # by 15:45.
        start = et_to_ns(2024, 6, 17, 15, 40, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        probe_ts = last_ts + _NS
        result = exit_obj.update(probe_ts, 10.0)
        assert result.band_pct == pytest.approx(0.20)
        assert result.lower_band == pytest.approx(8.0, abs=1e-6)

    # ── 7. Proximity exit fires on a price drop ──────────────────────

    def test_proximity_exit_fires(self):
        """Synthetic price drop crossing proximity threshold returns EXIT_HALT."""
        exit_obj = LuldProximityExit(proximity_pct_threshold=5.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        # Warm up at $10
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        # The new tick is also added to the buffer before the mean is computed,
        # so reference includes it. With 120 ticks at $10 + 1 tick at $9.30:
        # ref ≈ (120 * 10 + 9.30) / 121 ≈ 9.99421
        # lower_band ≈ 9.99421 * 0.9 ≈ 8.99479
        # proximity_pct = (9.30 - 8.99479) / 9.30 * 100 ≈ 3.28% — well below 5%.
        result = exit_obj.update(last_ts + _NS, 9.30)
        assert result.state == ProximityState.EXIT_HALT
        # Compute expected ref including the new tick
        expected_ref = (120 * 10.0 + 9.30) / 121
        expected_lower = expected_ref * 0.9
        expected_prox = (9.30 - expected_lower) / 9.30 * 100
        assert result.reference_price == pytest.approx(expected_ref, abs=1e-6)
        assert result.lower_band == pytest.approx(expected_lower, abs=1e-6)
        assert result.proximity_pct == pytest.approx(expected_prox, abs=1e-4)

        # And a price safely above triggers SAFE
        result_safe = exit_obj.update(last_ts + 2 * _NS, 10.0)
        assert result_safe.state == ProximityState.SAFE

    # ── 8. Reference price is rolling ────────────────────────────────

    def test_reference_price_rolling(self):
        """Old prices outside the 5-minute window must be excluded from the mean."""
        exit_obj = LuldProximityExit(ref_window_sec=300.0, warmup_sec=60.0)
        # Feed 6 minutes at $10, then a final tick at $20 — the first minute's
        # ticks should have aged out, but the bulk of $10 ticks remain in window.
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        # 6 minutes of ticks at 1Hz at $10
        for i in range(0, 360):
            ts = start + i * _NS
            exit_obj.update(ts, 10.0)
        # Now the buffer should hold only the last 5 minutes (≈300 ticks)
        # Reference price ≈ 10.0 still
        last_ts = start + 360 * _NS
        result = exit_obj.update(last_ts, 10.0)
        assert result.reference_price == pytest.approx(10.0, abs=1e-6)

        # Push a series of high prices that would shift the mean if all
        # data remained; only the in-window data should affect the mean.
        # Skip 5 minutes forward, all $10 ticks should be expired.
        far_ts = last_ts + 6 * 60 * _NS  # 6 minutes later
        # Inject a single $20 tick at 17:06 ET (still RTH)
        # but first need warmup of 60s, so feed 65s of $20 first
        for i in range(0, 65):
            exit_obj.update(far_ts + i * _NS, 20.0)
        result_after = exit_obj.update(far_ts + 65 * _NS, 20.0)
        # After 6 minutes of nothing then 65s of $20, the buffer holds only
        # the $20 ticks
        assert result_after.reference_price == pytest.approx(20.0, abs=1e-6)

    # ── 9. Reset clears state ─────────────────────────────────────────

    def test_reset_clears_state(self):
        """After reset(), returns INACTIVE until warmup completes again."""
        exit_obj = LuldProximityExit(warmup_sec=60.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        # After warmup we should be SAFE
        active = exit_obj.update(last_ts + _NS, 10.0)
        assert active.state == ProximityState.SAFE

        exit_obj.reset()
        # Immediately after reset, the next tick has no buffer history → INACTIVE
        result_after_reset = exit_obj.update(last_ts + 2 * _NS, 10.0)
        assert result_after_reset.state == ProximityState.INACTIVE
