"""C1 unit tests for the rho_fast parameter added to run_setup_filter."""
from __future__ import annotations

import numpy as np
import pytest

from backtest.setup_filter import run_setup_filter, RHO_FAST

NS_PER_SECOND = 1_000_000_000
BAR_NS = 60 * NS_PER_SECOND


def _make_ticks(n_bars: int = 80, seed: int = 0) -> tuple:
    """Generate exactly n_bars 1-minute bars, one tick per bar at +30s.

    Matches the bar-aligned pattern used in test_runner_sf.py. Each tick lands
    30 seconds into its bar, guaranteeing bar_idx = i for tick i.
    """
    rng = np.random.default_rng(seed)
    sess_ns = 4 * 3600 * NS_PER_SECOND  # synthetic 4:00 AM anchor (no real-date dependency)
    ts = np.array(
        [sess_ns + i * BAR_NS + 30 * NS_PER_SECOND for i in range(n_bars)],
        dtype=np.int64,
    )
    prices = 50.0 + np.cumsum(rng.standard_normal(n_bars) * 0.05)
    prices = np.clip(prices, 1.0, None)
    sizes = rng.integers(100, 1000, size=n_bars).astype(np.float64)
    return ts, prices, sizes


def _run(rho: float | None = None, seed: int = 0, n_bars: int = 80):
    ts, prices, sizes = _make_ticks(n_bars=n_bars, seed=seed)
    start, end = int(ts[0]), int(ts[-1])
    kwargs: dict = {}
    if rho is not None:
        kwargs["rho_fast"] = rho
    return run_setup_filter(ts, prices, sizes, start, end, **kwargs)


# ---------------------------------------------------------------------------
# T3b — explicit rho_fast=RHO_FAST produces bit-for-bit identical output to default
# ---------------------------------------------------------------------------

class TestRhoFastDefault:

    def test_explicit_rho_fast_equals_default_q_tilde(self):
        """T3b: run_setup_filter(rho_fast=RHO_FAST) == run_setup_filter() q_tilde."""
        r_default = _run(rho=None)
        r_explicit = _run(rho=RHO_FAST)
        assert len(r_default.q_tilde) > 0, "q_tilde must be non-empty (tick generation issue)"
        np.testing.assert_array_equal(
            r_default.q_tilde,
            r_explicit.q_tilde,
            err_msg="Explicit rho_fast=RHO_FAST must be bit-for-bit identical to default",
        )

    def test_explicit_rho_fast_equals_default_q_raw(self):
        r_default = _run(rho=None)
        r_explicit = _run(rho=RHO_FAST)
        assert len(r_default.q_raw) > 0
        np.testing.assert_array_equal(r_default.q_raw, r_explicit.q_raw)

    def test_explicit_rho_fast_equals_default_passes(self):
        r_default = _run(rho=None)
        r_explicit = _run(rho=RHO_FAST)
        assert r_default.passes == r_explicit.passes

    def test_explicit_rho_fast_equals_default_mean_q(self):
        r_default = _run(rho=None)
        r_explicit = _run(rho=RHO_FAST)
        assert r_default.mean_q_tilde == r_explicit.mean_q_tilde

    def test_rho_fast_default_value_is_0_90(self):
        assert RHO_FAST == 0.90

    def test_q_tilde_is_non_empty_with_80_bars(self):
        """Sanity: 80 bars produces non-empty q_tilde (guards against bad tick generation)."""
        r = _run()
        assert len(r.q_tilde) > 0
        assert r.weakest_signal != "insufficient_bars"


# ---------------------------------------------------------------------------
# T3c — rho_fast=0.75 produces DIFFERENT (faster-adapting) q_tilde from default
# ---------------------------------------------------------------------------

class TestRhoFastAlternate:

    def test_rho_0_75_differs_from_default_q_tilde(self):
        """T3c: rho_fast=0.75 must produce different q_tilde than rho_fast=0.90."""
        r_default = _run(rho=RHO_FAST)
        r_fast = _run(rho=0.75)
        assert len(r_default.q_tilde) > 0, "q_tilde must be non-empty"
        assert not np.array_equal(r_default.q_tilde, r_fast.q_tilde), (
            "rho_fast=0.75 must produce different q_tilde than rho_fast=0.90"
        )

    def test_rho_0_75_tracks_q_raw_more_closely(self):
        """Lower rho → less smoothing → q_tilde tracks q_raw with less lag (smaller MAE)."""
        r_default = _run(rho=RHO_FAST, seed=1)
        r_fast = _run(rho=0.75, seed=1)
        assert len(r_default.q_tilde) > 0
        mae_default = float(np.mean(np.abs(r_default.q_tilde - r_default.q_raw)))
        mae_fast = float(np.mean(np.abs(r_fast.q_tilde - r_fast.q_raw)))
        assert mae_fast < mae_default, (
            f"Lower rho should track q_raw with less lag: "
            f"mae_fast={mae_fast:.6f} vs mae_default={mae_default:.6f}"
        )

    def test_rho_1_0_produces_constant_q_tilde(self):
        """rho_fast=1.0 means no update → q_tilde stays at initial value (0.5)."""
        r = _run(rho=1.0)
        assert len(r.q_tilde) > 0
        assert np.all(r.q_tilde == 0.5), (
            "rho_fast=1.0 should freeze q_tilde at initial value qt=0.5 (see _compute_setup_signals)"
        )

    def test_rho_0_0_produces_q_tilde_equal_to_q_raw(self):
        """rho_fast=0.0 means no memory → q_tilde == q_raw bar-for-bar."""
        r = _run(rho=0.0, seed=2)
        assert len(r.q_tilde) > 0
        np.testing.assert_array_almost_equal(
            r.q_tilde, r.q_raw, decimal=10,
            err_msg="rho_fast=0.0 must make q_tilde track q_raw exactly",
        )

    def test_rho_0_5_differs_from_both_endpoints(self):
        r_fast = _run(rho=0.0)
        r_slow = _run(rho=1.0)
        r_mid = _run(rho=0.5)
        assert len(r_mid.q_tilde) > 0
        assert not np.array_equal(r_mid.q_tilde, r_fast.q_tilde)
        assert not np.array_equal(r_mid.q_tilde, r_slow.q_tilde)

    def test_higher_rho_means_more_inertia(self):
        """Compare three rho values: 0.0 < 0.75 < 0.90 in terms of q_raw tracking."""
        r_0 = _run(rho=0.0, seed=3)
        r_75 = _run(rho=0.75, seed=3)
        r_90 = _run(rho=RHO_FAST, seed=3)
        assert len(r_0.q_tilde) > 0
        mae_0 = float(np.mean(np.abs(r_0.q_tilde - r_0.q_raw)))
        mae_75 = float(np.mean(np.abs(r_75.q_tilde - r_75.q_raw)))
        mae_90 = float(np.mean(np.abs(r_90.q_tilde - r_90.q_raw)))
        assert mae_0 <= mae_75 <= mae_90, (
            f"MAE should monotonically increase with rho: "
            f"mae_0={mae_0:.6f}, mae_75={mae_75:.6f}, mae_90={mae_90:.6f}"
        )
