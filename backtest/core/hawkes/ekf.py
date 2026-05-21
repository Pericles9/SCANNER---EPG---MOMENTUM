"""
Kalman Intensity Estimator (log-state EKF) and Snyder Filter.

Estimates arrival intensity λ̂(t) from inter-arrival times.
Log-state EKF ensures positivity without clipping.
Snyder filter implemented alongside for Phase A benchmark.

Dependencies: None (pure Numba).
"""
from __future__ import annotations

import numba as nb
import numpy as np


# ── Numba-compiled EKF core ─────────────────────────────────────────────

@nb.njit(cache=True, fastmath=True)
def _ekf_update(
    x: float,        # log(lambda_hat) state
    P: float,        # scalar variance
    dt: float,       # inter-arrival time in seconds
    Q: float,        # process noise variance
    c_R: float,      # R floor coefficient
    lambda_ref: float,
) -> tuple:           # (x_new, P_new, lambda_hat)
    """Single EKF predict+update step in log-space."""
    # Predict
    P_pred = P + Q

    # Observation: instantaneous rate y = 1/dt
    y = 1.0 / dt if dt > 0 else 0.0

    # Current lambda estimate
    lam_hat = np.exp(x)

    # Jacobian H = d(lambda)/d(log lambda) = lambda
    H = lam_hat

    # Observation noise R = max(lambda_hat, c_R * lambda_ref)^2
    R_floor = c_R * lambda_ref
    R_val = max(lam_hat, R_floor)
    R_noise = R_val * R_val

    # Innovation
    S = H * P_pred * H + R_noise
    K = P_pred * H / S
    innov = y - lam_hat

    # Update
    x_new = x + K * innov
    # Clamp log-state to prevent divergence: lambda in [~2e-9, ~5e8]
    x_new = min(max(x_new, -20.0), 20.0)
    P_new = (1.0 - K * H) * P_pred

    # Ensure P stays positive (numerical safety)
    if P_new < 1e-30:
        P_new = 1e-30

    lam_hat_new = np.exp(x_new)
    return x_new, P_new, lam_hat_new


@nb.njit(cache=True)
def _ekf_halt_resume(P: float, Q: float, t_halt: float, lambda_ref: float) -> float:
    """Inflate uncertainty after a halt period."""
    return P + Q * t_halt * lambda_ref


# ── Python wrapper class ────────────────────────────────────────────────

class KalmanIntensityEstimator:
    """1D Extended Kalman Filter for Poisson intensity, log-state formulation.

    Initializes at lambda_ref with high uncertainty, converges rapidly.
    """

    def __init__(
        self,
        lambda_ref: float,
        Q_scale: float = 0.01,
        c_R: float = 0.1,
    ):
        self._lambda_ref = lambda_ref
        self._Q = (Q_scale * lambda_ref) ** 2
        self._c_R = c_R

        # Initialize state: x = log(lambda_ref), P = (3 * lambda_ref)^2
        self._x = np.log(max(lambda_ref, 1e-10))
        self._P = (3.0 * lambda_ref) ** 2
        self._lambda_hat = lambda_ref

    @property
    def lambda_hat(self) -> float:
        return self._lambda_hat

    def update(self, dt: float) -> float:
        """Update with new inter-arrival time. Returns lambda_hat."""
        if dt <= 0:
            return self._lambda_hat
        self._x, self._P, self._lambda_hat = _ekf_update(
            self._x, self._P, dt, self._Q, self._c_R, self._lambda_ref,
        )
        return self._lambda_hat

    def on_halt_resume(self, halt_duration_sec: float) -> None:
        """Inflate uncertainty after a LULD halt."""
        self._P = _ekf_halt_resume(
            self._P, self._Q, halt_duration_sec, self._lambda_ref,
        )

    def reset(self) -> None:
        """Reset to initial state."""
        self._x = np.log(max(self._lambda_ref, 1e-10))
        self._P = (3.0 * self._lambda_ref) ** 2
        self._lambda_hat = self._lambda_ref

    def get_state(self) -> tuple:
        """Return (x, P) for serialization / Numba replay."""
        return self._x, self._P


# ── Numba-compiled Snyder filter core ───────────────────────────────────

