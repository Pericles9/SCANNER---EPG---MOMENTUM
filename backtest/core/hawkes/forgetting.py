"""
Exponential forgetting MLE for univariate Hawkes parameters with K=1, fixed beta.

Beta is a fixed design constant (not fitted by MLE). It is set to match
the regime-detection timescale (~7 second half-life), not the next-arrival
prediction timescale (~0.5ms). MLE fits only the 4 remaining parameters.

Cross-excitation dropped in iter 7: at beta=0.1 (7s memory), Lee-Ready
classification noise corrupts buy/sell direction at this timescale. The
iter 6 full run confirmed alpha_cross collapsed to 1e-8 across all 2162
events. The model is explicitly univariate now.

Parameter vector (optimized): [alpha_self_buy, alpha_self_sell,
                               mu_buy, mu_sell]

Beta is passed as a fixed constant to all fitting functions.

Dependencies: core/hawkes/engine.py (for LL computation).
"""
from __future__ import annotations

from dataclasses import dataclass

import numba as nb
import numpy as np
from scipy.optimize import minimize

from core.hawkes.engine import _branching_ratio_2x2_scalar


@dataclass
class HawkesParams:
    """Hawkes parameters (K=1, univariate, fixed beta).

    Cross-excitation terms are zero by design — not fitted.
    Beta is a design constant, not fitted.
    """
    alpha_buy_self: float
    alpha_buy_cross: float     # always 0.0 — kept for downstream compat
    alpha_sell_self: float
    alpha_sell_cross: float    # always 0.0 — kept for downstream compat
    mu_buy: float
    mu_sell: float
    beta: float                # stored for downstream use, but NOT fitted
    log_likelihood: float
    n_events_used: int


# ── Numba-compiled weighted log-likelihood ──────────────────────────────

@nb.njit(cache=True, fastmath=True)
def _weighted_log_likelihood(
    t_sec: np.ndarray,
    sides: np.ndarray,
    alpha_self_buy: float,
    alpha_self_sell: float,
    mu_buy: float,
    mu_sell: float,
    beta: float,
    T: float,
    rho: float,
) -> float:
    """Weighted univariate Hawkes log-likelihood with exponential forgetting.

    K=1, fixed beta, no cross-excitation. Single-pass implementation.
    Weight for event i is rho^(N-1-i).
    """
    N = len(t_sec)
    if N < 2:
        return -1e18

    mu_total = mu_buy + mu_sell
    if mu_total < 1e-10:
        return -1e18

    R_buy = 0.0
    R_sell = 0.0

    log_sum = 0.0
    compensator = 0.0

    for i in range(N):
        weight = rho ** (N - 1 - i)

        if i > 0:
            dt = t_sec[i] - t_sec[i - 1]
            bdt = beta * dt

            if bdt < 1e-8:
                factor = dt - 0.5 * beta * dt * dt
            else:
                factor = (1.0 - np.exp(-bdt)) / beta

            compensator += weight * (
                alpha_self_buy * R_buy * factor +
                alpha_self_sell * R_sell * factor
            )

            # Background rate compensator
            compensator += weight * mu_total * dt

            # Decay R AFTER compensator accumulation
            decay = np.exp(-bdt)
            R_buy *= decay
            R_sell *= decay

        # Intensity at left-limit: only self-excitation
        lam_b = mu_buy + alpha_self_buy * R_buy
        lam_s = mu_sell + alpha_self_sell * R_sell

        if lam_b < 1e-300:
            lam_b = 1e-300
        if lam_s < 1e-300:
            lam_s = 1e-300

        if sides[i] == 1:
            log_sum += weight * np.log(lam_b)
        else:
            log_sum += weight * np.log(lam_s)

        if sides[i] == 1:
            R_buy += 1.0
        else:
            R_sell += 1.0

    # Final interval compensator: [t_N, T]
    dt_final = T - t_sec[N - 1]
    if dt_final > 0:
        weight_final = 1.0
        bdt = beta * dt_final

        if bdt < 1e-8:
            factor = dt_final - 0.5 * beta * dt_final * dt_final
        else:
            factor = (1.0 - np.exp(-bdt)) / beta

        compensator += weight_final * (
            alpha_self_buy * R_buy * factor +
            alpha_self_sell * R_sell * factor
        )
        compensator += weight_final * mu_total * dt_final

    return log_sum - compensator


