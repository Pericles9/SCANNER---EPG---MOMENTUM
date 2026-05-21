"""Inverse-intensity re-entry signal (Phase B).

After EXIT_D fires and the position is flat within the same EPG PASS window,
this module monitors buy-side intensity dominance. A re-entry fires when
I_buy(t) >= (1 - theta) holds continuously for >= tau_recovery seconds.

Re-entry condition per tick:
    I_buy(t) = lambda_buy(t) / (lambda_buy(t) + lambda_sell(t)) >= (1 - theta)

The gate_state check (must be PASS) is enforced inside update(); passing any
non-PASS state resets the timer without firing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.epg.gate import GateState

_NS_PER_SEC = 1_000_000_000


class ReentrySignal:
    """Streaming inverse-intensity re-entry timer.

    Stateful per PASS window. Call reset() after each re-entry fires and
    when the PASS window closes without a re-entry.
    """

    def __init__(self, theta: float, tau_recovery_sec: float):
        """
        Parameters
        ----------
        theta : float
            Shared EXIT_D sell-dominance threshold; buy-dominance threshold
            for re-entry = (1 - theta). Must be in (0, 1).
        tau_recovery_sec : float
            Minimum continuous seconds of buy dominance required to fire.
        """
        if not (0 < theta < 1):
            raise ValueError(f"theta must be in (0, 1), got {theta}")
        if tau_recovery_sec <= 0:
            raise ValueError(
                f"tau_recovery_sec must be positive, got {tau_recovery_sec}"
            )

        self.theta = theta
        self._buy_threshold = 1.0 - theta
        self._tau_recovery_ns = int(tau_recovery_sec * _NS_PER_SEC)
        self._timer_start_ns: int | None = None

    def reset(self) -> None:
        """Clear timer. Call after re-entry fires or PASS window closes."""
        self._timer_start_ns = None

    def update(
        self,
        timestamp_ns: int,
        lam_buy: float,
        lam_sell: float,
        gate_state: GateState,
    ) -> bool:
        """Process one tick and return True if re-entry fires.

        Parameters
        ----------
        timestamp_ns : int
            Trade timestamp in unix nanoseconds.
        lam_buy : float
            Current buy intensity lambda_buy(t) from Hawkes replay.
        lam_sell : float
            Current sell intensity lambda_sell(t) from Hawkes replay.
        gate_state : GateState
            Current EPG gate state. Re-entry only fires when PASS.

        Returns
        -------
        bool
            True on the tick when the timer first reaches tau_recovery.
            Timer is reset to None on True return; caller should also call
            reset() to prevent duplicate fires if the signal is polled again.
        """
        if gate_state != GateState.PASS:
            self._timer_start_ns = None
            return False

        lam_total = lam_buy + lam_sell
        I_buy = lam_buy / lam_total if lam_total > 0 else 0.0

        if I_buy >= self._buy_threshold:
            if self._timer_start_ns is None:
                self._timer_start_ns = timestamp_ns
            elif timestamp_ns - self._timer_start_ns >= self._tau_recovery_ns:
                self._timer_start_ns = None
                return True
        else:
            self._timer_start_ns = None

        return False
