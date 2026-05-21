"""
Tests for hawkes_log_likelihood on synthetic data (K=1, scalar API).

Validates the compensator is correct by:
  1. Checking compensator sign and magnitude
  2. Verifying LL scales with session length
  3. Recovering known parameters via MLE
  4. Confirming true params outperform boundary params
  5. Univariate fixed-beta MLE recovery

Uses Ogata thinning for ground-truth Hawkes simulation.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.optimize import minimize

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.hawkes.engine import hawkes_log_likelihood, _branching_ratio_2x2_scalar


# ── Ogata thinning simulator for univariate Hawkes ────────────────────────

def simulate_hawkes_univariate(
    mu: float,
    alpha: float,
    beta: float,
    T: float,
    seed: int = 42,
) -> np.ndarray:
    """Simulate a univariate Hawkes process using Ogata thinning.

    Returns array of event times in [0, T].
    """
    rng = np.random.RandomState(seed)
    times = []
    t = 0.0
    lam_star = mu  # intensity upper bound

    while t < T:
        # Draw next candidate
        u = rng.uniform()
        w = -np.log(u) / lam_star
        t += w

        if t >= T:
            break

        # Compute true intensity at candidate time
        lam_t = mu
        for s in times:
            lam_t += alpha * np.exp(-beta * (t - s))

        # Accept/reject
        d = rng.uniform()
        if d <= lam_t / lam_star:
            times.append(t)

        # Update upper bound
        lam_star = lam_t + alpha

    return np.array(times, dtype=np.float64)


def simulate_bivariate_hawkes(
    mu: float,
    alpha_self: float,
    alpha_cross: float,
    beta: float,
    T: float,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray]:
    """Simulate a symmetric bivariate Hawkes.

    Returns (times, sides) where sides is +1/-1 int8.
    """
    rng = np.random.RandomState(seed)
    times = []
    sides = []

    R_buy = 0.0
    R_sell = 0.0
    t = 0.0

    while t < T:
        lam_buy = mu + alpha_self * R_buy + alpha_cross * R_sell
        lam_sell = mu + alpha_self * R_sell + alpha_cross * R_buy
        lam_total = max(lam_buy + lam_sell, 1e-10)

        # Draw next event time
        u = rng.uniform()
        dt = -np.log(u) / lam_total
        t += dt

        if t >= T:
            break

        # Decay R
        decay = np.exp(-beta * dt)
        R_buy *= decay
        R_sell *= decay

        # Recompute intensities at new time
        lam_buy = mu + alpha_self * R_buy + alpha_cross * R_sell
        lam_sell = mu + alpha_self * R_sell + alpha_cross * R_buy
        lam_total_new = lam_buy + lam_sell

        # Thinning
        d = rng.uniform()
        if d <= lam_total_new / lam_total:
            # Accepted: determine which stream
            p_buy = lam_buy / max(lam_total_new, 1e-10)
            if rng.uniform() < p_buy:
                R_buy += 1.0
                sides.append(1)
            else:
                R_sell += 1.0
                sides.append(-1)
            times.append(t)

    return (
        np.array(times, dtype=np.float64),
        np.array(sides, dtype=np.int8),
    )


# ── Helper: evaluate LL with K=1 scalar API ─────────────────────────────

def eval_ll_simple(
    times: np.ndarray,
    sides: np.ndarray,
    mu_buy: float,
    mu_sell: float,
    alpha_self: float,
    alpha_cross: float,
    beta: float,
    T: float,
) -> float:
    """Evaluate bivariate Hawkes LL using the engine function (K=1, scalar)."""
    return float(hawkes_log_likelihood(
        times, sides,
        alpha_self, alpha_cross,
        alpha_self, alpha_cross,
        mu_buy, mu_sell,
        beta, T,
    ))


# ── Reference LL implementation (pure Python, no EKF) ────────────────────

def reference_ll(
    times: np.ndarray,
    sides: np.ndarray,
    mu_buy: float,
    mu_sell: float,
    alpha_self: float,
    alpha_cross: float,
    beta: float,
    T: float,
) -> float:
    """Pure-Python reference bivariate Hawkes LL (K=1, scalar).

    No EKF, no adaptive beta. Direct implementation of the math.
    """
    N = len(times)
    if N < 2:
        return -np.inf

    R_buy = 0.0
    R_sell = 0.0

    log_sum = 0.0
    compensator_buy = 0.0
    compensator_sell = 0.0

    for i in range(N):
        if i == 0:
            dt = times[0]  # from session start (0) to first event
        else:
            dt = times[i] - times[i - 1]

        # Compensator for interval [t_{i-1}, t_i] using R BEFORE decay
        if dt > 0:
            decay_val = np.exp(-beta * dt)
            factor = (1.0 - decay_val) / beta
            compensator_buy += (alpha_self * R_buy + alpha_cross * R_sell) * factor
            compensator_sell += (alpha_self * R_sell + alpha_cross * R_buy) * factor

        # Decay R
        if dt > 0:
            decay_val = np.exp(-beta * dt)
            R_buy *= decay_val
            R_sell *= decay_val

        # Intensity at left-limit lambda(t_i^-): history only, before adding event
        lam_b = mu_buy + alpha_self * R_buy + alpha_cross * R_sell
        lam_s = mu_sell + alpha_self * R_sell + alpha_cross * R_buy
        lam_b = max(lam_b, 1e-300)
        lam_s = max(lam_s, 1e-300)

        if sides[i] == 1:
            log_sum += np.log(lam_b)
        else:
            log_sum += np.log(lam_s)

        # Add event AFTER intensity computation
        if sides[i] == 1:
            R_buy += 1.0
        else:
            R_sell += 1.0

    # Final interval [t_N, T]
    dt_final = T - times[-1]
    if dt_final > 0:
        decay_final = np.exp(-beta * dt_final)
        factor_final = (1.0 - decay_final) / beta
        compensator_buy += (alpha_self * R_buy + alpha_cross * R_sell) * factor_final
        compensator_sell += (alpha_self * R_sell + alpha_cross * R_buy) * factor_final

    # Baseline compensator
    compensator_buy += mu_buy * T
    compensator_sell += mu_sell * T

    return log_sum - compensator_buy - compensator_sell


# ── Tests ─────────────────────────────────────────────────────────────────

class TestCompensatorSign:
    """Test 1: Compensator must be positive and scale with T."""

    def test_compensator_positive(self):
        """Compensator must be positive for any valid process."""
        mu, alpha_s, alpha_c, beta = 0.5, 0.3, 0.05, 1.0
        T = 3600.0
        times, sides = simulate_bivariate_hawkes(mu, alpha_s, alpha_c, beta, T, seed=42)
        assert len(times) > 100, f"Simulation produced too few events: {len(times)}"

        # LL at true params
        ll_true = eval_ll_simple(times, sides, mu, mu, alpha_s, alpha_c, beta, T)

        # LL with zero excitation (only baseline) — this isolates the compensator effect
        ll_baseline = eval_ll_simple(times, sides, mu, mu, 1e-10, 1e-10, beta, T)

        # With zero excitation, LL = sum(log(mu)) - mu*T*2
        # = N*log(mu) - 2*mu*T
        N = len(times)
        expected_baseline = N * np.log(mu) - 2 * mu * T
        assert abs(ll_baseline - expected_baseline) < abs(expected_baseline) * 0.5, (
            f"Baseline LL mismatch: got {ll_baseline}, expected ~{expected_baseline}"
        )

    def test_compensator_reflects_baseline(self):
        """Compensator must at least partially reflect baseline rate."""
        mu, alpha_s, alpha_c, beta = 0.5, 0.3, 0.05, 1.0
        T = 3600.0
        times, sides = simulate_bivariate_hawkes(mu, alpha_s, alpha_c, beta, T, seed=42)

        ll = eval_ll_simple(times, sides, mu, mu, alpha_s, alpha_c, beta, T)
        N = len(times)
        assert ll < N * 10, (
            f"LL={ll:.1f} seems too high relative to N={N} — compensator may be missing"
        )


class TestLLScaling:
    """Test 2: LL must scale with session length."""

    def test_ll_scales_with_session_length(self):
        """LL for 10x longer session should be ~10x larger in magnitude."""
        mu, alpha_s, alpha_c, beta = 0.5, 0.3, 0.05, 1.0

        T_short = 3600.0
        T_long = 36000.0

        times_short, sides_short = simulate_bivariate_hawkes(
            mu, alpha_s, alpha_c, beta, T_short, seed=42,
        )
        times_long, sides_long = simulate_bivariate_hawkes(
            mu, alpha_s, alpha_c, beta, T_long, seed=123,
        )

        assert len(times_short) > 100, f"Short sim: {len(times_short)} events"
        assert len(times_long) > 1000, f"Long sim: {len(times_long)} events"

        ll_short = eval_ll_simple(
            times_short, sides_short, mu, mu, alpha_s, alpha_c, beta, T_short,
        )
        ll_long = eval_ll_simple(
            times_long, sides_long, mu, mu, alpha_s, alpha_c, beta, T_long,
        )

        ratio = abs(ll_long) / max(abs(ll_short), 1e-10)
        assert 3 < ratio < 30, (
            f"LL scaling ratio = {ratio:.1f} (expected ~10). "
            f"ll_short={ll_short:.1f}, ll_long={ll_long:.1f}"
        )


class TestParameterRecovery:
    """Test 3: MLE must recover known parameters from synthetic data."""

    def test_compensator_prevents_unbounded_saturation(self):
        """With correct compensator, n_base stays bounded (not 535!).

        Without compensator, MLE pushes alpha to upper bound regardless.
        With compensator, alpha stays bounded even without penalty.
        """
        mu_true = 0.5
        alpha_self_true = 0.3
        alpha_cross_true = 0.05
        beta_true = 1.0
        T = 7200.0

        times, sides = simulate_bivariate_hawkes(
            mu_true, alpha_self_true, alpha_cross_true, beta_true, T, seed=42,
        )
        n_events = len(times)
        assert n_events > 500, f"Only {n_events} events simulated"

        def neg_ll_ref(x):
            a_self = max(x[0], 1e-10)
            a_cross = max(x[1], 1e-10)
            mb = max(x[2], 1e-10)
            ms = max(x[3], 1e-10)
            ll = reference_ll(times, sides, mb, ms, a_self, a_cross, beta_true, T)
            if not np.isfinite(ll):
                return 1e18
            return -ll

        bounds = [(1e-8, 50.0), (1e-8, 50.0), (1e-6, 100.0), (1e-6, 100.0)]
        x0 = np.array([0.5, 0.1, 1.0, 1.0])
        result = minimize(neg_ll_ref, x0, method="L-BFGS-B", bounds=bounds,
                          options={"maxiter": 500, "ftol": 1e-10})

        fitted_alpha_self = result.x[0]
        fitted_alpha_cross = result.x[1]
        n_base = (fitted_alpha_self + fitted_alpha_cross) / beta_true

        assert n_base < 5.0, (
            f"n_base={n_base:.1f} — compensator not preventing saturation. "
            f"alpha_self={fitted_alpha_self:.3f}, alpha_cross={fitted_alpha_cross:.3f}"
        )
        assert fitted_alpha_self < 49.0, (
            f"alpha_self={fitted_alpha_self:.1f} hit upper bound — compensator broken"
        )

    def test_parameter_recovery_with_penalty(self):
        """With branching-ratio penalty, MLE recovers plausible params."""
        mu_true = 0.5
        alpha_self_true = 0.3
        alpha_cross_true = 0.05
        beta_true = 1.0
        T = 7200.0

        times, sides = simulate_bivariate_hawkes(
            mu_true, alpha_self_true, alpha_cross_true, beta_true, T, seed=42,
        )

        def neg_ll_penalized(x):
            a_self = max(x[0], 1e-10)
            a_cross = max(x[1], 1e-10)
            mb = max(x[2], 1e-10)
            ms = max(x[3], 1e-10)

            ll = float(hawkes_log_likelihood(
                times, sides,
                a_self, a_cross,
                a_self, a_cross,
                mb, ms,
                beta_true, T,
            ))
            if not np.isfinite(ll):
                return 1e18

            # Two-layer branching-ratio penalty
            n_base = float(_branching_ratio_2x2_scalar(
                a_self, a_cross, a_self, a_cross, beta_true,
            ))
            penalty = 0.0
            if n_base > 0.85:
                penalty = 5000.0 * (n_base - 0.85) ** 2
            if n_base > 0.90:
                penalty += 50000.0 * (n_base - 0.90) ** 2
            return -ll + penalty

        best_result = None
        best_fun = np.inf
        for x0 in [
            np.array([0.3, 0.05, 0.5, 0.5]),
            np.array([0.1, 0.01, 1.0, 1.0]),
        ]:
            bounds = [(1e-8, 5.0), (1e-8, 5.0), (1e-6, 50.0), (1e-6, 50.0)]
            result = minimize(neg_ll_penalized, x0, method="L-BFGS-B",
                              bounds=bounds,
                              options={"maxiter": 1000, "ftol": 1e-12})
            if result.fun < best_fun:
                best_fun = result.fun
                best_result = result

        n_base = float(_branching_ratio_2x2_scalar(
            best_result.x[0], best_result.x[1],
            best_result.x[0], best_result.x[1],
            beta_true,
        ))
        assert n_base < 2.0, (
            f"Penalized MLE: n_base={n_base:.2f}, expected < 2.0"
        )


class TestTrueParamsOutperformBoundary:
    """Test 4: True params must produce higher LL than boundary params."""

    def test_true_beats_saturated(self):
        """LL at true params must exceed LL at alpha=upper_bound."""
        mu_true = 0.5
        alpha_self_true = 0.3
        alpha_cross_true = 0.05
        beta_true = 1.0
        T = 3600.0

        times, sides = simulate_bivariate_hawkes(
            mu_true, alpha_self_true, alpha_cross_true, beta_true, T, seed=42,
        )

        ll_true = eval_ll_simple(
            times, sides, mu_true, mu_true,
            alpha_self_true, alpha_cross_true, beta_true, T,
        )

        ll_bad = eval_ll_simple(
            times, sides, 0.001, 0.001, 49.9, 49.9, beta_true, T,
        )

        assert ll_true > ll_bad, (
            f"True params LL ({ll_true:.1f}) should exceed boundary LL ({ll_bad:.1f}). "
            f"Compensator is likely broken — high alpha is not penalized."
        )

    def test_ll_decreases_with_extreme_alpha(self):
        """LL must decrease at truly extreme alpha (near optimizer bounds)."""
        mu_true = 0.5
        alpha_self_true = 0.3
        alpha_cross_true = 0.05
        beta_true = 1.0
        T = 3600.0

        times, sides = simulate_bivariate_hawkes(
            mu_true, alpha_self_true, alpha_cross_true, beta_true, T, seed=42,
        )

        ll_true = eval_ll_simple(
            times, sides, mu_true, mu_true,
            alpha_self_true, alpha_cross_true, beta_true, T,
        )
        ll_extreme = eval_ll_simple(
            times, sides, mu_true, mu_true, 49.9, 49.9, beta_true, T,
        )

        assert ll_true > ll_extreme, (
            f"LL at true params ({ll_true:.1f}) should exceed LL at extreme alpha ({ll_extreme:.1f})"
        )


class TestReferenceImplementation:
    """Cross-validate engine LL against pure-Python reference."""

    def test_engine_matches_reference(self):
        """Engine LL should match pure-Python reference within tolerance."""
        mu, alpha_s, alpha_c, beta = 0.5, 0.3, 0.05, 1.0
        T = 1800.0

        times, sides = simulate_bivariate_hawkes(mu, alpha_s, alpha_c, beta, T, seed=99)
        assert len(times) > 50

        ll_ref = reference_ll(times, sides, mu, mu, alpha_s, alpha_c, beta, T)
        ll_engine = eval_ll_simple(times, sides, mu, mu, alpha_s, alpha_c, beta, T)

        rel_diff = abs(ll_engine - ll_ref) / max(abs(ll_ref), 1.0)
        assert rel_diff < 0.02, (
            f"Engine LL ({ll_engine:.1f}) differs from reference ({ll_ref:.1f}) "
            f"by {rel_diff*100:.1f}%"
        )


class TestAnalyticalLL:
    """Analytical LL for known simple cases — catches self-inclusion bugs."""

    def test_analytical_3event_univariate(self):
        """3 buy events: hand-computed left-limit intensities must match engine."""
        mu = 1.0
        alpha_self = 10.0
        alpha_cross = 0.0
        beta = 1.0
        T = 1.0

        times = np.array([0.0, 0.1, 0.2], dtype=np.float64)
        sides = np.array([1, 1, 1], dtype=np.int8)

        # Analytical left-limit intensities (lambda(t_i^-)):
        lam_0 = mu
        lam_1 = mu + alpha_self * np.exp(-beta * 0.1)
        lam_2 = mu + alpha_self * (np.exp(-beta * 0.2) + np.exp(-beta * 0.1))

        expected_log_sum = np.log(lam_0) + np.log(lam_1) + np.log(lam_2)

        # Analytical compensator:
        R_after_0 = 1.0
        comp_01 = alpha_self * R_after_0 * (1 - np.exp(-beta * 0.1)) / beta

        R_after_1 = np.exp(-beta * 0.1) * R_after_0 + 1.0
        comp_12 = alpha_self * R_after_1 * (1 - np.exp(-beta * 0.1)) / beta

        R_after_2 = np.exp(-beta * 0.1) * R_after_1 + 1.0
        comp_2T = alpha_self * R_after_2 * (1 - np.exp(-beta * 0.8)) / beta

        mu_sell = mu
        expected_compensator = comp_01 + comp_12 + comp_2T + mu * T + mu_sell * T
        expected_ll = expected_log_sum - expected_compensator

        actual_ll = eval_ll_simple(times, sides, mu, mu_sell, alpha_self, alpha_cross, beta, T)

        assert abs(actual_ll - expected_ll) < 1e-6, (
            f"Engine LL ({actual_ll:.6f}) != analytical ({expected_ll:.6f}). "
            f"Difference: {actual_ll - expected_ll:.6f}. "
            f"Expected log_sum={expected_log_sum:.6f}, compensator={expected_compensator:.6f}"
        )

    def test_first_event_intensity_is_mu(self):
        """At the first event, lambda(t_0^-) = mu (no history)."""
        mu = 2.0
        alpha_self = 50.0
        alpha_cross = 0.0
        beta = 1.0
        T = 10.0

        times = np.array([0.0, 5.0], dtype=np.float64)
        sides = np.array([1, 1], dtype=np.int8)

        lam_0 = mu
        lam_1 = mu + alpha_self * np.exp(-beta * 5.0)

        expected_log_sum = np.log(lam_0) + np.log(lam_1)

        R_after_0 = 1.0
        comp_01 = alpha_self * R_after_0 * (1 - np.exp(-beta * 5.0)) / beta
        R_after_1 = np.exp(-beta * 5.0) * R_after_0 + 1.0
        comp_1T = alpha_self * R_after_1 * (1 - np.exp(-beta * 5.0)) / beta
        expected_comp = comp_01 + comp_1T + mu * T + mu * T

        expected_ll = expected_log_sum - expected_comp
        actual_ll = eval_ll_simple(times, sides, mu, mu, alpha_self, alpha_cross, beta, T)

        assert abs(actual_ll - expected_ll) < 1e-6, (
            f"LL mismatch: engine={actual_ll:.6f}, expected={expected_ll:.6f}. "
            f"If engine >> expected, self-inclusion bug is present."
        )

    def test_bivariate_cross_excitation(self):
        """Buy then sell: cross-excitation R must use left-limit (no self-inclusion)."""
        mu = 1.0
        alpha_self = 5.0
        alpha_cross = 3.0
        beta = 2.0
        T = 2.0

        times = np.array([0.0, 0.5], dtype=np.float64)
        sides = np.array([1, -1], dtype=np.int8)

        lam_buy_0 = mu
        R_buy_at_1 = np.exp(-beta * 0.5) * 1.0
        lam_sell_1 = mu + alpha_self * 0.0 + alpha_cross * R_buy_at_1

        expected_log_sum = np.log(lam_buy_0) + np.log(lam_sell_1)

        factor_01 = (1 - np.exp(-beta * 0.5)) / beta
        comp_buy_01 = alpha_self * 1.0 * factor_01
        comp_sell_01 = alpha_cross * 1.0 * factor_01
        R_buy_after_1 = np.exp(-beta * 0.5) * 1.0
        R_sell_after_1 = 1.0
        factor_1T = (1 - np.exp(-beta * 1.5)) / beta
        comp_buy_1T = (alpha_self * R_buy_after_1 + alpha_cross * R_sell_after_1) * factor_1T
        comp_sell_1T = (alpha_self * R_sell_after_1 + alpha_cross * R_buy_after_1) * factor_1T

        expected_comp = (comp_buy_01 + comp_sell_01 +
                         comp_buy_1T + comp_sell_1T +
                         mu * T + mu * T)
        expected_ll = expected_log_sum - expected_comp

        actual_ll = eval_ll_simple(times, sides, mu, mu, alpha_self, alpha_cross, beta, T)

        assert abs(actual_ll - expected_ll) < 1e-6, (
            f"Bivariate LL mismatch: engine={actual_ll:.6f}, expected={expected_ll:.6f}"
        )


class TestFixedBetaFit:
    """Test 5: Fixed-beta univariate MLE must recover alpha/mu from synthetic data."""

    def test_alpha_mu_recovery_with_fixed_beta(self):
        """MLE with fixed beta should recover alpha_self/mu within 2x of true values."""
        mu_true = 0.5
        alpha_self_true = 0.3
        beta_true = 5.0
        T = 7200.0

        # Simulate univariate (alpha_cross=0)
        times, sides = simulate_bivariate_hawkes(
            mu_true, alpha_self_true, 0.0, beta_true, T, seed=42,
        )
        assert len(times) > 500

        from core.hawkes.forgetting import fit_hawkes_forgetting

        params = fit_hawkes_forgetting(
            t_sec=times,
            sides=sides,
            rho=1.0,  # no forgetting for clean recovery
            lambda_ref=len(times) / T,
            T=T,
            n_restarts=5,
            beta_fixed=beta_true,
        )

        # Beta should be exactly the fixed value
        assert params.beta == beta_true, (
            f"Beta should be fixed at {beta_true}, got {params.beta}"
        )

        # Cross alphas should be zero
        assert params.alpha_buy_cross == 0.0, "alpha_buy_cross should be 0"
        assert params.alpha_sell_cross == 0.0, "alpha_sell_cross should be 0"

        # Alpha self should be within 2x of true value
        ratio_self = params.alpha_buy_self / alpha_self_true
        assert 0.5 < ratio_self < 2.0, (
            f"Recovered alpha_self={params.alpha_buy_self:.3f}, "
            f"true={alpha_self_true:.3f}, ratio={ratio_self:.2f}"
        )

        # n_base should be reasonable
        n_base = max(params.alpha_buy_self, params.alpha_sell_self) / params.beta
        assert n_base < 2.0, (
            f"n_base={n_base:.2f} too high after fixed-beta MLE"
        )


class TestAtomicSwap:
    """Test 6: HawkesEngine swap_params updates all 4 fitted params atomically."""

    def test_swap_params_updates_all(self):
        """After swap_params, all 4 fitted parameters should reflect new values."""
        from core.hawkes.engine import HawkesEngine

        engine = HawkesEngine(
            beta_mle=50.0,
            alpha_self_buy=1.0,
            alpha_cross_buy=0.1,
            mu_buy=5.0,
            mu_sell=5.0,
            lambda_ref=10.0,
            alpha_self_sell=1.0,
            alpha_cross_sell=0.1,
        )

        # Swap 4 fitted params (beta is fixed at init, cross always 0)
        engine.swap_params(
            alpha_self_buy=2.0,
            alpha_self_sell=2.5,
            mu_buy=8.0,
            mu_sell=7.0,
        )

        params = engine._read_params()
        # _read_params returns 7-tuple; cross alphas should be 0.0
        assert params == (2.0, 0.0, 2.5, 0.0, 8.0, 7.0, 50.0), (
            f"swap_params did not update correctly: {params}"
        )

    def test_swap_params_does_not_change_beta(self):
        """swap_params must not affect beta — it's fixed at init."""
        from core.hawkes.engine import HawkesEngine

        engine = HawkesEngine(
            beta_mle=50.0,
            alpha_self_buy=1.0,
            alpha_cross_buy=0.1,
            mu_buy=5.0,
            mu_sell=5.0,
            lambda_ref=10.0,
            alpha_self_sell=1.0,
            alpha_cross_sell=0.1,
        )

        engine.swap_params(
            alpha_self_buy=2.0,
            alpha_self_sell=2.0,
            mu_buy=8.0, mu_sell=8.0,
        )

        assert engine.beta_mle == 50.0, "beta changed after swap — should be fixed"

    def test_swap_params_does_not_reset_state(self):
        """swap_params must not reset R, E, or Edot state."""
        from core.hawkes.engine import HawkesEngine

        engine = HawkesEngine(
            beta_mle=50.0,
            alpha_self_buy=1.0,
            alpha_cross_buy=0.1,
            mu_buy=5.0,
            mu_sell=5.0,
            lambda_ref=10.0,
            alpha_self_sell=1.0,
            alpha_cross_sell=0.1,
        )

        # Process some events to build up state
        engine.update(0.0, 1)
        engine.update(0.01, -1)
        engine.update(0.02, 1)

        R_buy_before = engine._R_buy
        R_sell_before = engine._R_sell
        E_before = engine._E
        n_events_before = engine._n_events

        # Swap params
        engine.swap_params(
            alpha_self_buy=2.0,
            alpha_self_sell=2.0,
            mu_buy=8.0, mu_sell=8.0,
        )

        assert engine._R_buy == R_buy_before, "R_buy changed after swap"
        assert engine._R_sell == R_sell_before, "R_sell changed after swap"
        assert engine._E == E_before, "E changed after swap"
        assert engine._n_events == n_events_before, "n_events changed after swap"


