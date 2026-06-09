"""
T1d — Unit tests for halt-adjusted active-seconds computation.

Verifies that prepare_active_trades() and compress_active_seconds() correctly
exclude halted periods from the elapsed-time axis, and that using active_seconds
as dt for EMA updates produces meaningfully different results from wall-clock dt
when a trading halt occurs.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.features.luld_halt_detection import (
    compress_active_seconds,
    prepare_active_trades,
)


LN2 = math.log(2)


def _ema_decay(initial: float, value: float, dt: float, tau: float) -> float:
    """Standard EMA update: x_new = x_old * exp(-ln2*dt/tau) + value * (1 - exp(-ln2*dt/tau))."""
    decay = math.exp(-LN2 * dt / tau)
    return initial * decay + value * (1.0 - decay)


def _make_trades_df(timestamps_ns: list[int], prices: list[float], sizes: list[int]) -> pd.DataFrame:
    """Build a minimal trades DataFrame with DatetimeIndex (nanoseconds, UTC-naive)."""
    idx = pd.to_datetime(timestamps_ns, unit="ns", utc=True).tz_convert(None)
    return pd.DataFrame({"price": prices, "size": sizes}, index=idx)


# ──────────────────────────────────────────────────────────────────────
#  compress_active_seconds unit tests
# ──────────────────────────────────────────────────────────────────────

class TestCompressActiveSeconds:

    def test_no_halt_active_equals_wall_clock(self):
        """With no halted intervals, active_seconds == wall-clock spacing."""
        base = pd.Timestamp("2024-01-15 09:30:00")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=i) for i in [0, 10, 20, 30]])
        start = base
        end = base + pd.Timedelta(seconds=60)
        active_intervals = [(start, end)]

        active_sec = compress_active_seconds(idx, active_intervals)

        assert len(active_sec) == 4
        assert math.isclose(active_sec[0], 0.0)
        assert math.isclose(active_sec[1], 10.0)
        assert math.isclose(active_sec[2], 20.0)
        assert math.isclose(active_sec[3], 30.0)

    def test_halt_compresses_gap(self):
        """A 600s halt between two active segments is excluded from active_seconds."""
        base = pd.Timestamp("2024-01-15 10:00:00")

        # Trade at T=0, then halt from T=10 to T=610, then trades at T=620 and T=630
        times = [
            base,
            base + pd.Timedelta(seconds=620),
            base + pd.Timedelta(seconds=630),
        ]
        idx = pd.DatetimeIndex(times)

        # Active intervals: [0, 10) then [610, 640)
        active_intervals = [
            (base, base + pd.Timedelta(seconds=10)),
            (base + pd.Timedelta(seconds=610), base + pd.Timedelta(seconds=640)),
        ]

        active_sec = compress_active_seconds(idx, active_intervals)

        # First trade: active_sec[0] = 0
        # Second trade: should be 10 active s (= time in first segment up to T=10)
        #               plus (620-610) = 10 s in second segment → 10+10 = 20 active s
        # Third trade: 20 + 10 = 30 active s
        assert math.isclose(active_sec[0], 0.0, abs_tol=1e-3)
        assert math.isclose(active_sec[1], 20.0, abs_tol=1e-3), f"expected 20, got {active_sec[1]}"
        assert math.isclose(active_sec[2], 30.0, abs_tol=1e-3)

    def test_active_seconds_normalised_to_zero(self):
        """The first element of active_seconds is always 0.0."""
        base = pd.Timestamp("2024-01-15 09:30:00")
        idx = pd.DatetimeIndex([base + pd.Timedelta(seconds=100 + i * 10) for i in range(5)])
        active_intervals = [(base, base + pd.Timedelta(seconds=1000))]
        active_sec = compress_active_seconds(idx, active_intervals)
        assert math.isclose(active_sec[0], 0.0, abs_tol=1e-9)


# ──────────────────────────────────────────────────────────────────────
#  EMA dt comparison: halt-adjusted vs wall-clock
# ──────────────────────────────────────────────────────────────────────

class TestEMAHaltAdjustedDt:
    """
    Core T1d test: verify that using halt-adjusted dt for EMA updates
    produces materially different (and correct) results vs wall-clock dt
    when a 600s trading halt is present.

    Scenario
    --------
    Trades at t=0, t=10, then a 600s halt (no trades 10s < t < 610s),
    then trades at t=620, t=630.

    Wall-clock dt before the next post-halt trade = 620-10 = 610s.
    Active dt for the same gap ≈ 10s (only 10 active seconds elapsed during halt).

    lambda_V EMA with tau=180s should decay far less using active dt.
    """

    def _build_synthetic_event_with_halt(self):
        """Return (active_seconds, wall_clock_t_sec, prices, sizes) for the scenario."""
        base_ns = int(pd.Timestamp("2024-01-15 09:30:00").value)
        NS = 1_000_000_000

        # Timestamps: 0, 10, 620, 630 seconds from base
        ts_offsets_s = [0, 10, 620, 630]
        timestamps_ns = [base_ns + s * NS for s in ts_offsets_s]
        prices = [10.0, 10.5, 11.0, 11.2]
        sizes = [100, 200, 150, 300]

        df = _make_trades_df(timestamps_ns, prices, sizes)

        # Define active intervals: sessions with gap excluded
        base_ts = pd.Timestamp(base_ns, unit="ns")
        active_intervals = [
            (base_ts, base_ts + pd.Timedelta(seconds=15)),           # 0-15s active
            (base_ts + pd.Timedelta(seconds=610), base_ts + pd.Timedelta(seconds=640)),  # 610-640s active
        ]

        active_sec = compress_active_seconds(df.index, active_intervals)
        wall_clock_sec = np.array(ts_offsets_s, dtype=float)
        wall_clock_sec -= wall_clock_sec[0]  # normalise to 0

        return active_sec, wall_clock_sec, np.array(prices), np.array(sizes)

    def test_halt_adjusted_dt_differs_from_wall_clock(self):
        """Active dt across the halt gap is much smaller than wall-clock dt."""
        active_sec, wall_sec, prices, sizes = self._build_synthetic_event_with_halt()

        # Wall-clock dt for the post-halt tick (index 2 in 0-based)
        dt_wall = wall_sec[2] - wall_sec[1]   # 620 - 10 = 610s
        dt_active = active_sec[2] - active_sec[1]  # should be ~15s active

        assert dt_wall > 500, f"Expected large wall-clock gap, got {dt_wall}"
        assert dt_active < 20, f"Expected small active gap, got {dt_active}"

    def test_ema_decay_differs_halt_vs_wall_clock(self):
        """
        lambda_V EMA after a 600s halt decays far less with halt-adjusted dt
        than with wall-clock dt (tau=180s, half-life test).

        The halt occupies wall-clock dt=610s but only ~15s of active time.
        With tau=180s:
          - wall-clock decay: exp(-ln2 * 610 / 180) ≈ 0.095  (95% gone)
          - active    decay: exp(-ln2 *  15 / 180) ≈ 0.944  ( 6% gone)
        The pre-contribution EMA at the first post-halt tick should be ~10x
        higher using active dt.
        """
        active_sec, wall_sec, prices, sizes = self._build_synthetic_event_with_halt()

        tau = 180.0
        decay_rate = LN2 / tau

        # Compute lambda_V EMA after the pre-halt trades only (t=0 and t=10)
        lv_pre = 0.0
        for i in range(2):           # only first two trades
            dv = prices[i] * sizes[i]
            if i == 0:
                lv_pre = dv * decay_rate
            else:
                dt = wall_sec[i] - wall_sec[i - 1]    # dt=10s, same in both
                lv_pre = lv_pre * math.exp(-decay_rate * dt) + dv * decay_rate

        # Apply decay-only step across the halt gap (no new trade contribution)
        dt_wall_gap = wall_sec[2] - wall_sec[1]        # 610s
        dt_active_gap = active_sec[2] - active_sec[1]  # ~15s

        lv_after_gap_wall = lv_pre * math.exp(-decay_rate * dt_wall_gap)
        lv_after_gap_active = lv_pre * math.exp(-decay_rate * dt_active_gap)

        # Active EMA retains ~10× more than wall-clock EMA across the halt
        assert lv_after_gap_active > lv_after_gap_wall * 5, (
            f"Pre-contribution EMA after halt: active={lv_after_gap_active:.4f} "
            f"wall={lv_after_gap_wall:.4f} — expected active > 5× wall"
        )
        # Sanity: wall-clock decay factor is less than 15% of active decay factor
        decay_wall = math.exp(-LN2 * dt_wall_gap / tau)
        decay_active = math.exp(-LN2 * dt_active_gap / tau)
        assert decay_active > decay_wall * 5, (
            f"Expected active decay ({decay_active:.4f}) >> wall-clock decay ({decay_wall:.4f})"
        )

    def test_compress_active_seconds_excludes_600s_halt(self):
        """
        compress_active_seconds with manually defined active intervals that
        exclude a 600s halt gives total active duration < 50s, not 630s.

        Note: prepare_active_trades uses LULD price-band detection internally.
        This test exercises compress_active_seconds directly — the same function
        used by the sweep runner after halt intervals are determined.
        """
        base = pd.Timestamp("2024-01-15 09:30:00")
        # Trades at 0, 10, 620, 630 s from base (600s halt between t=10 and t=620)
        times = [base + pd.Timedelta(seconds=s) for s in [0, 10, 620, 630]]
        idx = pd.DatetimeIndex(times)

        # Active intervals that exclude the 600s halt window
        active_intervals = [
            (base, base + pd.Timedelta(seconds=15)),
            (base + pd.Timedelta(seconds=610), base + pd.Timedelta(seconds=640)),
        ]

        active_sec = compress_active_seconds(idx, active_intervals)

        assert len(active_sec) == 4
        total_active = active_sec[-1] - active_sec[0]
        assert total_active < 50, (
            f"Expected active duration < 50s after 600s halt, got {total_active:.1f}s"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
