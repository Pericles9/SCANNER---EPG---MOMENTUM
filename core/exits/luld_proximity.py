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

Exit trigger (Phase E symmetric spread-multiple):
    buffer = n_spread_multiple * spread   (spread = ask - bid)
    lower_trigger = lower_band + buffer
    upper_trigger = upper_band - buffer
    EXIT_HALT fires when price < lower_trigger OR price > upper_trigger.
If spread is unavailable or invalid (ask <= bid), buffer = 0 (band-touch fallback).

Known approximations (per Phase T A.1):
1. The LULD spec excludes certain "ineligible transactions" (e.g. odd lots,
   certain VWAP prints) from the reference price calculation. We use all
   trades -- a known approximation.
2. The reference price is technically published by the SIP every second; we
   recompute it on every tick. This is more responsive than the official
   feed, not less.
3. Bands round to the nearest penny per spec; we do not round (working in
   float). Rounding error is at most $0.005 -- negligible relative to a 5%
   proximity threshold.

Module is self-contained -- only stdlib + numpy. No imports from backtest/,
core/hawkes/, or core/events/.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo


# -- Constants ----------------------------------------------------------------

_ET = ZoneInfo("America/New_York")
_NS_PER_SECOND = 1_000_000_000

# Time-of-day band schedule in seconds-from-midnight ET
_RTH_START_SEC = 9 * 3600 + 30 * 60        # 09:30:00 ET
_DOUBLED_AM_END_SEC = 9 * 3600 + 45 * 60   # 09:45:00 ET
_DOUBLED_PM_START_SEC = 15 * 3600 + 35 * 60  # 15:35:00 ET
_RTH_END_SEC = 16 * 3600                   # 16:00:00 ET

_BAND_PCT_NORMAL = 0.10
_BAND_PCT_DOUBLED = 0.20

_DEQUE_CAP = 100_000


# -- Enums and dataclasses ----------------------------------------------------


class ProximityState(Enum):
    """Current state of the LULD proximity exit."""
    INACTIVE = "INACTIVE"      # outside RTH, or in warmup
    SAFE = "SAFE"              # within both triggers
    EXIT_HALT = "EXIT_HALT"    # outside a trigger


@dataclass
class ProximityResult:
    """Single-tick proximity computation result."""
    state: ProximityState
    reference_price: Optional[float]
    lower_band: Optional[float]
    upper_band: Optional[float]
    lower_proximity_bps: Optional[float]   # (price - lower_band) / price * 10000
    upper_proximity_bps: Optional[float]   # (upper_band - price) / price * 10000
    fire_side: Optional[str]               # "lower", "upper", or None
    band_pct: float
    spread_used: Optional[float]           # actual spread (0.0 if fallback)


# -- Helpers ------------------------------------------------------------------


def _et_seconds_of_day(timestamp_ns: int) -> int:
    """Return seconds-from-midnight in Eastern Time for a UTC nanosecond timestamp."""
    dt_utc = datetime.fromtimestamp(timestamp_ns / _NS_PER_SECOND, tz=timezone.utc)
    dt_et = dt_utc.astimezone(_ET)
    return dt_et.hour * 3600 + dt_et.minute * 60 + dt_et.second


def _band_pct_for_time(et_sec_of_day: int) -> float:
    """Return Tier 2 band_pct for a given ET second-of-day."""
    if et_sec_of_day < _RTH_START_SEC or et_sec_of_day >= _RTH_END_SEC:
        return 0.0
    if et_sec_of_day < _DOUBLED_AM_END_SEC:
        return _BAND_PCT_DOUBLED
    if et_sec_of_day >= _DOUBLED_PM_START_SEC:
        return _BAND_PCT_DOUBLED
    return _BAND_PCT_NORMAL


def _is_regular_hours(et_sec_of_day: int) -> bool:
    return _RTH_START_SEC <= et_sec_of_day < _RTH_END_SEC


# -- Main class ---------------------------------------------------------------