@nb.njit(cache=True, fastmath=True)
def _snyder_update(
    lam: float,       # current intensity estimate
    P: float,         # variance
    dt: float,        # inter-arrival time
    Q: float,         # process noise
    c_R: float,
    lambda_ref: float,
) -> tuple:            # (lam_new, P_new)
    """Snyder filter: ODE-based intensity tracking for Poisson processes.

    Between events: dλ/dt = -a*(λ - μ), dP/dt = -2*a*P + Q_rate
    At event: λ+ = λ- + P- / max(λ-, floor), P+ = P- - P-^2 / (λ-^2 + R)

    Uses an approximation: between events, decay λ toward lambda_ref
    with a relaxation rate. At each event, perform a Bayesian update.
    """
    # Between-event ODE: decay toward lambda_ref
    a = 0.1  # relaxation rate
    Q_rate = Q / max(dt, 0.001)

    # Simple exponential relaxation
    decay = np.exp(-a * dt)
    lam_pred = lambda_ref + (lam - lambda_ref) * decay
    P_pred = P * decay * decay + Q_rate * dt

    # At-event update
    R_floor = c_R * lambda_ref
    R_val = max(lam_pred, R_floor)
    R_noise = R_val * R_val

    # Observation: an event occurred, so y = 1/dt
    y = 1.0 / dt if dt > 0 else lam_pred
    K = P_pred / (P_pred + R_noise)
    lam_new = lam_pred + K * (y - lam_pred)
    P_new = (1.0 - K) * P_pred

    if lam_new < 1e-10:
        lam_new = 1e-10
    if P_new < 1e-30:
        P_new = 1e-30

    return lam_new, P_new


class SnyderFilter:
    """Snyder filter for Poisson intensity estimation.

    Tracks intensity via between-event ODE relaxation + at-event Bayesian update.
    Used as benchmark against EKF in Phase A.
    """

    def __init__(
        self,
        lambda_ref: float,
        Q_scale: float = 0.01,
        c_R: float = 0.1,
    ):
        self._lambda_ref = lambda_ref
        self._Q = (Q_scale * lambda_ref) ** 2
        self._c_R = c_R
        self._lambda_hat = lambda_ref
        self._P = (3.0 * lambda_ref) ** 2

    @property
    def lambda_hat(self) -> float:
        return self._lambda_hat

    def update(self, dt: float) -> float:
        """Update with new inter-arrival time. Returns lambda_hat."""
        if dt <= 0:
            return self._lambda_hat
        self._lambda_hat, self._P = _snyder_update(
            self._lambda_hat, self._P, dt,
            self._Q, self._c_R, self._lambda_ref,
        )
        return self._lambda_hat

    def on_halt_resume(self, halt_duration_sec: float) -> None:
        """Inflate uncertainty after a halt."""
        self._P += self._Q * halt_duration_sec * self._lambda_ref

    def reset(self) -> None:
        self._lambda_hat = self._lambda_ref
        self._P = (3.0 * self._lambda_ref) ** 2


# ── Batch replay for benchmarking ────────────────────────────────────────

@nb.njit(cache=True, fastmath=True)
def ekf_replay(
    t_sec: np.ndarray,
    lambda_ref: float,
    Q_scale: float,
    c_R: float,
) -> np.ndarray:
    """Replay EKF over a full event sequence. Returns lambda_hat per event."""
    N = len(t_sec)
    out = np.empty(N, dtype=np.float64)
    if N == 0:
        return out

    if lambda_ref < 1e-10:
        out[:] = 1e-10
        return out
    Q = (Q_scale * lambda_ref) ** 2
    x = np.log(max(lambda_ref, 1e-10))
    P = (3.0 * lambda_ref) ** 2
    lam_hat = lambda_ref
    out[0] = lam_hat

    for i in range(1, N):
        dt = t_sec[i] - t_sec[i - 1]
        if dt > 0:
            x, P, lam_hat = _ekf_update(x, P, dt, Q, c_R, lambda_ref)
        out[i] = lam_hat
    return out


@nb.njit(cache=True, fastmath=True)
def snyder_replay(
    t_sec: np.ndarray,
    lambda_ref: float,
    Q_scale: float,
    c_R: float,
) -> np.ndarray:
    """Replay Snyder filter over a full event sequence. Returns lambda_hat per event."""
    N = len(t_sec)
    out = np.empty(N, dtype=np.float64)
    if N == 0:
        return out

    if lambda_ref < 1e-10:
        out[:] = 1e-10
        return out
    Q = (Q_scale * lambda_ref) ** 2
    lam = lambda_ref
    P = (3.0 * lambda_ref) ** 2
    out[0] = lam

    for i in range(1, N):
        dt = t_sec[i] - t_sec[i - 1]
        if dt > 0:
            lam, P = _snyder_update(lam, P, dt, Q, c_R, lambda_ref)
        out[i] = lam
    return out