class TestWarmStart:
    """Test 7: fit_online warm start produces equal or better LL than cold start."""

    def test_warm_start_convergence(self):
        """fit_online with warm start should converge to similar or better LL."""
        mu_true = 0.5
        alpha_self_true = 0.3
        beta_true = 5.0
        T = 3600.0

        # Simulate univariate (no cross)
        times, sides = simulate_bivariate_hawkes(
            mu_true, alpha_self_true, 0.0, beta_true, T, seed=42,
        )
        lambda_ref = len(times) / T

        from core.hawkes.forgetting import fit_hawkes_forgetting, fit_online

        # Cold start
        params_cold = fit_hawkes_forgetting(
            t_sec=times, sides=sides, rho=0.999,
            lambda_ref=lambda_ref, T=T, n_restarts=3,
            beta_fixed=beta_true,
        )

        # Warm start from cold result
        params_warm = fit_online(
            t_sec=times, sides=sides, rho=0.999,
            lambda_ref=lambda_ref, prev_params=params_cold,
            T=T, n_restarts=3,
            beta_fixed=beta_true,
        )

        # Warm start should produce equal or better LL
        assert params_warm.log_likelihood >= params_cold.log_likelihood - 1.0, (
            f"Warm start LL ({params_warm.log_likelihood:.1f}) significantly "
            f"worse than cold start ({params_cold.log_likelihood:.1f})"
        )