# ── Numba-compiled LL + analytical gradient (single pass) ─────────────

@nb.njit(cache=True, fastmath=True)
def _weighted_ll_and_grad(
    t_sec: np.ndarray,
    sides: np.ndarray,
    alpha_self_buy: float,
    alpha_self_sell: float,
    mu_buy: float,
    mu_sell: float,
    beta: float,
    T: float,
    rho: float,
):
    """Compute weighted univariate Hawkes LL AND 4-element gradient.

    Gradient w.r.t. [a_sb, a_ss, mu_b, mu_s].

    No cross-excitation, no beta gradient.
    Returns (ll, grad) where grad is shape (4,) float64.
    """
    N = len(t_sec)
    grad = np.zeros(4, dtype=np.float64)

    if N < 2:
        return -1e18, grad

    mu_total = mu_buy + mu_sell
    if mu_total < 1e-10:
        return -1e18, grad

    R_b = 0.0
    R_s = 0.0

    log_sum = 0.0
    compensator = 0.0

    for i in range(N):
        weight = rho ** (N - 1 - i)

        if i > 0:
            dt = t_sec[i] - t_sec[i - 1]
            bdt = beta * dt

            R_b_pre = R_b
            R_s_pre = R_s

            decay = np.exp(-bdt)
            if bdt < 1e-8:
                factor = dt - 0.5 * beta * dt * dt
            else:
                factor = (1.0 - decay) / beta

            # Compensator (univariate — no cross terms)
            exc_total = alpha_self_buy * R_b_pre + alpha_self_sell * R_s_pre
            compensator += weight * (exc_total * factor + mu_total * dt)

            # Compensator gradients
            wf = weight * factor
            grad[0] -= wf * R_b_pre            # d/d(a_sb)
            grad[1] -= wf * R_s_pre            # d/d(a_ss)
            w_dt = weight * dt
            grad[2] -= w_dt                    # d/d(mu_b)
            grad[3] -= w_dt                    # d/d(mu_s)

            # Decay R
            R_b = R_b_pre * decay
            R_s = R_s_pre * decay

        # Intensity at left-limit (univariate — no cross terms)
        lam_b = mu_buy + alpha_self_buy * R_b
        lam_s = mu_sell + alpha_self_sell * R_s

        if lam_b < 1e-300:
            lam_b = 1e-300
        if lam_s < 1e-300:
            lam_s = 1e-300

        # Log-sum and its gradient
        if sides[i] == 1:
            log_sum += weight * np.log(lam_b)
            inv_lam = weight / lam_b
            grad[0] += inv_lam * R_b              # d/d(a_sb)
            grad[2] += inv_lam                    # d/d(mu_b)
        else:
            log_sum += weight * np.log(lam_s)
            inv_lam = weight / lam_s
            grad[1] += inv_lam * R_s              # d/d(a_ss)
            grad[3] += inv_lam                    # d/d(mu_s)

        # Add event to R
        if sides[i] == 1:
            R_b += 1.0
        else:
            R_s += 1.0

    # Final interval compensator [t_N, T]
    dt_f = T - t_sec[N - 1]
    if dt_f > 0:
        bdt_f = beta * dt_f
        decay_f = np.exp(-bdt_f)

        if bdt_f < 1e-8:
            factor_f = dt_f - 0.5 * beta * dt_f * dt_f
        else:
            factor_f = (1.0 - decay_f) / beta

        exc_total_f = alpha_self_buy * R_b + alpha_self_sell * R_s
        compensator += exc_total_f * factor_f + mu_total * dt_f

        # Final interval gradients (weight = 1.0 implicitly)
        grad[0] -= R_b * factor_f
        grad[1] -= R_s * factor_f
        grad[2] -= dt_f
        grad[3] -= dt_f

    ll = log_sum - compensator
    return ll, grad


