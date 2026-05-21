"""Unit tests for core.exits.luld_proximity (Phase E symmetric spread-multiple interface).

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
    return int(dt.timestamp() * _NS) + (micro * 1000 % _NS) - (micro * 1000)


class TestLuldProximityExit:
    def _feed_warmup(self, exit_obj: LuldProximityExit,
                     start_ns: int, price: float,
                     bid: float = 9.95, ask: float = 10.05,
                     duration_sec: int = 65,
                     interval_sec: int = 1) -> int:
        """Feed duration_sec of steady-price ticks at 1Hz starting at start_ns."""
        last_ts = start_ns
        for i in range(0, duration_sec, interval_sec):
            last_ts = start_ns + i * _NS
            exit_obj.update(last_ts, price, bid, ask)
        return last_ts

    # -- 1. Pre-market is INACTIVE -----------------------------------------

    def test_inactive_pre_market(self):
        """Tick at 08:00 ET returns INACTIVE regardless of price."""
        exit_obj = LuldProximityExit()
        ts = et_to_ns(2024, 6, 17, 8, 0, 0)
        result = exit_obj.update(ts, 10.0, bid=9.95, ask=10.05)
        assert result.state == ProximityState.INACTIVE
        assert result.reference_price is None
        assert result.lower_proximity_bps is None
        assert result.upper_proximity_bps is None
        assert result.fire_side is None
        assert result.band_pct == 0.0

    # -- 2. Post-market is INACTIVE ----------------------------------------

    def test_inactive_post_market(self):
        """Tick at 17:00 ET returns INACTIVE."""
        exit_obj = LuldProximityExit()
        ts = et_to_ns(2024, 6, 17, 17, 0, 0)
        result = exit_obj.update(ts, 10.0, bid=9.95, ask=10.05)
        assert result.state == ProximityState.INACTIVE

    # -- 3. Warmup window returns INACTIVE ---------------------------------

    def test_warmup_period(self):
        """First 60s of in-RTH data returns INACTIVE."""
        exit_obj = LuldProximityExit(warmup_sec=60.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        for i in range(0, 59):
            ts = start + i * _NS
            result = exit_obj.update(ts, 10.0, bid=9.95, ask=10.05)
            assert result.state == ProximityState.INACTIVE, (
                f"tick at +{i}s should be INACTIVE during warmup"
            )

    # -- 4. Normal-hours band 0.10 ----------------------------------------

    def test_normal_band_regular_hours(self):
        """At 11:00 ET with steady prices, band_pct=0.10 and bands are ref +/- 10%."""
        exit_obj = LuldProximityExit(n_spread_multiple=0.0)  # buffer=0, band-touch only
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        result = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        assert result.band_pct == pytest.approx(0.10)
        assert result.reference_price == pytest.approx(10.0, abs=1e-6)
        assert result.lower_band == pytest.approx(9.0, abs=1e-6)
        assert result.upper_band == pytest.approx(11.0, abs=1e-6)
        assert result.state == ProximityState.SAFE

    # -- 5. Doubled band at the open --------------------------------------

    def test_doubled_band_opening(self):
        """At 09:35 ET, band_pct=0.20."""
        exit_obj = LuldProximityExit(n_spread_multiple=0.0)
        start = et_to_ns(2024, 6, 17, 9, 30, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        probe_ts = et_to_ns(2024, 6, 17, 9, 35, 0)
        result = exit_obj.update(probe_ts, 10.0, bid=9.9, ask=10.1)
        assert result.band_pct == pytest.approx(0.20)
        assert result.lower_band == pytest.approx(8.0, abs=1e-6)

    # -- 6. Doubled band at the close -------------------------------------

    def test_doubled_band_closing(self):
        """At 15:45 ET, band_pct=0.20."""
        exit_obj = LuldProximityExit(n_spread_multiple=0.0)
        start = et_to_ns(2024, 6, 17, 15, 40, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        probe_ts = last_ts + _NS
        result = exit_obj.update(probe_ts, 10.0, bid=9.9, ask=10.1)
        assert result.band_pct == pytest.approx(0.20)
        assert result.lower_band == pytest.approx(8.0, abs=1e-6)

    # -- T2a: Lower band fire -------------------------------------------

    def test_lower_band_fire(self):
        """Price within N spreads of lower band triggers EXIT_HALT with fire_side='lower'."""
        # band_pct=0.10, ref=10.0, lower_band=9.0
        # spread=0.10, N=2, buffer=0.20
        # lower_trigger = 9.0 + 0.20 = 9.20
        # price=9.15 < 9.20 -> EXIT_HALT lower
        exit_obj = LuldProximityExit(n_spread_multiple=2.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        result = exit_obj.update(last_ts + _NS, 9.15, bid=9.95, ask=10.05)
        assert result.state == ProximityState.EXIT_HALT
        assert result.fire_side == "lower"
        assert result.lower_proximity_bps is not None

    # -- T2b: Upper band fire -------------------------------------------

    def test_upper_band_fire(self):
        """Price within N spreads of upper band triggers EXIT_HALT with fire_side='upper'."""
        # band_pct=0.10, ref=10.0, upper_band=11.0
        # spread=0.10, N=2, buffer=0.20
        # upper_trigger = 11.0 - 0.20 = 10.80
        # price=10.85 > 10.80 -> EXIT_HALT upper
        exit_obj = LuldProximityExit(n_spread_multiple=2.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        result = exit_obj.update(last_ts + _NS, 10.85, bid=9.95, ask=10.05)
        assert result.state == ProximityState.EXIT_HALT
        assert result.fire_side == "upper"
        assert result.upper_proximity_bps is not None

    # -- T2c: Safe when price inside both triggers ----------------------

    def test_safe_inside_triggers(self):
        """Price well inside both triggers returns SAFE with fire_side=None."""
        # band_pct=0.10, ref=10.0, lower_band=9.0, upper_band=11.0
        # spread=0.10, N=2, buffer=0.20
        # lower_trigger=9.20, upper_trigger=10.80
        # price=10.0 -> SAFE
        exit_obj = LuldProximityExit(n_spread_multiple=2.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        result = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        assert result.state == ProximityState.SAFE
        assert result.fire_side is None
        assert result.spread_used == pytest.approx(0.10, abs=1e-8)

    # -- T2d: Fallback on invalid spread --------------------------------

    def test_fallback_invalid_spread(self):
        """ask <= bid or None triggers band-touch fallback (buffer=0)."""
        # band_pct=0.10, ref=10.0, lower_band=9.0, upper_band=11.0
        # N=2, spread=0 (fallback), buffer=0
        # lower_trigger=9.0, upper_trigger=11.0
        # price=9.05 -> 9.05 > 9.0 -> SAFE (not at band itself)
        exit_obj = LuldProximityExit(n_spread_multiple=2.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        # Invalid spread: ask < bid
        result = exit_obj.update(last_ts + _NS, 9.05, bid=10.1, ask=9.9)
        assert result.state == ProximityState.SAFE
        assert result.spread_used == pytest.approx(0.0, abs=1e-10)

        # At exactly the band with invalid spread: EXIT_HALT
        result2 = exit_obj.update(last_ts + 2 * _NS, 8.95, bid=10.1, ask=9.9)
        assert result2.state == ProximityState.EXIT_HALT
        assert result2.fire_side == "lower"

    # -- T2e: Symmetric fire at doubled bands ---------------------------

    def test_symmetric_doubled_bands(self):
        """During open window (band_pct=0.20), both lower and upper fire symmetrically."""
        # band_pct=0.20, ref=10.0, lower_band=8.0, upper_band=12.0
        # spread=0.20, N=3, buffer=0.60
        # lower_trigger=8.60, upper_trigger=11.40
        exit_obj = LuldProximityExit(n_spread_multiple=3.0)
        start = et_to_ns(2024, 6, 17, 9, 30, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.90, ask=10.10,
                                    duration_sec=120)
        probe_ts = et_to_ns(2024, 6, 17, 9, 35, 0)

        # Lower fire: price below 8.60
        res_lower = exit_obj.update(probe_ts, 8.50, bid=9.90, ask=10.10)
        assert res_lower.state == ProximityState.EXIT_HALT
        assert res_lower.fire_side == "lower"

        # Upper fire: price above 11.40
        res_upper = exit_obj.update(probe_ts + _NS, 11.50, bid=9.90, ask=10.10)
        assert res_upper.state == ProximityState.EXIT_HALT
        assert res_upper.fire_side == "upper"

    # -- 8. Reference price is rolling ------------------------------------

    def test_reference_price_rolling(self):
        """Old prices outside the 5-minute window must be excluded from the mean."""
        exit_obj = LuldProximityExit(ref_window_sec=300.0, warmup_sec=60.0,
                                     n_spread_multiple=0.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        for i in range(0, 360):
            ts = start + i * _NS
            exit_obj.update(ts, 10.0, bid=9.95, ask=10.05)
        last_ts = start + 360 * _NS
        result = exit_obj.update(last_ts, 10.0, bid=9.95, ask=10.05)
        assert result.reference_price == pytest.approx(10.0, abs=1e-6)

        far_ts = last_ts + 6 * 60 * _NS
        for i in range(0, 65):
            exit_obj.update(far_ts + i * _NS, 20.0, bid=19.95, ask=20.05)
        result_after = exit_obj.update(far_ts + 65 * _NS, 20.0, bid=19.95, ask=20.05)
        assert result_after.reference_price == pytest.approx(20.0, abs=1e-6)

    # -- 9. Reset clears state -------------------------------------------

    def test_reset_clears_state(self):
        """After reset(), returns INACTIVE until warmup completes again."""
        exit_obj = LuldProximityExit(warmup_sec=60.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        active = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        assert active.state == ProximityState.SAFE

        exit_obj.reset()
        result_after_reset = exit_obj.update(last_ts + 2 * _NS, 10.0, bid=9.95, ask=10.05)
        assert result_after_reset.state == ProximityState.INACTIVE

    # -- 10. None bid/ask triggers fallback ----------------------------

    def test_none_bid_ask_fallback(self):
        """bid=None or ask=None triggers band-touch fallback (buffer=0)."""
        exit_obj = LuldProximityExit(n_spread_multiple=3.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        # price=9.05 with no quote -> spread=0 -> buffer=0 -> trigger at bands
        # lower_band=9.0, price=9.05 > 9.0 -> SAFE
        result = exit_obj.update(last_ts + _NS, 9.05, bid=None, ask=None)
        assert result.state == ProximityState.SAFE
        assert result.spread_used == pytest.approx(0.0, abs=1e-10)

    # -- 11. proximity_bps fields populated ----------------------------

    def test_proximity_bps_fields(self):
        """lower_proximity_bps and upper_proximity_bps are populated when active."""
        exit_obj = LuldProximityExit(n_spread_multiple=0.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        result = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        # ref=10.0, lower_band=9.0, upper_band=11.0, price=10.0
        # lower_prox_bps = (10.0 - 9.0) / 10.0 * 10000 = 1000.0
        # upper_prox_bps = (11.0 - 10.0) / 10.0 * 10000 = 1000.0
        assert result.lower_proximity_bps == pytest.approx(1000.0, abs=1e-4)
        assert result.upper_proximity_bps == pytest.approx(1000.0, abs=1e-4)
