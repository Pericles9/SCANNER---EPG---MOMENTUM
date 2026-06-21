"""LULD halt-proximity exit — quote-based, sticky 1% reference price.

Tier 2 LULD bands. Active 09:30-16:00 ET only; pre-market and post-market
are out of scope (module returns INACTIVE outside RTH).

Tier 2 band_pct schedule (ET):
    09:30:00 - 09:45:00   0.20  (doubled)
    09:45:00 - 15:35:00   0.10  (normal)
    15:35:00 - 16:00:00   0.20  (doubled)

Reference price — sticky SIP approximation:
    Published reference price updates only when the new 5-minute rolling
    arithmetic mean is ≥1% away from the current published reference price.
    On cold start (first tick after warmup), the published reference is set
    unconditionally. During an active Limit State proxy (EXIT_HALT), the
    published reference is frozen; updates resume when the state clears.

Exit signal — quote-based:
    Upper band (NBB approaching upper band):
        bid_proximity_pct = (upper_band - nbbo_bid) / upper_band
        When luld_exit_duration_sec == 0 (default / V2 behaviour):
            EXIT_HALT fires immediately when bid_proximity_pct ≤ proximity_threshold_upper.
        When luld_exit_duration_sec > 0 (V3 pin+duration clock):
            EXIT_HALT fires only after the proximity condition is sustained for
            at least luld_exit_duration_sec seconds without interruption. If the
            bid leaves the proximity zone, the clock resets to zero.
    Lower band (NBA approaching lower band, when lower_enabled=True):
        ask_proximity_pct = (nbbo_ask - lower_band) / lower_band
        EXIT_HALT fires immediately when ask_proximity_pct ≤ proximity_threshold_lower.
        (No pin+duration clock on lower band.)
    If no valid quote is available (bid ≤ 0 or ask ≤ bid), falls back to
    trade-price comparison on the affected side.
    If both upper and lower fire on the same tick: fire_side = "upper".

lower_enabled=False (default): lower band disabled. All existing callers unaffected.

Known approximations:
1. All trades are included in the reference price window, including odd lots and
   ineligible transactions excluded by the SIP spec.
2. The SIP publishes reference prices on a second-by-second cadence; we
   recompute on each trade tick with a 1% sticky filter.
3. Bands round to the nearest penny per spec; we apply round(..., 2).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from zoneinfo import ZoneInfo


# -- Constants ----------------------------------------------------------------

_ET = ZoneInfo("America/New_York")
_NS_PER_SECOND = 1_000_000_000

_RTH_START_SEC = 9 * 3600 + 30 * 60        # 09:30:00 ET
_DOUBLED_AM_END_SEC = 9 * 3600 + 45 * 60   # 09:45:00 ET
_DOUBLED_PM_START_SEC = 15 * 3600 + 35 * 60  # 15:35:00 ET
_RTH_END_SEC = 16 * 3600                    # 16:00:00 ET

_BAND_PCT_NORMAL = 0.10
_BAND_PCT_DOUBLED = 0.20

# Minimum relative change in rolling mean required to update published ref price
_STICKY_MIN_CHANGE = 0.01

_DEQUE_CAP = 100_000


# -- Enums and dataclasses ----------------------------------------------------


class ProximityState(Enum):
    """Current state of the LULD proximity exit."""
    INACTIVE = "INACTIVE"      # outside RTH, warmup, or invalid price
    SAFE = "SAFE"              # bid not yet near upper band
    EXIT_HALT = "EXIT_HALT"    # bid within proximity_threshold of upper band


@dataclass
class ProximityResult:
    """Single-tick proximity computation result."""
    state: ProximityState
    fire_side: Optional[str]       # "upper", "lower", or None
    reference_price: float         # current published sticky ref price (0.0 before warmup)
    upper_band: float              # current upper band value (0.0 before warmup)
    lower_band: float              # current lower band value (0.0 before warmup or disabled)
    bid_proximity_pct: float       # (upper_band - nbbo_bid) / upper_band; NaN if fallback
    ask_proximity_pct: float       # (nbbo_ask - lower_band) / lower_band; NaN if disabled/fallback
    spread_used: float             # ask - bid at this tick; 0.0 if no valid quote
    band_pct: float                # current Tier 2 band percentage
    pin_duration_sec: float = 0.0  # seconds bid has been within proximity_threshold_upper (V3)


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
    """Streaming LULD halt-proximity exit detector (quote-based, sticky reference price).

    Maintains a 5-minute rolling deque of trade prices for the reference price
    calculation. The published reference price is sticky: it only updates when
    the rolling mean moves ≥1% from the current published value. During an active
    EXIT_HALT (Limit State proxy), the reference price is frozen.

    Upper band: uses prevailing NBBO bid. Lower band (when lower_enabled=True): uses
    prevailing NBBO ask. Falls back to trade-price comparison when no valid quote.

    Backward-compatible: existing callers that pass only proximity_threshold continue
    to work unchanged — proximity_threshold_upper and proximity_threshold_lower both
    default to proximity_threshold; lower_enabled=False disables the lower band.
    """

    def __init__(
        self,
        ref_window_sec: float = 300.0,
        proximity_threshold: float = 0.02,
        warmup_sec: float = 60.0,
        luld_exit_duration_sec: float = 0.0,
        proximity_threshold_upper: Optional[float] = None,
        proximity_threshold_lower: Optional[float] = None,
        lower_enabled: bool = False,
    ):
        if ref_window_sec <= 0:
            raise ValueError(f"ref_window_sec must be positive, got {ref_window_sec}")
        if proximity_threshold < 0:
            raise ValueError(
                f"proximity_threshold must be >= 0, got {proximity_threshold}"
            )
        if warmup_sec < 0:
            raise ValueError(f"warmup_sec must be >= 0, got {warmup_sec}")
        if luld_exit_duration_sec < 0:
            raise ValueError(
                f"luld_exit_duration_sec must be >= 0, got {luld_exit_duration_sec}"
            )

        self.ref_window_sec = ref_window_sec
        self.proximity_threshold = proximity_threshold
        self.warmup_sec = warmup_sec
        self.luld_exit_duration_sec = luld_exit_duration_sec
        self.proximity_threshold_upper = (
            proximity_threshold_upper if proximity_threshold_upper is not None
            else proximity_threshold
        )
        self.proximity_threshold_lower = (
            proximity_threshold_lower if proximity_threshold_lower is not None
            else proximity_threshold
        )
        self.lower_enabled = lower_enabled

        self._ref_window_ns = int(ref_window_sec * _NS_PER_SECOND)
        self._warmup_ns = int(warmup_sec * _NS_PER_SECOND)
        self._buffer: deque = deque(maxlen=_DEQUE_CAP)
        self._published_ref: float = 0.0   # 0.0 = not yet set (cold start)
        self._in_limit_state: bool = False
        self._warned_fallback: bool = False
        self._pin_start_ns: Optional[int] = None  # wall-clock when pin started

    def reset(self) -> None:
        """Clear all state. Next update() returns INACTIVE until warmup completes."""
        self._buffer.clear()
        self._published_ref = 0.0
        self._in_limit_state = False
        self._warned_fallback = False
        self._pin_start_ns = None

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
            Trade price (used as fallback when no valid quote is available).
        bid : float or None
            Prevailing NBBO bid at this tick. None triggers trade-price fallback.
        ask : float or None
            Prevailing NBBO ask at this tick. None triggers trade-price fallback.
        """
        et_sec = _et_seconds_of_day(timestamp_ns)

        if not _is_regular_hours(et_sec):
            self._in_limit_state = False
            self._pin_start_ns = None
            return ProximityResult(
                state=ProximityState.INACTIVE,
                fire_side=None,
                reference_price=0.0,
                upper_band=0.0,
                lower_band=0.0,
                bid_proximity_pct=math.nan,
                ask_proximity_pct=math.nan,
                spread_used=0.0,
                band_pct=0.0,
            )

        self._buffer.append((timestamp_ns, price))
        cutoff = timestamp_ns - self._ref_window_ns
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()

        oldest_ts = self._buffer[0][0]
        in_warmup = (timestamp_ns - oldest_ts) < self._warmup_ns
        # Return INACTIVE only on a genuine cold start (no reference price ever established).
        # If _published_ref > 0, the module was previously warm: per SIP §4 the reference
        # price stays in effect during gaps, so we use the persisted value instead of
        # returning INACTIVE just because the buffer is sparse after a gap.
        if in_warmup and self._published_ref == 0.0:
            band_pct = _band_pct_for_time(et_sec)
            self._in_limit_state = False
            self._pin_start_ns = None
            return ProximityResult(
                state=ProximityState.INACTIVE,
                fire_side=None,
                reference_price=0.0,
                upper_band=0.0,
                lower_band=0.0,
                bid_proximity_pct=math.nan,
                ask_proximity_pct=math.nan,
                spread_used=0.0,
                band_pct=band_pct,
            )

        # Compute rolling mean
        n = len(self._buffer)
        s = 0.0
        for _, p in self._buffer:
            s += p
        new_mean = s / n

        # Sticky reference price update: frozen during active EXIT_HALT
        if not self._in_limit_state:
            if self._published_ref == 0.0:
                # Cold start: set unconditionally on first post-warmup tick
                self._published_ref = new_mean
            else:
                rel_change = abs(new_mean - self._published_ref) / self._published_ref
                if rel_change >= _STICKY_MIN_CHANGE:
                    self._published_ref = new_mean

        band_pct = _band_pct_for_time(et_sec)
        upper_band = round(self._published_ref * (1.0 + band_pct), 2)
        lower_band = round(self._published_ref * (1.0 - band_pct), 2) if self.lower_enabled else 0.0

        if price <= 0:
            self._in_limit_state = False
            self._pin_start_ns = None
            return ProximityResult(
                state=ProximityState.INACTIVE,
                fire_side=None,
                reference_price=self._published_ref,
                upper_band=upper_band,
                lower_band=lower_band,
                bid_proximity_pct=math.nan,
                ask_proximity_pct=math.nan,
                spread_used=0.0,
                band_pct=band_pct,
            )

        # Determine proximity using prevailing bid/ask; fallback to trade price
        valid_quote = (
            bid is not None and ask is not None
            and bid > 0.0 and ask > bid
        )

        if valid_quote:
            spread_used = float(ask) - float(bid)
            bid_prox = (
                (upper_band - float(bid)) / upper_band
                if upper_band > 0.0 else math.nan
            )
            upper_fires = not math.isnan(bid_prox) and bid_prox <= self.proximity_threshold_upper
            if self.lower_enabled and lower_band > 0.0:
                ask_prox = (float(ask) - lower_band) / lower_band
                lower_fires = not math.isnan(ask_prox) and ask_prox <= self.proximity_threshold_lower
            else:
                ask_prox = math.nan
                lower_fires = False
        else:
            if not self._warned_fallback:
                self._warned_fallback = True
            spread_used = 0.0
            bid_prox = math.nan
            price_prox_upper = (
                (upper_band - price) / upper_band
                if upper_band > 0.0 else math.nan
            )
            upper_fires = not math.isnan(price_prox_upper) and price_prox_upper <= self.proximity_threshold_upper
            if self.lower_enabled and lower_band > 0.0:
                price_prox_lower = (price - lower_band) / lower_band
                ask_prox = math.nan
                lower_fires = not math.isnan(price_prox_lower) and price_prox_lower <= self.proximity_threshold_lower
            else:
                ask_prox = math.nan
                lower_fires = False

        # Upper band: apply pin+duration clock (V3). Lower band: immediate fire only.
        pin_duration_sec = 0.0
        fire_side: Optional[str] = None
        state: ProximityState

        if upper_fires:
            if self._in_limit_state:
                if self._pin_start_ns is not None:
                    pin_duration_sec = (timestamp_ns - self._pin_start_ns) / _NS_PER_SECOND
                state = ProximityState.EXIT_HALT
                fire_side = "upper"
            elif self.luld_exit_duration_sec <= 0.0:
                if self._pin_start_ns is None:
                    self._pin_start_ns = timestamp_ns
                pin_duration_sec = (timestamp_ns - self._pin_start_ns) / _NS_PER_SECOND
                state = ProximityState.EXIT_HALT
                fire_side = "upper"
            else:
                if self._pin_start_ns is None:
                    self._pin_start_ns = timestamp_ns
                pin_duration_sec = (timestamp_ns - self._pin_start_ns) / _NS_PER_SECOND
                if pin_duration_sec >= self.luld_exit_duration_sec:
                    state = ProximityState.EXIT_HALT
                    fire_side = "upper"
                else:
                    state = ProximityState.SAFE
                    fire_side = None
        elif lower_fires:
            # Lower band fires immediately; upper takes precedence if both fire
            self._pin_start_ns = None
            state = ProximityState.EXIT_HALT
            fire_side = "lower"
        else:
            self._pin_start_ns = None
            state = ProximityState.SAFE
            fire_side = None

        self._in_limit_state = (state == ProximityState.EXIT_HALT)

        return ProximityResult(
            state=state,
            fire_side=fire_side,
            reference_price=self._published_ref,
            upper_band=upper_band,
            lower_band=lower_band,
            bid_proximity_pct=bid_prox,
            ask_proximity_pct=ask_prox,
            spread_used=spread_used,
            band_pct=band_pct,
            pin_duration_sec=pin_duration_sec,
        )