# ── Branching penalty and its gradient ────────────────────────────────

def _branching_penalty_and_grad(
    x4: np.ndarray, beta_fixed: float,
) -> tuple[float, np.ndarray]:
    """Two-layer branching-ratio penalty + analytical 4-gradient.

    Univariate: n_base = max(a_sb, a_ss) / beta_fixed.
    No cross terms, no spectral radius needed.
    """
    a_sb, a_ss = x4[0], x4[1]
    inv_b = 1.0 / beta_fixed

    n_buy = a_sb * inv_b
    n_sell = a_ss * inv_b
    n_base = max(n_buy, n_sell)

    penalty = 0.0
    dp_dn = 0.0
    if n_base > 0.85:
        penalty += 5000.0 * (n_base - 0.85) ** 2
        dp_dn += 10000.0 * (n_base - 0.85)
    if n_base > 0.90:
        penalty += 50000.0 * (n_base - 0.90) ** 2
        dp_dn += 100000.0 * (n_base - 0.90)

    grad = np.zeros(4, dtype=np.float64)
    if dp_dn == 0.0:
        return penalty, grad

    # Gradient flows through whichever alpha is larger
    if n_buy >= n_sell:
        grad[0] = dp_dn * inv_b     # d/d(a_sb)
    else:
        grad[1] = dp_dn * inv_b     # d/d(a_ss)

    return penalty, grad


# ── MLE fitting ─────────────────────────────────────────────────────────

# Parameter order: [a_sb, a_ss, mu_b, mu_s]
# Beta is fixed externally — not part of the optimizer parameter vector.

