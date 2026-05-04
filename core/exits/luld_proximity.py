"""LULD halt-proximity exit (standalone, regular-hours only).

Reverse-engineered Tier 2 LULD bands. Active 09:30-16:00 ET only; pre-market and
post-market halts are exchange-discretion events with no standardized formula and
are out of scope (the module returns INACTIVE outside RTH).

Tier 2 band_pct schedule (ET):
    09:30:00 - 09:45:00   0.20  (doubled)
    09:45:00 - 15:35:00   0.10  (normal)
    15:35:00 - 16:00:00   0.20  (doubled)

Reference price = mean of trade prices in the trailing 5-minute window.
lower_band = ref * (1 - band_pct), upper_band = ref * (1 + band_pct).
proximity_pct = (price - lower_band) / price * 100.
EXIT_HALT fires when proximity_pct < proximity_pct_threshold.

Known approximations (per Phase T A.1):
1. The LULD spec excludes certain "ineligible transactions" (e.g. odd lots,
   certain VWAP prints) from the reference price calculation. We use all
   trades — a known approximation.
2. The reference price is technically published by the SIP every second; we
   recompute it on every tick. This is more responsive than the official
   feed, not less.
3. Bands round to the nearest penny per spec; we do not round (working in
   float). Rounding error is at most $0.005 — negligible relative to a 5%
   proximity threshold.

Module is self-contained — only stdlib + numpy. No imports from backtest/,
core/hawkes/, or core/events/.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo


# ── Constants ──────────────────────────────────────────────────────────

_ET = ZoneInfo("America/New_York")
_NS_PER_SECOND = 1_000_000_000

# Time-of-day band schedule in seconds-from-midnight ET
_RTH_START_SEC = 9 * 3600 + 30 * 60        # 09:30:00 ET
_DOUBLED_AM_END_SEC = 9 * 3600 + 45 * 60   # 09:45:00 ET
_DOUBLED_PM_START_SEC = 15 * 3600 + 35 * 60  # 15:35:00 ET
_RTH_END_SEC = 16 * 3600                   # 16:00:00 ET

_BAND_PCT_NORMAL = 0.10
_BAND_PCT_DOUBLED = 0.20

# Cap on the rolling deque size. Practical events run ~250k ticks across the
# whole 16-hour session; a 5-minute window is much smaller. The cap prevents
# pathological growth if the caller feeds millions of ticks without expiring.
_DEQUE_CAP = 100_000


# ── Enums and dataclasses ──────────────────────────────────────────────


class ProximityState(Enum):
    """Current state of the LULD proximity exit."""
    INACTIVE = "INACTIVE"      # outside RTH, or in warmup
    SAFE = "SAFE"              # proximity_pct >= threshold
    EXIT_HALT = "EXIT_HALT"    # proximity_pct < threshold


@dataclass
class ProximityResult:
    """Single-tick proximity computation result."""
    state: ProximityState
    reference_price: Optional[float]
    lower_band: Optional[float]
    upper_band: Optional[float]
    proximity_pct: Optional[float]
    band_pct: float


# ── Helpers ────────────────────────────────────────────────────────────


def _et_seconds_of_day(timestamp_ns: int) -> int:
    """Return seconds-from-midnight in Eastern Time for a UTC nanosecond timestamp.

    Uses zoneinfo so DST boundaries are handled correctly.
    """
    dt_utc = datetime.fromtimestamp(timestamp_ns / _NS_PER_SECOND, tz=timezone.utc)
    dt_et = dt_utc.astimezone(_ET)
    return dt_et.hour * 3600 + dt_et.minute * 60 + dt_et.second


def _band_pct_for_time(et_sec_of_day: int) -> float:
    """Return Tier 2 band_pct for a given ET second-of-day.

    Returns 0.0 outside RTH (caller has already checked RTH membership;
    this is a defensive fallback).
    """
    if et_sec_of_day < _RTH_START_SEC or et_sec_of_day >= _RTH_END_SEC:
        return 0.0
    if et_sec_of_day < _DOUBLED_AM_END_SEC:
        return _BAND_PCT_DOUBLED
    if et_sec_of_day >= _DOUBLED_PM_START_SEC:
        return _BAND_PCT_DOUBLED
    return _BAND_PCT_NORMAL


def _is_regular_hours(et_sec_of_day: int) -> bool:
    return _RTH_START_SEC <= et_sec_of_day < _RTH_END_SEC


# ── Main class ─────────────────────────────────────────────────────────


class LuldProximityExit:
    """Streaming LULD halt-proximity exit detector.

    Maintains a 5-minute rolling deque of (timestamp_ns, price). On each
    update():
      1. Compute ET seconds-of-day. If outside 09:30-16:00 ET, return INACTIVE.
      2. Append the new tick, expire entries older than ref_window_sec.
      3. If less than warmup_sec of in-window data accumulated, return INACTIVE.
      4. Compute reference_price = mean(in-window prices), bands, proximity_pct.
      5. Return EXIT_HALT if proximity_pct < threshold, else SAFE.

    The deque is also capped at _DEQUE_CAP entries to prevent runaway memory
    if the caller feeds an unrealistic number of ticks per second; the cap is
    far above any realistic 5-minute tick count.
    """

    def __init__(
        self,
        ref_window_sec: float = 300.0,
        proximity_pct_threshold: float = 5.0,
        warmup_sec: float = 60.0,
    ):
        if ref_window_sec <= 0:
            raise ValueError(f"ref_window_sec must be positive, got {ref_window_sec}")
        if proximity_pct_threshold <= 0:
            raise ValueError(
                f"proximity_pct_threshold must be positive, got {proximity_pct_threshold}"
            )
        if warmup_sec < 0:
            raise ValueError(f"warmup_sec must be >= 0, got {warmup_sec}")

        self.ref_window_sec = ref_window_sec
        self.proximity_pct_threshold = proximity_pct_threshold
        self.warmup_sec = warmup_sec

        self._ref_window_ns = int(ref_window_sec * _NS_PER_SECOND)
        self._warmup_ns = int(warmup_sec * _NS_PER_SECOND)
        self._buffer: deque = deque(maxlen=_DEQUE_CAP)

    def reset(self) -> None:
        """Clear all state. Next update() returns INACTIVE until warmup completes."""
        self._buffer.clear()

    def update(self, timestamp_ns: int, price: float) -> ProximityResult:
        """Process a single trade tick and return current proximity state.

        Parameters
        ----------
        timestamp_ns : int
            Trade timestamp in unix nanoseconds (UTC).
        price : float
            Trade price.
        """
        et_sec = _et_seconds_of_day(timestamp_ns)

        # Outside regular hours: clear nothing, just return INACTIVE.
        # The buffer state is preserved so resuming RTH the next day works,
        # but expired entries are purged on the first in-RTH tick anyway.
        if not _is_regular_hours(et_sec):
            return ProximityResult(
                state=ProximityState.INACTIVE,
                reference_price=None, lower_band=None, upper_band=None,
                proximity_pct=None, band_pct=0.0,
            )

        # Append + expire stale entries
        self._buffer.append((timestamp_ns, price))
        cutoff = timestamp_ns - self._ref_window_ns
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()

        # Warmup check: need warmup_sec of data in the buffer
        oldest_ts = self._buffer[0][0]
        if (timestamp_ns - oldest_ts) < self._warmup_ns:
            band_pct = _band_pct_for_time(et_sec)
            return ProximityResult(
                state=ProximityState.INACTIVE,
                reference_price=None, lower_band=None, upper_band=None,
                proximity_pct=None, band_pct=band_pct,
            )

        # Compute reference price and bands
        # (mean of all in-window prices, including the just-added tick)
        n = len(self._buffer)
        # Sum prices via simple loop; deque doesn't support numpy indexing.
        # n is bounded by the 5-minute tick rate; this is fine for streaming.
        s = 0.0
        for _, p in self._buffer:
            s += p
        ref_price = s / n

        band_pct = _band_pct_for_time(et_sec)
        lower_band = ref_price * (1.0 - band_pct)
        upper_band = ref_price * (1.0 + band_pct)

        if price <= 0:
            # Defensive: don't divide by zero or negative price
            return ProximityResult(
                state=ProximityState.INACTIVE,
                reference_price=ref_price, lower_band=lower_band,
                upper_band=upper_band, proximity_pct=None, band_pct=band_pct,
            )

        proximity_pct = (price - lower_band) / price * 100.0

        if proximity_pct < self.proximity_pct_threshold:
            state = ProximityState.EXIT_HALT
        else:
            state = ProximityState.SAFE

        return ProximityResult(
            state=state,
            reference_price=ref_price,
            lower_band=lower_band,
            upper_band=upper_band,
            proximity_pct=proximity_pct,
            band_pct=band_pct,
        )
