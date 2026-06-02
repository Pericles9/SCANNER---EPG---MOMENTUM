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

Peak cooling: when the gate has been in FAIL for >= m_cool_sec seconds, the running
peak decays with half-life tau_cool_sec. This prevents the ratchet from locking out
re-entry after a multi-leg move. Set m_cool_sec=0 to disable (default, no change to
existing behavior).
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
    """Dollar volume intensity gate with running peak threshold and optional hysteresis."""

    def __init__(
        self,
        half_life_seconds: float,
        peak_threshold_p: float,
        warmup_seconds: float = WARMUP_DURATION_SEC,
        *,
        p_open: Optional[float] = None,
        p_close: Optional[float] = None,
        m_cool_sec: float = 0.0,
        tau_cool_sec: float = 120.0,
    ):
        """
        Parameters
        ----------
        half_life_seconds : float
            Exponential decay half-life for lambda_V (e.g. 300 for 5 min).
        peak_threshold_p : float
            Fraction of running peak for symmetric PASS (e.g. 0.65). When
            p_open/p_close are omitted, both default to this value, reproducing
            the original symmetric behavior exactly.
        warmup_seconds : float
            Duration of WARMUP period after T_event (default 300s = 5 min).
        p_open : float, optional
            FAIL→PASS opening threshold fraction. Defaults to peak_threshold_p.
        p_close : float, optional
            PASS→FAIL closing threshold fraction. Defaults to p_open.
            Must satisfy p_close <= p_open.
        m_cool_sec : float
            Minimum continuous FAIL duration (seconds) before peak cooling
            activates. Set to 0 to disable cooling entirely (default).
        tau_cool_sec : float
            Half-life (seconds) of peak decay once cooling is active.
            Ignored when m_cool_sec=0.
        """
        if half_life_seconds <= 0:
            raise ValueError(f"half_life must be positive, got {half_life_seconds}")
        if not (0 < peak_threshold_p <= 1.0):
            raise ValueError(f"peak_threshold_p must be in (0, 1], got {peak_threshold_p}")
        if m_cool_sec < 0:
            raise ValueError(f"m_cool_sec must be >= 0, got {m_cool_sec}")
        if m_cool_sec > 0 and tau_cool_sec <= 0:
            raise ValueError(f"tau_cool_sec must be positive when cooling enabled, got {tau_cool_sec}")

        _p_open = p_open if p_open is not None else peak_threshold_p
        _p_close = p_close if p_close is not None else _p_open
        if not (0 < _p_open <= 1.0):
            raise ValueError(f"p_open must be in (0, 1], got {_p_open}")
        if not (0 < _p_close <= 1.0):
            raise ValueError(f"p_close must be in (0, 1], got {_p_close}")
        if _p_close > _p_open:
            raise ValueError(f"p_close ({_p_close}) must be <= p_open ({_p_open})")

        self.half_life = half_life_seconds
        self.peak_threshold_p = peak_threshold_p
        self.p_open = _p_open
        self.p_close = _p_close
        self.warmup_seconds = warmup_seconds
        self.m_cool_sec = m_cool_sec
        self.tau_cool_sec = tau_cool_sec

        # Precompute decay constant: ln2 / tau
        self._decay_rate = LN2 / half_life_seconds

        # State
        self._lambda_v: float = 0.0
        self._lambda_v_peak: float = 0.0
        self._t_event: Optional[float] = None
        self._last_timestamp: Optional[float] = None
        self._active = False
        self._in_pass: bool = False

        # Peak cooling state
        self._fail_start_ts: Optional[float] = None
        self._cooling_active: bool = False
        self._peak_at_cool_start: float = 0.0
        self._cool_start_ts: Optional[float] = None

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
        """Opening threshold = p_open * running_peak (FAIL→PASS boundary)."""
        return self.p_open * self._lambda_v_peak

    @property
    def threshold_close(self) -> float:
        """Closing threshold = p_close * running_peak (PASS→FAIL boundary)."""
        return self.p_close * self._lambda_v_peak

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
        self._fail_start_ts = None
        self._cooling_active = False
        self._peak_at_cool_start = 0.0
        self._cool_start_ts = None

    def update(self, dollar_vol: float, timestamp: float, side: int = 0) -> GateState:
        """
        Update lambda_V with a new trade and return gate state.

        Parameters
        ----------
        dollar_vol : float
            Dollar volume of this trade (price * size).
        timestamp : float
            Trade timestamp (seconds from session start, or absolute —
            must be consistent with t_event).
        side : int, optional
            Trade side (+1 buy, -1 sell, 0 unknown). Ignored by this gate;
            included for interface uniformity with gate_variants classes.

        Returns
        -------
        GateState
            INACTIVE if T_event not set, WARMUP if within warmup period,
            PASS/FAIL based on asymmetric hysteresis threshold.
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

        # Check warmup
        time_since_t_event = timestamp - self._t_event
        if time_since_t_event < self.warmup_seconds:
            # Still accumulate peak during warmup (normal path)
            if self._lambda_v > self._lambda_v_peak:
                self._lambda_v_peak = self._lambda_v
            return GateState.WARMUP

        # Peak update and cooling logic (post-warmup only)
        if self._in_pass:
            # PASS: update peak normally; clear any lingering cooling state
            self._fail_start_ts = None
            self._cooling_active = False
            if self._lambda_v > self._lambda_v_peak:
                self._lambda_v_peak = self._lambda_v
        elif self.m_cool_sec > 0:
            # FAIL with cooling enabled: manage the fail timer and peak decay
            if self._fail_start_ts is None:
                self._fail_start_ts = timestamp
            fail_duration = timestamp - self._fail_start_ts
            if not self._cooling_active and fail_duration >= self.m_cool_sec:
                self._cooling_active = True
                self._peak_at_cool_start = self._lambda_v_peak
                self._cool_start_ts = timestamp
            if self._cooling_active:
                cool_elapsed = timestamp - self._cool_start_ts
                self._lambda_v_peak = self._peak_at_cool_start * math.exp(
                    -LN2 * cool_elapsed / self.tau_cool_sec
                )
            else:
                # Cooling not yet active — peak updates normally
                # (In FAIL, λ_V < p_open * peak ≤ peak, so this is only
                # reachable when building from zero on the very first tick.)
                if self._lambda_v > self._lambda_v_peak:
                    self._lambda_v_peak = self._lambda_v
        else:
            # FAIL with cooling disabled: update peak normally
            if self._lambda_v > self._lambda_v_peak:
                self._lambda_v_peak = self._lambda_v

        # Asymmetric hysteresis: FAIL→PASS at p_open, PASS→FAIL at p_close.
        # Symmetric when p_open == p_close (reproduces original behavior exactly).
        if self._in_pass:
            if self._lambda_v < self.p_close * self._lambda_v_peak:
                self._in_pass = False
                if self.m_cool_sec > 0:
                    self._fail_start_ts = timestamp
                    self._cooling_active = False
        else:
            if self._lambda_v >= self.p_open * self._lambda_v_peak:
                self._in_pass = True
                self._fail_start_ts = None
                self._cooling_active = False
        return GateState.PASS if self._in_pass else GateState.FAIL

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
        self._in_pass = False
        self._fail_start_ts = None
        self._cooling_active = False
        self._peak_at_cool_start = 0.0
        self._cool_start_ts = None
