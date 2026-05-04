"""
EventAnchor — T_event detector for the Event Participation Gate.

T_event is the first moment Hawkes lambda_hat(t) crosses k * lambda_ref,
indicating a genuine participation spike. Once set, T_event does not move
for the remainder of the session — even if lambda_hat drops and re-crosses.

Continuation events (same ticker on consecutive days) call reset() at
session open to re-anchor T_event to the new session's first crossing.

# DESIGN NOTE: lambda_ref for EventAnchor must be the Hawkes background rate.
# Correct:  mu_buy + mu_sell  from the cold-start MLE fit.
#           This is trades/sec with zero self-excitation — the pure baseline.
#           k × mu means "current intensity is k× above background."
# Wrong:    (mu_buy + mu_sell) / (1 - n_base)  — equilibrium including self-excitation.
#           For high-n_base events this amplifies the threshold, making T_event
#           harder to fire precisely when self-excitation is strongest. Backwards.
# Wrong:    Empirical T-3..T-1 trades/sec — different scale, inflated by burst days.
# Call set_lambda_ref(mu_buy + mu_sell) after cold-start fit completes.
# See Phase R2 post-mortem for the full diagnosis.
"""
from __future__ import annotations

from typing import Optional


class EventAnchor:
    """Detects T_event: first lambda_hat crossing of k * lambda_ref."""

    def __init__(self, lambda_ref: float, k_multiplier: float):
        """
        Parameters
        ----------
        lambda_ref : float
            Initial lambda_ref (Hawkes intensity scale). Should be overridden
            with set_lambda_ref() after cold-start MLE fit using
            (mu_buy + mu_sell) / (1 - n_base) from the fitted params.
        k_multiplier : float
            Crossing threshold multiplier (e.g. 5 means fire when
            lambda_hat > 5 * lambda_ref).
        """
        if lambda_ref <= 0:
            raise ValueError(f"lambda_ref must be positive, got {lambda_ref}")
        if k_multiplier <= 0:
            raise ValueError(f"k_multiplier must be positive, got {k_multiplier}")

        self._lambda_ref = lambda_ref
        self.k_multiplier = k_multiplier

        self._t_event: Optional[float] = None
        self._fired = False

    @property
    def lambda_ref(self) -> float:
        """Current lambda_ref used for threshold computation."""
        return self._lambda_ref

    @property
    def threshold(self) -> float:
        """Current crossing threshold: k_multiplier * lambda_ref."""
        return self.k_multiplier * self._lambda_ref

    def set_lambda_ref(self, lambda_ref: float) -> None:
        """
        Update lambda_ref used for threshold computation.

        Call once after cold-start MLE fit completes, before event replay begins.
        lambda_ref must be the pure background rate from the cold-start fit:
            mu_buy + mu_sell

        Do NOT pass (mu_buy + mu_sell) / (1 - n_base) — the equilibrium formula
        amplifies the value for high-n_base events, making T_event harder to fire
        precisely during active bursts (backwards from intended behavior).
        Do NOT pass empirical T-3..T-1 trades/sec — that quantity belongs in
        fit_hawkes_forgetting() for MLE fitting only.

        Parameters
        ----------
        lambda_ref : float
            Hawkes equilibrium arrival rate from cold-start fit.
        """
        if lambda_ref <= 0.0:
            raise ValueError(f"lambda_ref must be positive, got {lambda_ref}")
        self._lambda_ref = lambda_ref

    @property
    def t_event(self) -> Optional[float]:
        """T_event timestamp, or None if not yet crossed."""
        return self._t_event

    @property
    def has_fired(self) -> bool:
        """Whether T_event has been established this session."""
        return self._fired

    def update(self, lambda_hat_t: float, timestamp: float) -> Optional[float]:
        """
        Check if lambda_hat crosses the threshold.

        Parameters
        ----------
        lambda_hat_t : float
            Current total Hawkes intensity (lambda_buy + lambda_sell).
        timestamp : float
            Current timestamp (seconds from session start or nanoseconds —
            must be consistent with how the caller uses it).

        Returns
        -------
        Optional[float]
            T_event timestamp on first crossing, None before crossing.
            After first crossing, returns the same T_event on every call.
        """
        if self._fired:
            return self._t_event

        if lambda_hat_t > self.threshold:
            self._t_event = timestamp
            self._fired = True
            return self._t_event

        return None

    def reset(self) -> None:
        """
        Reset for continuation events. Call at session open.

        Clears T_event so it can be re-anchored to the new session's
        first crossing.
        """
        self._t_event = None
        self._fired = False
