"""Unit tests for core.exits.luld_proximity (LULD-REBUILD quote-based interface).

Synthetic timestamps built from ET wall-clock times so DST handling is exercised
through the same `zoneinfo` path as production code.
"""
from __future__ import annotations

import math
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
        assert result.reference_price == pytest.approx(0.0)
        assert result.upper_band == pytest.approx(0.0)
        assert math.isnan(result.bid_proximity_pct)
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
        """At 11:00 ET with steady prices, band_pct=0.10 and upper_band=ref*1.10."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        # bid=9.95 is far from upper_band=11.0 → SAFE
        result = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        assert result.band_pct == pytest.approx(0.10)
        assert result.reference_price == pytest.approx(10.0, abs=1e-6)
        assert result.upper_band == pytest.approx(11.0, abs=1e-2)
        assert result.state == ProximityState.SAFE

    # -- 5. Doubled band at the open --------------------------------------

    def test_doubled_band_opening(self):
        """At 09:35 ET, band_pct=0.20 and upper_band=ref*1.20."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 9, 30, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        probe_ts = et_to_ns(2024, 6, 17, 9, 35, 0)
        result = exit_obj.update(probe_ts, 10.0, bid=9.9, ask=10.1)
        assert result.band_pct == pytest.approx(0.20)
        assert result.upper_band == pytest.approx(12.0, abs=1e-2)

    # -- 6. Doubled band at the close -------------------------------------

    def test_doubled_band_closing(self):
        """At 15:45 ET, band_pct=0.20 and upper_band=ref*1.20."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 15, 40, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        probe_ts = last_ts + _NS
        result = exit_obj.update(probe_ts, 10.0, bid=9.9, ask=10.1)
        assert result.band_pct == pytest.approx(0.20)
        assert result.upper_band == pytest.approx(12.0, abs=1e-2)

    # -- T2a: Bid proximity fire (upper band) --------------------------

    def test_bid_proximity_fire(self):
        """Bid within proximity_threshold of upper band fires EXIT_HALT upper."""
        # ref=10.0, band_pct=0.10, upper_band=11.0, threshold=0.02
        # fire when bid >= 11.0 * (1 - 0.02) = 10.78
        # bid=10.85 → bid_proximity_pct = (11.0 - 10.85) / 11.0 ≈ 0.0136 < 0.02 → fires
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        result = exit_obj.update(last_ts + _NS, 10.85, bid=10.85, ask=10.90)
        assert result.state == ProximityState.EXIT_HALT
        assert result.fire_side == "upper"
        assert result.bid_proximity_pct < 0.02

    # -- T2b: Upper band fire via bid proximity -------------------------

    def test_upper_band_fire(self):
        """Bid near upper band triggers EXIT_HALT with fire_side='upper'."""
        # ref=10.0, upper_band=11.0
        # bid=10.90 → bid_proximity_pct = (11.0 - 10.90) / 11.0 ≈ 0.0091 < 0.02 → fires
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        result = exit_obj.update(last_ts + _NS, 10.90, bid=10.90, ask=10.95)
        assert result.state == ProximityState.EXIT_HALT
        assert result.fire_side == "upper"
        assert not math.isnan(result.bid_proximity_pct)

    # -- T2c: Safe when bid well below upper band ----------------------

    def test_safe_inside_triggers(self):
        """Bid far below upper band returns SAFE with fire_side=None."""
        # ref=10.0, upper_band=11.0, threshold=0.02 → fire when bid_prox ≤ 0.02
        # bid=9.95 → bid_proximity_pct = (11.0 - 9.95) / 11.0 ≈ 0.0955 > 0.02 → SAFE
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        result = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        assert result.state == ProximityState.SAFE
        assert result.fire_side is None
        assert result.spread_used == pytest.approx(0.10, abs=1e-8)
        assert result.bid_proximity_pct > 0.02

    # -- T2d: Fallback on invalid quote --------------------------------

    def test_fallback_invalid_spread(self):
        """ask <= bid or None triggers trade-price fallback."""
        # ref=10.0, upper_band=11.0, threshold=0.02
        # fallback fires when (upper_band - price) / upper_band ≤ 0.02
        # i.e. price >= 11.0 * 0.98 = 10.78
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        # Invalid spread: ask < bid — price=10.5 < 10.78 → SAFE fallback
        result = exit_obj.update(last_ts + _NS, 10.5, bid=10.1, ask=9.9)
        assert result.state == ProximityState.SAFE
        assert result.spread_used == pytest.approx(0.0, abs=1e-10)
        assert math.isnan(result.bid_proximity_pct)

        # Invalid spread, price=10.85 → (11.0 - 10.85) / 11.0 ≈ 0.0136 < 0.02 → EXIT_HALT
        result2 = exit_obj.update(last_ts + 2 * _NS, 10.85, bid=10.1, ask=9.9)
        assert result2.state == ProximityState.EXIT_HALT
        assert result2.fire_side == "upper"

    # -- T2e: Upper fire at doubled bands ------------------------------

    def test_upper_fire_doubled_bands(self):
        """During open window (band_pct=0.20), upper band fires correctly."""
        # ref=10.0, band_pct=0.20, upper_band=12.0, threshold=0.02
        # fire when bid >= 12.0 * 0.98 = 11.76
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 9, 30, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.90, ask=10.10,
                                    duration_sec=120)
        probe_ts = et_to_ns(2024, 6, 17, 9, 35, 0)

        # Upper fire: bid=11.85 → (12.0 - 11.85) / 12.0 = 0.0125 < 0.02 → fires
        res_upper = exit_obj.update(probe_ts, 11.85, bid=11.85, ask=11.90)
        assert res_upper.state == ProximityState.EXIT_HALT
        assert res_upper.fire_side == "upper"

        # Well below upper band: bid=9.0 → SAFE
        res_safe = exit_obj.update(probe_ts + _NS, 9.0, bid=9.0, ask=9.05)
        assert res_safe.state == ProximityState.SAFE
        assert res_safe.fire_side is None

    # -- 8. Reference price is rolling and sticky ----------------------

    def test_reference_price_rolling(self):
        """Old prices outside the 5-minute window excluded; sticky ref updates on
        sufficient change."""
        exit_obj = LuldProximityExit(ref_window_sec=300.0, warmup_sec=60.0,
                                     proximity_threshold=0.001)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        for i in range(0, 360):
            ts = start + i * _NS
            exit_obj.update(ts, 10.0, bid=9.95, ask=10.05)
        last_ts = start + 360 * _NS
        result = exit_obj.update(last_ts, 10.0, bid=9.95, ask=10.05)
        assert result.reference_price == pytest.approx(10.0, abs=1e-6)

        # Jump 6 minutes ahead: all 10.0 prices expire; feed 20.0 prices
        far_ts = last_ts + 6 * 60 * _NS
        for i in range(0, 65):
            exit_obj.update(far_ts + i * _NS, 20.0, bid=19.95, ask=20.05)
        result_after = exit_obj.update(far_ts + 65 * _NS, 20.0, bid=19.95, ask=20.05)
        # 20.0 is 100% away from 10.0 → sticky filter triggers → ref updates to 20.0
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
        """bid=None or ask=None triggers trade-price fallback."""
        # ref=10.0, upper_band=11.0, threshold=0.02 → fallback fires at price >= 10.78
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, bid=9.95, ask=10.05,
                                    duration_sec=120)
        # price=9.05 well below 10.78 → SAFE in fallback mode
        result = exit_obj.update(last_ts + _NS, 9.05, bid=None, ask=None)
        assert result.state == ProximityState.SAFE
        assert result.spread_used == pytest.approx(0.0, abs=1e-10)

    # -- 11. bid_proximity_pct field populated -------------------------

    def test_bid_proximity_pct_field(self):
        """bid_proximity_pct is (upper_band - bid) / upper_band when valid quote present."""
        exit_obj = LuldProximityExit(proximity_threshold=0.05)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        # ref=10.0, upper_band=11.0, bid=9.95
        # bid_proximity_pct = (11.0 - 9.95) / 11.0 ≈ 0.09545
        result = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        expected_prox = (result.upper_band - 9.95) / result.upper_band
        assert result.bid_proximity_pct == pytest.approx(expected_prox, abs=1e-6)
        assert not math.isnan(result.bid_proximity_pct)

    # -- 12. Sticky reference price does not update on small moves -----

    def test_sticky_ref_does_not_update_on_small_move(self):
        """Reference price stays fixed when rolling mean moves <1%."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        # Warmup at price 10.0 → published_ref = 10.0
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        r0 = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        ref_initial = r0.reference_price
        assert ref_initial == pytest.approx(10.0, abs=1e-6)

        # Feed a few ticks at 10.05 (0.5% move — below 1% threshold)
        ts = last_ts + 2 * _NS
        for i in range(10):
            r = exit_obj.update(ts + i * _NS, 10.05, bid=9.95, ask=10.05)
        # Reference price should not have updated (change < 1%)
        assert r.reference_price == pytest.approx(ref_initial, abs=1e-6)

    # -- 13. Sticky ref freezes during EXIT_HALT -----------------------

    def test_sticky_ref_frozen_during_limit_state(self):
        """Published reference price is frozen while state is EXIT_HALT."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        # First, fire EXIT_HALT with bid near upper_band
        r_halt = exit_obj.update(last_ts + _NS, 10.90, bid=10.90, ask=10.95)
        assert r_halt.state == ProximityState.EXIT_HALT
        frozen_ref = r_halt.reference_price

        # Now feed a price far from original (30% higher) while still in HALT
        # Published ref should NOT update
        r_during = exit_obj.update(last_ts + 2 * _NS, 13.0, bid=10.90, ask=10.95)
        assert r_during.reference_price == pytest.approx(frozen_ref, abs=1e-6)

    # -- 14-18. Pin+duration clock (V3 luld_exit_duration_sec) ----------

    def test_duration_zero_fires_immediately(self):
        """luld_exit_duration_sec=0 (default) preserves immediate-fire behaviour."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02, luld_exit_duration_sec=0.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        # First tick with bid in zone → immediate EXIT_HALT
        result = exit_obj.update(last_ts + _NS, 10.90, bid=10.90, ask=10.95)
        assert result.state == ProximityState.EXIT_HALT
        assert result.fire_side == "upper"
        # pin_duration_sec = 0 because this is the very first tick in zone
        assert result.pin_duration_sec == pytest.approx(0.0, abs=1e-6)

    def test_duration_gate_holds_safe_until_elapsed(self):
        """With duration=5s, state stays SAFE while pin_duration_sec < 5."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02, luld_exit_duration_sec=5.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)

        # Feed 4 ticks at 1s intervals with bid in zone — all should be SAFE
        for i in range(1, 5):
            ts = last_ts + i * _NS
            result = exit_obj.update(ts, 10.90, bid=10.90, ask=10.95)
            assert result.state == ProximityState.SAFE, (
                f"tick +{i}s should be SAFE (pin_duration={result.pin_duration_sec:.1f} < 5)"
            )
            assert result.pin_duration_sec == pytest.approx(float(i - 1), abs=0.01)

    def test_duration_gate_fires_when_elapsed(self):
        """With duration=5s, EXIT_HALT fires when pin_duration_sec ≥ 5.
        Pin starts on the first in-zone tick; at +1s intervals, 5s elapsed on tick 6."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02, luld_exit_duration_sec=5.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)

        # Ticks 1–5 (offsets +1s to +5s from last_ts) accumulate 0–4s of pin time → SAFE
        for i in range(1, 6):
            ts = last_ts + i * _NS
            result = exit_obj.update(ts, 10.90, bid=10.90, ask=10.95)
        assert result.state == ProximityState.SAFE, "5 ticks at 1Hz → 4s elapsed < 5s"

        # Tick 6 (+6s): pin_duration = 5s exactly → fire
        result6 = exit_obj.update(last_ts + 6 * _NS, 10.90, bid=10.90, ask=10.95)
        assert result6.state == ProximityState.EXIT_HALT
        assert result6.fire_side == "upper"
        assert result6.pin_duration_sec >= 5.0

    def test_duration_clock_resets_on_exit_from_zone(self):
        """If bid leaves proximity zone, pin clock resets; duration counts from zero again."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02, luld_exit_duration_sec=5.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)

        # 3 ticks in zone
        for i in range(1, 4):
            exit_obj.update(last_ts + i * _NS, 10.90, bid=10.90, ask=10.95)

        # Bid drops out of zone
        r_safe = exit_obj.update(last_ts + 4 * _NS, 10.0, bid=9.95, ask=10.05)
        assert r_safe.state == ProximityState.SAFE
        assert r_safe.pin_duration_sec == pytest.approx(0.0, abs=1e-6)

        # Re-enter zone — clock should restart, not accumulate from before
        r_reentry = exit_obj.update(last_ts + 5 * _NS, 10.90, bid=10.90, ask=10.95)
        assert r_reentry.state == ProximityState.SAFE  # only 0s elapsed since re-entry
        assert r_reentry.pin_duration_sec == pytest.approx(0.0, abs=0.01)

    def test_duration_pin_duration_sec_field(self):
        """pin_duration_sec is 0.0 when not pinning and grows while in zone."""
        exit_obj = LuldProximityExit(proximity_threshold=0.02, luld_exit_duration_sec=10.0)
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)

        # Not in zone → 0.0
        r_out = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        assert r_out.pin_duration_sec == pytest.approx(0.0, abs=1e-6)

        # Enter zone — pin starts
        r0 = exit_obj.update(last_ts + 2 * _NS, 10.90, bid=10.90, ask=10.95)
        assert r0.pin_duration_sec == pytest.approx(0.0, abs=0.01)

        # 3s later
        r3 = exit_obj.update(last_ts + 5 * _NS, 10.90, bid=10.90, ask=10.95)
        assert r3.pin_duration_sec == pytest.approx(3.0, abs=0.05)

    # -- Gap-freeze tests (T2c V3b) ----------------------------------------

    def test_gap_freeze_reference_persists(self):
        """After a gap longer than the rolling window, a previously-warm module must
        NOT return INACTIVE.  The published reference price persists per SIP §4."""
        exit_obj = LuldProximityExit(
            ref_window_sec=300.0, warmup_sec=60.0, proximity_threshold=0.02
        )
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        # Warm up at price 10.0
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        r_warm = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        assert r_warm.state == ProximityState.SAFE
        assert r_warm.reference_price == pytest.approx(10.0, abs=0.05)

        # Jump forward 400s (> 300s ref_window) — simulates a 6-minute trading gap
        gap_ts = last_ts + 400 * _NS
        r_post_gap = exit_obj.update(gap_ts, 10.0, bid=9.95, ask=10.05)

        # Must NOT be INACTIVE: module was previously warm
        assert r_post_gap.state != ProximityState.INACTIVE, (
            "Previously-warm module returned INACTIVE after gap — gap-freeze fix missing"
        )
        # Reference price must be the pre-gap value (not 0.0)
        assert r_post_gap.reference_price > 0.0, (
            "reference_price=0.0 after gap — gap-freeze fix did not persist ref"
        )
        assert r_post_gap.reference_price == pytest.approx(10.0, abs=0.15)

    def test_cold_start_still_inactive(self):
        """Genuine cold start (no reference price ever established) must still return
        INACTIVE — the gap-freeze fix must not accidentally remove cold-start warmup."""
        exit_obj = LuldProximityExit(warmup_sec=60.0)
        # First tick ever — no prior reference
        ts = et_to_ns(2024, 6, 17, 11, 0, 0)
        result = exit_obj.update(ts, 10.0, bid=9.95, ask=10.05)
        assert result.state == ProximityState.INACTIVE
        assert result.reference_price == pytest.approx(0.0)
        assert result.upper_band == pytest.approx(0.0)

    def test_gap_freeze_synthetic_idai(self):
        """Synthetic IDAI scenario: warm module goes through a 400s gap, then a
        band-breaching bid arrives just 24s into the recovery window.
        EXIT_HALT must fire because the module is now ACTIVE (not INACTIVE).

        The post-gap trade at price=10.90 triggers the sticky filter (9% from pre-gap
        ref=10.0), so published_ref updates to 10.90 and upper_band becomes ~11.99.
        A bid of 11.80 is within 2% of 11.99 → EXIT_HALT fires.

        The key assertion is NOT that the exact pre-gap reference persists (it doesn't,
        because the sticky filter fires on a large price jump) — the key assertion is that
        the module is ACTIVE and CAN fire EXIT_HALT, which would be impossible if it were
        INACTIVE (the pre-fix state)."""
        exit_obj = LuldProximityExit(
            ref_window_sec=300.0, warmup_sec=60.0, proximity_threshold=0.02,
            luld_exit_duration_sec=0.0  # V2 immediate-fire mode
        )
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        # Warm up: establish reference at 10.0
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)
        r_warm = exit_obj.update(last_ts + _NS, 10.0, bid=9.95, ask=10.05)
        assert r_warm.state == ProximityState.SAFE

        # Simulate 400s trading gap (> 300s buffer window)
        gap_ts = last_ts + 400 * _NS

        # 24s into recovery: first post-gap trade at price=10.90 (sticky filter fires →
        # ref updates to 10.90, upper ≈ 11.99). Bid=11.80 is within 2% of 11.99.
        recovery_ts = gap_ts + 24 * _NS
        r_halt = exit_obj.update(recovery_ts, 10.90, bid=11.80, ask=11.85)

        # Module must be ACTIVE — the gap-freeze fix makes this possible.
        # Without the fix, the module would be INACTIVE here (warmup not completed).
        assert r_halt.state != ProximityState.INACTIVE, (
            "Module should be ACTIVE 24s after gap — gap-freeze fix means previously-warm "
            "modules don't re-enter INACTIVE state just because the buffer was aged out."
        )
        assert r_halt.state == ProximityState.EXIT_HALT, (
            f"bid=11.80 should be within 2% of upper band, got state={r_halt.state}. "
            f"ref={r_halt.reference_price}, upper={r_halt.upper_band}, "
            f"bid_prox={r_halt.bid_proximity_pct:.4f}"
        )
        assert r_halt.fire_side == "upper"
        assert r_halt.reference_price > 0.0  # gap-freeze: ref never returned to 0.0

    def test_gap_freeze_first_post_gap_tick_only(self):
        """After a gap, only one trade is in the buffer (the fresh post-gap trade).
        The rolling mean of 1 trade equals that trade's price, but the sticky filter
        should suppress the update if it's within 1% of the persisted reference."""
        exit_obj = LuldProximityExit(
            ref_window_sec=300.0, warmup_sec=60.0, proximity_threshold=0.02
        )
        start = et_to_ns(2024, 6, 17, 11, 0, 0)
        last_ts = self._feed_warmup(exit_obj, start, price=10.0, duration_sec=120)

        # Gap of 400s; post-gap price is 10.005 (0.05% from ref=10.0 → below 1% sticky)
        gap_ts = last_ts + 400 * _NS
        r = exit_obj.update(gap_ts, 10.005, bid=9.95, ask=10.05)
        assert r.state != ProximityState.INACTIVE
        # Sticky filter: 0.05% change → reference should NOT update
        assert r.reference_price == pytest.approx(10.0, abs=0.02)
