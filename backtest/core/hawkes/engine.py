"""
Univariate clock-time Hawkes engine with K=1, fixed beta, and atomic parameter swap.

K=1 selected based on Phase A iteration 2 empirical evidence: only kernel 0
carried weight. Beta is a fixed design constant (not fitted by MLE) set to
match the regime-detection timescale (~7s half-life). Cross-excitation dropped
in iter 7 (collapsed to 1e-8 across all events). swap_params() enables online
refitting of the 4 fitted parameters (2 self-alphas + 2 mus).

Dependencies: core/hawkes/ekf.py
HPC note: Inner update loop is Numba-compiled. K=1 uses scalar ops.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import numba as nb
import numpy as np

from core.hawkes.ekf import KalmanIntensityEstimator


# ── Dataclass for output state ──────────────────────────────────────────

@dataclass
class HawkesState:
    """Full Hawkes engine state at a single event."""
    lambda_buy: float
    lambda_sell: float
    lambda_total: float
    E: float          # excitation ratio: lambda_total / (mu_buy + mu_sell)
    Edot: float       # EMA of dE/dt
    n_base: float     # branching ratio (spectral radius, beta_mle)
    n_eff: float      # branching ratio (spectral radius, beta_eff) — diagnostics
    lambda_hat: float  # Snyder/EKF estimate
    R_buy: float       # scalar (K=1)
    R_sell: float      # scalar (K=1)


# ── Numba core: single event update ─────────────────────────────────────

@nb.njit(cache=True, fastmath=True)
def _hawkes_update_event(
    t: float,
    t_prev: float,
    side: int,                      # +1 buy, -1 sell
    R_buy: float,                   # scalar (K=1)
    R_sell: float,                  # scalar (K=1)
    alpha_self_buy: float,
    alpha_cross_buy: float,
    alpha_self_sell: float,
    alpha_cross_sell: float,
    mu_buy: float,
    mu_sell: float,
    beta_mle: float,                # scalar — fitted by MLE
    lambda_hat: float,              # Snyder estimate
    lambda_ref: float,
) -> tuple:
    """Update Hawkes state for a single event. Returns (R_buy, R_sell, lam_buy, lam_sell, beta_eff)."""
    dt = t - t_prev

    # Adaptive beta: scale by current arrival rate ratio
    ratio = max(lambda_hat / max(lambda_ref, 1e-10), 0.1)
    beta_eff = beta_mle * ratio

    # Decay existing R
    decay = np.exp(-beta_eff * dt)
    R_buy *= decay
    R_sell *= decay

    # Compute bivariate intensities at left-limit lambda(t^-)
    # Uses R from history only (events before current), NOT including current event
    lam_buy = mu_buy + alpha_self_buy * R_buy + alpha_cross_buy * R_sell
    lam_sell = mu_sell + alpha_self_sell * R_sell + alpha_cross_sell * R_buy

    # Floor intensities at zero
    if lam_buy < 0.0:
        lam_buy = 0.0
    if lam_sell < 0.0:
        lam_sell = 0.0

    # Add new event contribution AFTER intensity computation
    if side == 1:
        R_buy += 1.0
    else:
        R_sell += 1.0

    return R_buy, R_sell, lam_buy, lam_sell, beta_eff


@nb.njit(cache=True, fastmath=True)
def _compute_edot(
    E_new: float,
    E_prev: float,
    dt: float,
    edot_prev: float,
    rho_E: float,
) -> float:
    """EMA of per-event dE/dt with 1.0s denominator cap."""
    dt_capped = min(dt, 1.0)
    if dt_capped < 1e-12:
        dt_capped = 1e-12
    raw_slope = (E_new - E_prev) / dt_capped
    return rho_E * edot_prev + (1.0 - rho_E) * raw_slope


@nb.njit(cache=True, fastmath=True)
def _branching_ratio_2x2_scalar(
    alpha_self_buy: float,
    alpha_cross_buy: float,
    alpha_self_sell: float,
    alpha_cross_sell: float,
    beta: float,
) -> float:
    """Exact spectral radius of the 2x2 branching matrix for K=1.

    M = (1/beta) * [[a_self_buy,  a_cross_buy],
                     [a_cross_sell, a_self_sell]]

    n = (M11 + M22 + sqrt((M11-M22)^2 + 4*M12*M21)) / 2
    """
    inv_beta = 1.0 / beta
    M11 = alpha_self_buy * inv_beta
    M12 = alpha_cross_buy * inv_beta
    M21 = alpha_cross_sell * inv_beta
    M22 = alpha_self_sell * inv_beta

    trace = M11 + M22
    diff = M11 - M22
    disc = diff * diff + 4.0 * M12 * M21

    if disc < 0.0:
        disc = 0.0

    return (trace + np.sqrt(disc)) / 2.0


# ── Full replay function (Numba-compiled, no allocations in loop) ───────

@nb.njit(cache=True, fastmath=True)
def hawkes_replay(
    t_sec: np.ndarray,         # float64, seconds from first event
    sides: np.ndarray,         # int8, +1/-1
    alpha_self_buy: float,
    alpha_cross_buy: float,
    alpha_self_sell: float,
    alpha_cross_sell: float,
    mu_buy: float,
    mu_sell: float,
    beta_mle: float,
    lambda_ref: float,
    rho_E: float,
    Q_scale: float,
    c_R: float,
    # Pre-allocated output arrays (filled in-place)
    lam_buy_out: np.ndarray,
    lam_sell_out: np.ndarray,
    E_out: np.ndarray,
    Edot_out: np.ndarray,
    lam_hat_out: np.ndarray,
    n_base_out: np.ndarray,
) -> None:
    """Replay full Hawkes + EKF over an event sequence.

    All output arrays are pre-allocated and filled in-place.
    No allocations in the hot loop.
    """
    N = len(t_sec)
    if N == 0:
        return

    mu_total = mu_buy + mu_sell
    if mu_total < 1e-10:
        mu_total = 1e-10

    # EKF log-state init
    Q = (Q_scale * lambda_ref) ** 2
    ekf_x = np.log(max(lambda_ref, 1e-10))
    ekf_P = (3.0 * lambda_ref) ** 2
    lam_hat = lambda_ref

    # Hawkes state init (K=1, scalar)
    R_buy = 0.0
    R_sell = 0.0

    E_prev = 1.0
    Edot_ema = 0.0

    # n_base is constant for a given parameter set
    n_base_val = _branching_ratio_2x2_scalar(
        alpha_self_buy, alpha_cross_buy,
        alpha_self_sell, alpha_cross_sell,
        beta_mle,
    )

    # First event: intensity at left-limit is just mu (no prior events)
    lam_b = mu_buy
    lam_s = mu_sell

    lam_total = max(lam_b, 0.0) + max(lam_s, 0.0)
    E_val = lam_total / mu_total

    lam_buy_out[0] = max(lam_b, 0.0)
    lam_sell_out[0] = max(lam_s, 0.0)
    E_out[0] = E_val
    Edot_out[0] = 0.0
    lam_hat_out[0] = lam_hat
    n_base_out[0] = n_base_val

    # Add first event to R AFTER outputting left-limit intensity
    if sides[0] == 1:
        R_buy = 1.0
    else:
        R_sell = 1.0

    E_prev = E_val

    for i in range(1, N):
        dt = t_sec[i] - t_sec[i - 1]

        # EKF update
        if dt > 0:
            ekf_P_pred = ekf_P + Q
            y = 1.0 / dt
            lam_hat_cur = np.exp(ekf_x)
            H = lam_hat_cur
            R_floor = c_R * lambda_ref
            R_val = max(lam_hat_cur, R_floor)
            R_noise = R_val * R_val
            S = H * ekf_P_pred * H + R_noise
            K_gain = ekf_P_pred * H / S
            ekf_x = ekf_x + K_gain * (y - lam_hat_cur)
            ekf_x = min(max(ekf_x, -20.0), 20.0)
            ekf_P = (1.0 - K_gain * H) * ekf_P_pred
            if ekf_P < 1e-30:
                ekf_P = 1e-30
            lam_hat = np.exp(ekf_x)

        # Adaptive beta + Hawkes decay
        ratio = max(lam_hat / max(lambda_ref, 1e-10), 0.1)
        beta_eff = beta_mle * ratio
        decay = np.exp(-beta_eff * dt)
        R_buy *= decay
        R_sell *= decay

        # Compute left-limit intensity lambda(t_i^-) BEFORE adding event
        lam_b = mu_buy + alpha_self_buy * R_buy + alpha_cross_buy * R_sell
        lam_s = mu_sell + alpha_self_sell * R_sell + alpha_cross_sell * R_buy

        if lam_b < 0.0:
            lam_b = 0.0
        if lam_s < 0.0:
            lam_s = 0.0

        lam_total = lam_b + lam_s
        E_val = lam_total / mu_total

        # Edot EMA
        dt_capped = min(dt, 1.0)
        if dt_capped < 1e-12:
            dt_capped = 1e-12
        raw_slope = (E_val - E_prev) / dt_capped
        Edot_ema = rho_E * Edot_ema + (1.0 - rho_E) * raw_slope

        lam_buy_out[i] = lam_b
        lam_sell_out[i] = lam_s
        E_out[i] = E_val
        Edot_out[i] = Edot_ema
        lam_hat_out[i] = lam_hat
        n_base_out[i] = n_base_val

        # Add event to R AFTER outputting left-limit intensity
        if sides[i] == 1:
            R_buy += 1.0
        else:
            R_sell += 1.0

        E_prev = E_val


# ── Fixed-beta replay (no adaptive scaling, for retrospective analysis) ──

@nb.njit(cache=True, fastmath=True)
def hawkes_replay_fixed_beta(
    t_sec: np.ndarray,         # float64, seconds from first event
    sides: np.ndarray,         # int8, +1/-1
    alpha_self_buy: float,
    alpha_cross_buy: float,
    alpha_self_sell: float,
    alpha_cross_sell: float,
    mu_buy: float,
    mu_sell: float,
    beta_mle: float,
    rho_E: float,
    # Pre-allocated output arrays (filled in-place)
    lam_buy_out: np.ndarray,
    lam_sell_out: np.ndarray,
    E_out: np.ndarray,
    Edot_out: np.ndarray,
) -> None:
    """Replay Hawkes with fixed beta (no EKF, no adaptive scaling).

    Used for Phase C E(t) trajectory generation where adaptive beta would
    cause lambda_hat divergence and suppress all excitation.
    """
    N = len(t_sec)
    if N == 0:
        return

    mu_total = mu_buy + mu_sell
    if mu_total < 1e-10:
        mu_total = 1e-10

    R_buy = 0.0
    R_sell = 0.0
    E_prev = 1.0
    Edot_ema = 0.0

    # First event
    lam_b = mu_buy
    lam_s = mu_sell
    lam_total = max(lam_b, 0.0) + max(lam_s, 0.0)
    E_val = lam_total / mu_total

    lam_buy_out[0] = max(lam_b, 0.0)
    lam_sell_out[0] = max(lam_s, 0.0)
    E_out[0] = E_val
    Edot_out[0] = 0.0

    if sides[0] == 1:
        R_buy = 1.0
    else:
        R_sell = 1.0

    E_prev = E_val

    for i in range(1, N):
        dt = t_sec[i] - t_sec[i - 1]

        # Fixed beta decay — no adaptive scaling
        if dt > 0:
            decay = np.exp(-beta_mle * dt)
            R_buy *= decay
            R_sell *= decay

        # Left-limit intensity
        lam_b = mu_buy + alpha_self_buy * R_buy + alpha_cross_buy * R_sell
        lam_s = mu_sell + alpha_self_sell * R_sell + alpha_cross_sell * R_buy

        if lam_b < 0.0:
            lam_b = 0.0
        if lam_s < 0.0:
            lam_s = 0.0

        lam_total = lam_b + lam_s
        E_val = lam_total / mu_total

        # Edot EMA
        dt_capped = min(dt, 1.0)
        if dt_capped < 1e-12:
            dt_capped = 1e-12
        raw_slope = (E_val - E_prev) / dt_capped
        Edot_ema = rho_E * Edot_ema + (1.0 - rho_E) * raw_slope

        lam_buy_out[i] = lam_b
        lam_sell_out[i] = lam_s
        E_out[i] = E_val
        Edot_out[i] = Edot_ema

        # Add event to R AFTER outputting left-limit intensity
        if sides[i] == 1:
            R_buy += 1.0
        else:
            R_sell += 1.0

        E_prev = E_val


# ── Numba-compiled log-likelihood ────────────────────────────────────────

@nb.njit(cache=True, fastmath=True)
def hawkes_log_likelihood(
    t_sec: np.ndarray,
    sides: np.ndarray,
    alpha_self_buy: float,
    alpha_cross_buy: float,
    alpha_self_sell: float,
    alpha_cross_sell: float,
    mu_buy: float,
    mu_sell: float,
    beta: float,
    T: float,
) -> float:
    """Compute log-likelihood of a bivariate Hawkes model with K=1.

    Single-pass implementation: accumulates both log-sum and compensator
    in one loop. The compensator for interval [t_{i-1}, t_i] uses R(t_{i-1})
    BEFORE the decay update.

    Beta is used directly (fixed during MLE). No adaptive scaling.

    Parameters
    ----------
    t_sec : event timestamps in seconds (float64, monotone increasing)
    sides : +1 buy, -1 sell (int8)
    alpha_self_buy, alpha_cross_buy : buy stream excitation weights (scalar)
    alpha_self_sell, alpha_cross_sell : sell stream excitation weights (scalar)
    mu_buy, mu_sell : baseline intensities
    beta : kernel decay rate (scalar)
    T : session end time (seconds, same clock as t_sec)
    """
    N = len(t_sec)
    if N < 2:
        return -np.inf

    mu_total = mu_buy + mu_sell
    if mu_total < 1e-10:
        return -np.inf

    R_buy = 0.0
    R_sell = 0.0

    log_sum = 0.0
    compensator = 0.0

    for i in range(N):
        if i > 0:
            dt = t_sec[i] - t_sec[i - 1]

            # COMPENSATOR for interval [t_{i-1}, t_i]:
            # Uses R BEFORE decay (the state active during this interval)
            bdt = beta * dt

            # Numerical guard: Taylor expansion for small beta*dt
            if bdt < 1e-8:
                factor = dt - 0.5 * beta * dt * dt
            else:
                factor = (1.0 - np.exp(-bdt)) / beta

            compensator += (alpha_self_buy * R_buy +
                            alpha_cross_buy * R_sell) * factor
            compensator += (alpha_self_sell * R_sell +
                            alpha_cross_sell * R_buy) * factor

            # Decay R AFTER compensator accumulation
            decay = np.exp(-bdt)
            R_buy *= decay
            R_sell *= decay

        # Compute intensity at left-limit lambda(t_i^-): uses R from
        # history only (events j < i), BEFORE adding current event.
        lam_b = mu_buy + alpha_self_buy * R_buy + alpha_cross_buy * R_sell
        lam_s = mu_sell + alpha_self_sell * R_sell + alpha_cross_sell * R_buy

        # Clamp to prevent log(0)
        if lam_b < 1e-300:
            lam_b = 1e-300
        if lam_s < 1e-300:
            lam_s = 1e-300

        # Log-sum: log(lambda_m(t_i^-)) for the stream that fired
        if sides[i] == 1:
            log_sum += np.log(lam_b)
        else:
            log_sum += np.log(lam_s)

        # Add event contribution to R AFTER intensity computation.
        if sides[i] == 1:
            R_buy += 1.0
        else:
            R_sell += 1.0

    # Final interval compensator: [t_N, T]
    dt_final = T - t_sec[N - 1]
    if dt_final > 0:
        bdt = beta * dt_final

        if bdt < 1e-8:
            factor = dt_final - 0.5 * beta * dt_final * dt_final
        else:
            factor = (1.0 - np.exp(-bdt)) / beta

        compensator += (alpha_self_buy * R_buy +
                        alpha_cross_buy * R_sell) * factor
        compensator += (alpha_self_sell * R_sell +
                        alpha_cross_sell * R_buy) * factor

    # Baseline compensator: mu*T for each stream
    compensator += mu_buy * T + mu_sell * T

    return log_sum - compensator


# ── Python wrapper class ────────────────────────────────────────────────

class HawkesEngine:
    """Bivariate clock-time Hawkes with K=1, free beta, Snyder intensity.

    Interface per spec:
      .update(t, side) -> HawkesState
      .update_edot(t_i, e_new) -> float
      .compute_excitation_ratio() -> float
      .branching_ratio_base() -> float
      .branching_ratio_eff() -> float
      .swap_params(...) — atomic update for online refitting
      .freeze() / .resume(halt_sec) / .reset()
    """

    def __init__(
        self,
        beta_mle: float,
        alpha_self_buy: float,
        alpha_cross_buy: float,
        mu_buy: float,
        mu_sell: float,
        lambda_ref: float,
        rho_E: float = 0.98,
        Q_scale: float = 0.01,
        c_R: float = 0.1,
        # Asymmetric alpha: if provided, use separate sell alpha
        alpha_self_sell: float | None = None,
        alpha_cross_sell: float | None = None,
    ):
        # Threading lock for atomic parameter swap
        self._param_lock = threading.Lock()

        self.beta_mle = beta_mle
        self.alpha_self_buy = alpha_self_buy
        self.alpha_cross_buy = alpha_cross_buy
        # If sell-side alpha not provided, mirror buy-side (symmetric)
        self.alpha_self_sell = (
            alpha_self_sell if alpha_self_sell is not None
            else alpha_self_buy
        )
        self.alpha_cross_sell = (
            alpha_cross_sell if alpha_cross_sell is not None
            else alpha_cross_buy
        )
        self.mu_buy = mu_buy
        self.mu_sell = mu_sell
        self.lambda_ref = lambda_ref
        self.rho_E = rho_E
        self._Q_scale = Q_scale
        self._c_R = c_R

        # EKF
        self.ekf = KalmanIntensityEstimator(lambda_ref, Q_scale, c_R)
        self.ekf_total = KalmanIntensityEstimator(lambda_ref, Q_scale, c_R)

        # State (K=1, scalar R)
        self._R_buy = 0.0
        self._R_sell = 0.0
        self._t_prev = 0.0
        self._E = 1.0
        self._Edot = 0.0
        self._lam_buy = mu_buy
        self._lam_sell = mu_sell
        self._beta_eff = beta_mle
        self._n_events = 0

        self._frozen = False

    def swap_params(
        self,
        alpha_self_buy: float,
        alpha_self_sell: float,
        mu_buy: float,
        mu_sell: float,
    ) -> None:
        """Atomically update all 4 fitted parameters. Thread-safe.

        Univariate model: no cross-excitation. Cross alphas stay 0.0.
        Beta is a fixed design constant set at init — not swapped.
        """
        with self._param_lock:
            self.alpha_self_buy = alpha_self_buy
            self.alpha_cross_buy = 0.0
            self.alpha_self_sell = alpha_self_sell
            self.alpha_cross_sell = 0.0
            self.mu_buy = mu_buy
            self.mu_sell = mu_sell

    def _read_params(self) -> tuple:
        """Read all 7 parameters atomically."""
        with self._param_lock:
            return (
                self.alpha_self_buy,
                self.alpha_cross_buy,
                self.alpha_self_sell,
                self.alpha_cross_sell,
                self.mu_buy,
                self.mu_sell,
                self.beta_mle,
            )

    def update(self, t: float, side: int) -> HawkesState:
        """Process a single event at time t with direction side (+1/-1)."""
        if self._frozen:
            return self._current_state()

        # Read params atomically
        (a_sb, a_cb, a_ss, a_cs,
         mu_b, mu_s, beta) = self._read_params()

        dt = t - self._t_prev if self._n_events > 0 else 0.0

        # Update both EKFs
        if dt > 0 and self._n_events > 0:
            self.ekf.update(dt)
            self.ekf_total.update(dt)

        # Hawkes update
        R_buy, R_sell, lam_b, lam_s, beta_eff = _hawkes_update_event(
            t, self._t_prev if self._n_events > 0 else t,
            side,
            self._R_buy, self._R_sell,
            a_sb, a_cb, a_ss, a_cs,
            mu_b, mu_s,
            beta,
            self.ekf.lambda_hat, self.lambda_ref,
        )

        self._R_buy = R_buy
        self._R_sell = R_sell
        self._lam_buy = lam_b
        self._lam_sell = lam_s
        self._beta_eff = beta_eff

        # Excitation ratio
        mu_total = mu_b + mu_s
        E_new = (lam_b + lam_s) / max(mu_total, 1e-10)

        # Edot
        if self._n_events > 0 and dt > 0:
            self._Edot = _compute_edot(
                E_new, self._E, dt, self._Edot, self.rho_E,
            )

        self._E = E_new
        self._t_prev = t
        self._n_events += 1

        return self._current_state()

    def _current_state(self) -> HawkesState:
        (a_sb, a_cb, a_ss, a_cs,
         mu_b, mu_s, beta) = self._read_params()
        return HawkesState(
            lambda_buy=self._lam_buy,
            lambda_sell=self._lam_sell,
            lambda_total=self._lam_buy + self._lam_sell,
            E=self._E,
            Edot=self._Edot,
            n_base=self.branching_ratio_base(),
            n_eff=self.branching_ratio_eff(),
            lambda_hat=self.ekf.lambda_hat,
            R_buy=self._R_buy,
            R_sell=self._R_sell,
        )

    def update_edot(self, t_i: float, e_new: float) -> float:
        """Manually update Edot with a specific E value (for external use)."""
        dt = t_i - self._t_prev
        self._Edot = _compute_edot(
            e_new, self._E, dt, self._Edot, self.rho_E,
        )
        return self._Edot

    def compute_excitation_ratio(self) -> float:
        """Current E(t)."""
        return self._E

    def branching_ratio_base(self) -> float:
        """Spectral radius of 2x2 branching matrix using beta_mle (regime gate)."""
        (a_sb, a_cb, a_ss, a_cs,
         _, _, beta) = self._read_params()
        return float(_branching_ratio_2x2_scalar(
            a_sb, a_cb, a_ss, a_cs, beta,
        ))

    def branching_ratio_eff(self) -> float:
        """Spectral radius using beta_eff (diagnostics only)."""
        (a_sb, a_cb, a_ss, a_cs,
         _, _, _) = self._read_params()
        return float(_branching_ratio_2x2_scalar(
            a_sb, a_cb, a_ss, a_cs, self._beta_eff,
        ))

    def freeze(self) -> None:
        """Freeze state during LULD halt."""
        self._frozen = True

    def resume(self, halt_duration_sec: float) -> None:
        """Resume after halt. Inflate EKF uncertainty."""
        self._frozen = False
        self.ekf.on_halt_resume(halt_duration_sec)
        self.ekf_total.on_halt_resume(halt_duration_sec)

    def reset(self) -> None:
        """Reset all state."""
        self._R_buy = 0.0
        self._R_sell = 0.0
        self._t_prev = 0.0
        self._E = 1.0
        self._Edot = 0.0
        self._lam_buy = self.mu_buy
        self._lam_sell = self.mu_sell
        self._beta_eff = self.beta_mle
        self._n_events = 0
        self._frozen = False
        self.ekf.reset()
        self.ekf_total.reset()

    def replay(
        self, t_sec: np.ndarray, sides: np.ndarray,
    ) -> dict[str, np.ndarray]:
        """Full vectorized replay over event sequence.

        Returns dict of output arrays (pre-allocated, filled by Numba).
        """
        (a_sb, a_cb, a_ss, a_cs,
         mu_b, mu_s, beta) = self._read_params()

        N = len(t_sec)
        out = {
            "lam_buy": np.empty(N, dtype=np.float64),
            "lam_sell": np.empty(N, dtype=np.float64),
            "E": np.empty(N, dtype=np.float64),
            "Edot": np.empty(N, dtype=np.float64),
            "lam_hat": np.empty(N, dtype=np.float64),
            "n_base": np.empty(N, dtype=np.float64),
        }
        hawkes_replay(
            t_sec, sides,
            a_sb, a_cb, a_ss, a_cs,
            mu_b, mu_s,
            beta, self.lambda_ref, self.rho_E,
            self._Q_scale, self._c_R,
            out["lam_buy"], out["lam_sell"],
            out["E"], out["Edot"],
            out["lam_hat"], out["n_base"],
        )
        return out

    def log_likelihood(
        self, t_sec: np.ndarray, sides: np.ndarray,
        T: float | None = None,
    ) -> float:
        """Compute log-likelihood on an event sequence.

        Parameters
        ----------
        T : session end time (seconds). If None, uses t_sec[-1].
        """
        (a_sb, a_cb, a_ss, a_cs,
         mu_b, mu_s, beta) = self._read_params()

        if T is None:
            T = float(t_sec[-1])
        return float(hawkes_log_likelihood(
            t_sec, sides,
            a_sb, a_cb, a_ss, a_cs,
            mu_b, mu_s,
            beta, T,
        ))