class LuldProximityExit:
    """Streaming LULD halt-proximity exit detector (symmetric spread-multiple trigger).

    Maintains a 5-minute rolling deque of (timestamp_ns, price). On each
    update():
      1. Compute ET seconds-of-day. If outside 09:30-16:00 ET, return INACTIVE.
      2. Append the new tick, expire entries older than ref_window_sec.
      3. If less than warmup_sec of in-window data accumulated, return INACTIVE.
      4. Compute reference_price = mean(in-window prices), bands.
      5. Compute buffer = n_spread_multiple * spread (0 if spread invalid).
      6. EXIT_HALT if price < lower_band + buffer OR price > upper_band - buffer.

    When spread is None or invalid (ask <= bid), buffer = 0 (fires only at the
    band itself). A warning is issued on the first such fallback per instance.
    """

    def __init__(
        self,
        ref_window_sec: float = 300.0,
        n_spread_multiple: float = 2.0,
        warmup_sec: float = 60.0,
    ):
        if ref_window_sec <= 0:
            raise ValueError(f"ref_window_sec must be positive, got {ref_window_sec}")
        if n_spread_multiple < 0:
            raise ValueError(
                f"n_spread_multiple must be >= 0, got {n_spread_multiple}"
            )
        if warmup_sec < 0:
            raise ValueError(f"warmup_sec must be >= 0, got {warmup_sec}")

        self.ref_window_sec = ref_window_sec
        self.n_spread_multiple = n_spread_multiple
        self.warmup_sec = warmup_sec

        self._ref_window_ns = int(ref_window_sec * _NS_PER_SECOND)
        self._warmup_ns = int(warmup_sec * _NS_PER_SECOND)
        self._buffer: deque = deque(maxlen=_DEQUE_CAP)
        self._warned_fallback = False

    def reset(self) -> None:
        """Clear all state. Next update() returns INACTIVE until warmup completes."""
        self._buffer.clear()
        self._warned_fallback = False

    def update(
        self,
        timestamp_ns: int,
        price: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> ProximityResult:
        """Process a single trade tick and return current proximity state.

        Parameters
        ----------
        timestamp_ns : int
            Trade timestamp in unix nanoseconds (UTC).
        price : float
            Trade price.
        bid : float or None
            Prevailing bid at this tick. None triggers band-touch fallback.
        ask : float or None
            Prevailing ask at this tick. None triggers band-touch fallback.
        """
        et_sec = _et_seconds_of_day(timestamp_ns)

        if not _is_regular_hours(et_sec):
            return ProximityResult(
                state=ProximityState.INACTIVE,
                reference_price=None, lower_band=None, upper_band=None,
                lower_proximity_bps=None, upper_proximity_bps=None,
                fire_side=None, band_pct=0.0, spread_used=None,
            )

        self._buffer.append((timestamp_ns, price))
        cutoff = timestamp_ns - self._ref_window_ns
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()

        oldest_ts = self._buffer[0][0]
        if (timestamp_ns - oldest_ts) < self._warmup_ns:
            band_pct = _band_pct_for_time(et_sec)
            return ProximityResult(
                state=ProximityState.INACTIVE,
                reference_price=None, lower_band=None, upper_band=None,
                lower_proximity_bps=None, upper_proximity_bps=None,
                fire_side=None, band_pct=band_pct, spread_used=None,
            )

        n = len(self._buffer)
        s = 0.0
        for _, p in self._buffer:
            s += p
        ref_price = s / n

        band_pct = _band_pct_for_time(et_sec)
        lower_band = ref_price * (1.0 - band_pct)
        upper_band = ref_price * (1.0 + band_pct)

        if price <= 0:
            return ProximityResult(
                state=ProximityState.INACTIVE,
                reference_price=ref_price, lower_band=lower_band,
                upper_band=upper_band,
                lower_proximity_bps=None, upper_proximity_bps=None,
                fire_side=None, band_pct=band_pct, spread_used=None,
            )

        # Compute spread; fallback to 0 if invalid
        spread = 0.0
        valid_spread = (
            bid is not None and ask is not None
            and ask > bid and bid > 0.0
        )
        if valid_spread:
            spread = ask - bid
        elif not self._warned_fallback:
            self._warned_fallback = True
            # first fallback per instance -- caller may log if needed

        buffer = self.n_spread_multiple * spread
        lower_trigger = lower_band + buffer
        upper_trigger = upper_band - buffer

        lower_prox_bps = (price - lower_band) / price * 10000.0
        upper_prox_bps = (upper_band - price) / price * 10000.0

        fire_side: Optional[str] = None
        if price < lower_trigger:
            fire_side = "lower"
        elif price > upper_trigger:
            fire_side = "upper"

        state = ProximityState.EXIT_HALT if fire_side is not None else ProximityState.SAFE

        return ProximityResult(
            state=state,
            reference_price=ref_price,
            lower_band=lower_band,
            upper_band=upper_band,
            lower_proximity_bps=lower_prox_bps,
            upper_proximity_bps=upper_prox_bps,
            fire_side=fire_side,
            band_pct=band_pct,
            spread_used=spread,
        )
