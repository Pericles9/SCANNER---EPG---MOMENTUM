"""
ParticipationGate — Dollar volume intensity gate for the EPG.

Tracks exponentially decaying dollar volume intensity lambda_V(t) and
compares it to a running peak threshold. The gate returns one of three
states: WARMUP, PASS, or FAIL.

Formula:
    lambda_V(t_i) = lambda_V(t_{i-1}) * exp(-ln2 * dt / tau) + dv_i * (ln2 / tau)

where dv_i = price_i * size_i (dollar volume of the trade).

The running peak is causal: lambda_V_peak at time t = max(lambda_V[T_event:t]).
The threshold ratchets up with each new peak — it never decreases.
"""
from __future__ import annotations

import enum
import math
from typing import Optional


LN2 = math.log(2)
WARMUP_DURATION_SEC = 300.0  # 5 minutes


class GateState(enum.Enum):
    """State of the participation gate."""
    WARMUP = "WARMUP"   # First 5 minutes after T_event — gate inactive
    PASS = "PASS"       # lambda_V >= p * running_peak
    FAIL = "FAIL"       # lambda_V < p * running_peak
    INACTIVE = "INACTIVE"  # T_event not yet established


class ParticipationGate:
    """Dollar volume intensity gate with running peak threshold."""

    def __init__(
        self,
        half_life_seconds: float,
        peak_threshold_p: float,
        warmup_seconds: float = WARMUP_DURATION_SEC,
    ):
        """
        Parameters
        ----------
        half_life_seconds : float
            Exponential decay half-life for lambda_V (e.g. 300 for 5 min).
        peak_threshold_p : float
            Fraction of running peak required for PASS (e.g. 0.65).
        warmup_seconds : float
            Duration of WARMUP period after T_event (default 300s = 5 min).
        """
        if half_life_seconds <= 0:
            raise ValueError(f"half_life must be positive, got {half_life_seconds}")
        if not (0 < peak_threshold_p <= 1.0):
            raise ValueError(f"peak_threshold_p must be in (0, 1], got {peak_threshold_p}")

        self.half_life = half_life_seconds
        self.peak_threshold_p = peak_threshold_p
        self.warmup_seconds = warmup_seconds

        # Precompute decay constant: ln2 / tau
        self._decay_rate = LN2 / half_life_seconds

        # State
        self._lambda_v: float = 0.0
        self._lambda_v_peak: float = 0.0
        self._t_event: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._active = False

    @property
    def lambda_v(self) -> float:
        """Current dollar volume intensity."""
        return self._lambda_v

    @property
    def lambda_v_peak(self) -> float:
        """Running peak of lambda_V since T_event."""
        return self._lambda_v_peak

    @property
    def t_event(self) -> Optional[float]:
        """T_event timestamp, or None if not yet set."""
        return self._t_event

    @property
    def threshold(self) -> float:
        """Current threshold = p * running_peak."""
        return self.peak_threshold_p * self._lambda_v_peak

    def activate(self, t_event: float) -> None:
        """
        Called when T_event is first established by EventAnchor.

        Resets all internal state and begins tracking lambda_V from this
        moment. The first `warmup_seconds` after t_event return WARMUP.

        Parameters
        ----------
        t_event : float
            Timestamp when T_event was detected (same time domain as
            update() timestamps).
        """
        self._t_event = t_event
        self._lambda_v = 0.0
        self._lambda_v_peak = 0.0
        self._last_timestamp = t_event
        self._active = True

    def update(self, dollar_vol: float, timestamp: float) -> GateState:
        """
        Update lambda_V with a new trade and return gate state.

        Parameters
        ----------
        dollar_vol : float
            Dollar volume of this trade (price * size).
        timestamp : float
            Trade timestamp (seconds from session start, or absolute —
            must be consistent with t_event).

        Returns
        -------
        GateState
            INACTIVE if T_event not set, WARMUP if within warmup period,
            PASS if lambda_V >= threshold, FAIL otherwise.
        """
        if not self._active or self._t_event is None:
            return GateState.INACTIVE

        # Compute dt and decay
        dt = timestamp - self._last_timestamp
        if dt < 0:
            dt = 0.0  # protect against out-of-order timestamps

        # Exponential decay + new contribution
        decay = math.exp(-self._decay_rate * dt)
        self._lambda_v = self._lambda_v * decay + dollar_vol * self._decay_rate
        self._last_timestamp = timestamp

        # Update running peak (causal)
        if self._lambda_v > self._lambda_v_peak:
            self._lambda_v_peak = self._lambda_v

        # Check warmup
        time_since_t_event = timestamp - self._t_event
        if time_since_t_event < self.warmup_seconds:
            return GateState.WARMUP

        # Check threshold
        if self._lambda_v >= self.peak_threshold_p * self._lambda_v_peak:
            return GateState.PASS
        else:
            return GateState.FAIL

    def reset(self) -> None:
        """
        Reset for continuation events or new session.

        Clears peak, lambda_V, and T_event. Call activate() again when
        the new session's T_event is established.
        """
        self._lambda_v = 0.0
        self._lambda_v_peak = 0.0
        self._t_event = None
        self._last_timestamp = None
        self._active = False