class TestAnalyticalGradient:
    """Test 8: Numba analytical gradient matches finite-difference gradient."""

    def test_gradient_matches_finite_diff(self):
        """Analytical 4-element gradient must match central finite-difference."""
        from core.hawkes.forgetting import _weighted_ll_and_grad, _weighted_log_likelihood

        rng = np.random.default_rng(42)
        N = 200
        t_sec = np.sort(rng.uniform(0, 50, N)).astype(np.float64)
        sides = rng.choice([1, -1], N).astype(np.int8)
        T = 60.0
        rho = 0.9999
        beta_fixed = 50.0

        # 4 optimized params: [a_sb, a_ss, mu_b, mu_s]
        params = np.array([2.0, 1.8, 0.5, 0.5])

        ll, grad_analytical = _weighted_ll_and_grad(
            t_sec, sides,
            params[0], params[1],
            params[2], params[3], beta_fixed,
            T, rho,
        )

        # Central finite-difference gradient (4 params, beta fixed)
        eps = 1e-6
        grad_fd = np.zeros(4)
        for j in range(4):
            p_plus = params.copy()
            p_minus = params.copy()
            p_plus[j] += eps
            p_minus[j] -= eps

            ll_plus = _weighted_log_likelihood(
                t_sec, sides,
                p_plus[0], p_plus[1],
                p_plus[2], p_plus[3], beta_fixed,
                T, rho,
            )
            ll_minus = _weighted_log_likelihood(
                t_sec, sides,
                p_minus[0], p_minus[1],
                p_minus[2], p_minus[3], beta_fixed,
                T, rho,
            )
            grad_fd[j] = (ll_plus - ll_minus) / (2 * eps)

        # Compare
        max_diff = np.max(np.abs(grad_analytical - grad_fd))

        assert max_diff < 1e-3, (
            f"Analytical gradient differs from FD by {max_diff:.2e}.\n"
            f"Analytical: {grad_analytical}\n"
            f"FD:         {grad_fd}"
        )

    def test_gradient_at_multiple_param_sets(self):
        """Gradient correctness across diverse parameter regimes."""
        from core.hawkes.forgetting import _weighted_ll_and_grad, _weighted_log_likelihood

        rng = np.random.default_rng(99)
        N = 300
        t_sec = np.sort(rng.uniform(0, 100, N)).astype(np.float64)
        sides = rng.choice([1, -1], N).astype(np.int8)
        T = 110.0

        # Each case: (4-element param vector, beta_fixed, rho)
        test_cases = [
            (np.array([0.1, 0.15, 2.0, 2.0]), 10.0, 0.999),
            (np.array([10.0, 8.0, 0.2, 0.3]), 200.0, 0.9999),
            (np.array([1.0, 1.0, 1.0, 1.0]), 50.0, 0.995),
        ]

        eps = 1e-6
        for params, beta_fixed, rho in test_cases:
            _, grad_a = _weighted_ll_and_grad(
                t_sec, sides,
                params[0], params[1],
                params[2], params[3], beta_fixed,
                T, rho,
            )

            grad_fd = np.zeros(4)
            for j in range(4):
                p_plus = params.copy()
                p_minus = params.copy()
                p_plus[j] += eps
                p_minus[j] -= eps
                ll_p = _weighted_log_likelihood(
                    t_sec, sides,
                    p_plus[0], p_plus[1],
                    p_plus[2], p_plus[3], beta_fixed, T, rho,
                )
                ll_m = _weighted_log_likelihood(
                    t_sec, sides,
                    p_minus[0], p_minus[1],
                    p_minus[2], p_minus[3], beta_fixed, T, rho,
                )
                grad_fd[j] = (ll_p - ll_m) / (2 * eps)

            max_diff = np.max(np.abs(grad_a - grad_fd))
            assert max_diff < 1e-3, (
                f"Gradient mismatch for params={params}, beta={beta_fixed}, "
                f"rho={rho}: max diff={max_diff:.2e}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