def fit_hawkes_forgetting(
    t_sec: np.ndarray,
    sides: np.ndarray,
    rho: float,
    lambda_ref: float,
    T: float | None = None,
    init_params: np.ndarray | None = None,
    symmetric: bool = False,
    n_restarts: int = 5,
    beta_fixed: float = 0.1,
) -> HawkesParams:
    """Fit univariate Hawkes via MLE with exponential forgetting, K=1, fixed beta.

    Beta is a fixed design constant set to match the regime-detection
    timescale. MLE optimizes only 4 parameters (2 self-alphas + 2 mus).

    Parameters
    ----------
    t_sec : seconds from first event (float64)
    sides : +1 buy, -1 sell (int8)
    rho : forgetting rate (event-count indexed)
    lambda_ref : baseline arrival rate
    T : session end time in seconds. If None, uses t_sec[-1].
    symmetric : if True, constrain alpha_buy_self = alpha_sell_self
    n_restarts : number of random restarts to avoid local optima
    beta_fixed : fixed decay rate (design constant, not fitted)
    """
    sides_f = sides.astype(np.int8)

    if T is None:
        T = float(t_sec[-1])

    # Alpha upper bound: n_base = alpha/beta < 0.90 → alpha < 0.90 * beta
    alpha_upper = 0.90 * beta_fixed
    mu_lower = max(lambda_ref * 0.05, 0.1)

    if symmetric:
        bounds = (
            [(1e-8, alpha_upper)] * 1 +   # alpha_self (shared)
            [(mu_lower, 1000.0)] * 2       # mu_buy, mu_sell
        )
    else:
        bounds = (
            [(1e-8, alpha_upper)] * 2 +   # alpha_self_buy, alpha_self_sell
            [(mu_lower, 1000.0)] * 2       # mu_buy, mu_sell
        )

    best_ll = -np.inf
    best_x = None

    def neg_ll_and_grad_4(x4):
        """Negative LL + penalty with analytical gradient (4 params)."""
        ll, g_ll = _weighted_ll_and_grad(
            t_sec, sides_f,
            x4[0], x4[1],
            x4[2], x4[3], beta_fixed,
            T, rho,
        )
        penalty, g_pen = _branching_penalty_and_grad(x4, beta_fixed)
        return -ll + penalty, -g_ll + g_pen

    def neg_ll_and_grad_3(x3):
        """Symmetric wrapper: 3 params -> 4 with gradient projection."""
        x4 = np.array([x3[0], x3[0], x3[1], x3[2]])
        val, g4 = neg_ll_and_grad_4(x4)
        g3 = np.array([
            g4[0] + g4[1],   # d/d(a_self) = d/d(a_sb) + d/d(a_ss)
            g4[2], g4[3],
        ])
        return val, g3

    obj_func = neg_ll_and_grad_3 if symmetric else neg_ll_and_grad_4

    for restart in range(n_restarts):
        if init_params is not None and restart == 0:
            if symmetric and len(init_params) == 4:
                x0 = np.array([init_params[0], init_params[2], init_params[3]])
            elif symmetric:
                x0 = init_params.copy()
            else:
                if len(init_params) == 4:
                    x0 = init_params.copy()
                else:
                    # Handle legacy 6-element init by stripping cross terms
                    x0 = np.array([init_params[0], init_params[2],
                                   init_params[4], init_params[5]])
        else:
            rng = np.random.RandomState(42 + restart)
            n_alpha = 1 if symmetric else 2
            x0 = np.concatenate([
                rng.uniform(0.01, min(alpha_upper, 1.0), n_alpha),
                rng.uniform(1.0, max(lambda_ref * 0.5, 2.0), 2),
            ])

        try:
            result = minimize(
                obj_func, x0,
                method="L-BFGS-B",
                jac=True,
                bounds=bounds,
                options={"maxiter": 500, "ftol": 1e-8},
            )
            # Recover full 4-element vector
            if symmetric:
                x4 = np.array([result.x[0], result.x[0],
                               result.x[1], result.x[2]])
            else:
                x4 = result.x

            raw_ll = _weighted_log_likelihood(
                t_sec, sides_f,
                x4[0], x4[1],
                x4[2], x4[3], beta_fixed,
                T, rho,
            )
            if raw_ll > best_ll:
                best_ll = raw_ll
                best_x = x4.copy()
        except Exception:
            continue

    if best_x is None:
        best_x = np.array([
            0.01, 0.01,
            max(lambda_ref * 0.3, 1.0),
            max(lambda_ref * 0.3, 1.0),
        ])
        best_ll = -np.inf

    return HawkesParams(
        alpha_buy_self=best_x[0],
        alpha_buy_cross=0.0,
        alpha_sell_self=best_x[1],
        alpha_sell_cross=0.0,
        mu_buy=best_x[2],
        mu_sell=best_x[3],
        beta=beta_fixed,
        log_likelihood=best_ll,
        n_events_used=len(t_sec),
    )


def fit_online(
    t_sec: np.ndarray,
    sides: np.ndarray,
    rho: float,
    lambda_ref: float,
    prev_params: HawkesParams,
    T: float | None = None,
    n_restarts: int = 5,
    beta_fixed: float = 0.1,
) -> HawkesParams:
    """Online refitting entry point — uses prev_params as warm start.

    Parameters
    ----------
    t_sec : event buffer timestamps (float64)
    sides : event buffer sides (int8)
    rho : forgetting rate
    lambda_ref : baseline arrival rate
    prev_params : previous fit result (used as warm start)
    T : session end time. If None, uses t_sec[-1].
    n_restarts : total restarts including the warm start
    beta_fixed : fixed decay rate (design constant, not fitted)
    """
    # Build 4-element warm start (self-alphas + mus)
    warm_start = np.array([
        prev_params.alpha_buy_self,
        prev_params.alpha_sell_self,
        prev_params.mu_buy,
        prev_params.mu_sell,
    ])

    return fit_hawkes_forgetting(
        t_sec=t_sec,
        sides=sides,
        rho=rho,
        lambda_ref=lambda_ref,
        T=T,
        init_params=warm_start,
        symmetric=False,
        n_restarts=n_restarts,
        beta_fixed=beta_fixed,
    )
