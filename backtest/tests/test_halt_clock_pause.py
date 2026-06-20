"""C3 unit tests — halt-gap clock pause in _hawkes_replay_with_refit.

Verifies that inter-trade gaps overlapping a halt window are paused (dt_eff = 1e-6)
so Hawkes intensity does not decay to baseline across a trading halt.

T3a: halt_intervals=None → output numerically identical to default (no-halt) path.
T3b: halt interval covering a 300s gap → lam_buy[post_gap] > 10 × no-halt baseline.
T3c: Combined gate intensity (lam_buy + lam_sell) elevated at first post-gap tick.
T3d: Gap < halt_gap_threshold (60s) is NOT paused even when covered by an interval.
T3e: Multiple halt intervals in one event sequence all pause correctly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runner as _runner_mod
from core.hawkes.forgetting import HawkesParams

# ---------------------------------------------------------------------------
# Fixed Hawkes params used in all tests — bypasses scipy MLE for speed and
# determinism.  alpha=0.5, mu=0.1, beta=0.1 give a half-life of ~7s and
# equilibrium R ≈ 10.5 for all-buy streams at 1s cadence, producing a clear
# ratio signal for T3b/T3c.
# ---------------------------------------------------------------------------
_FIXED = HawkesParams(
    alpha_buy_self=0.5,
    alpha_buy_cross=0.0,
    alpha_sell_self=0.5,
    alpha_sell_cross=0.0,
    mu_buy=0.1,
    mu_sell=0.1,
    beta=0.1,
    log_likelihood=-999.0,
    n_events_used=100,
)


@pytest.fixture(autouse=True)
def mock_fits(monkeypatch):
    """Replace scipy MLE with fixed deterministic params for all C3 tests."""
    monkeypatch.setattr(_runner_mod, "fit_hawkes_forgetting", lambda *a, **kw: _FIXED)
    monkeypatch.setattr(_runner_mod, "fit_online", lambda *a, **kw: _FIXED)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INIT = {
    "alpha_buy_self": 0.5,
    "alpha_sell_self": 0.5,
    "mu_buy": 0.1,
    "mu_sell": 0.1,
    "beta": 0.1,
}


def _replay(t_sec, sides, halt_intervals=None, halt_gap_threshold=60.0):
    """Call _hawkes_replay_with_refit and return (lam_buy, lam_sell) arrays."""
    N = len(t_sec)
    lam_b = np.zeros(N)
    lam_s = np.zeros(N)
    E = np.zeros(N)
    Edot = np.zeros(N)
    nb = np.zeros(N)
    _runner_mod._hawkes_replay_with_refit(
        t_sec=t_sec,
        sides=sides,
        rho=0.95,
        lambda_ref=0.2,
        init_params=_INIT,
        rho_E=0.9,
        lam_buy_out=lam_b,
        lam_sell_out=lam_s,
        E_out=E,
        Edot_out=Edot,
        n_base_out=nb,
        halt_intervals=halt_intervals,
        halt_gap_threshold=halt_gap_threshold,
    )
    return lam_b, lam_s


def _gap_sequence(n_before, gap_sec, n_after=20, dt=1.0):
    """Build (t_sec, sides) with N=n_before+1+n_after trades and one gap.

    Returns relative-second t_sec (first trade at t=0) and all-buy sides.
    The gap is between trade index (n_before-1) and trade index n_before.
    """
    t_before = np.arange(n_before, dtype=float) * dt           # 0 .. n_before-1
    t_gap_start = t_before[-1] + gap_sec                        # first post-gap trade
    t_after = t_gap_start + np.arange(1, n_after + 1) * dt
    t_sec = np.concatenate([t_before, [t_gap_start], t_after])
    sides = np.ones(len(t_sec), dtype=np.int8)                  # all buys
    return t_sec, sides


# ---------------------------------------------------------------------------
# T3a — Identity when halt_intervals=None
# ---------------------------------------------------------------------------

class TestT3aIdentity:
    """halt_intervals=None gives byte-identical output to the default call."""

    def test_lam_buy_identical(self):
        t_sec, sides = _gap_sequence(100, 300.0)
        lam_b_default, _ = _replay(t_sec, sides)
        lam_b_none, _ = _replay(t_sec, sides, halt_intervals=None)
        np.testing.assert_array_equal(lam_b_default, lam_b_none)

    def test_lam_sell_identical(self):
        t_sec, sides = _gap_sequence(100, 300.0)
        _, lam_s_default = _replay(t_sec, sides)
        _, lam_s_none = _replay(t_sec, sides, halt_intervals=None)
        np.testing.assert_array_equal(lam_s_default, lam_s_none)

    def test_empty_list_identical_to_none(self):
        """Empty halt_intervals list behaves identically to None."""
        t_sec, sides = _gap_sequence(100, 300.0)
        lam_b_none, _ = _replay(t_sec, sides, halt_intervals=None)
        lam_b_empty, _ = _replay(t_sec, sides, halt_intervals=[])
        np.testing.assert_array_equal(lam_b_none, lam_b_empty)


# ---------------------------------------------------------------------------
# T3b — λ_buy elevated at post-gap tick when halt covers the gap
# ---------------------------------------------------------------------------

class TestT3bLambdaElevated:
    """After a 300s halt-covered gap, lam_buy[post_gap] > 10× no-halt baseline."""

    def test_lambda_ratio_exceeds_10x(self):
        t_sec, sides = _gap_sequence(100, 300.0)
        gap_idx = 100  # the post-gap trade

        # The gap is [t_sec[99], t_sec[100]] = [99, 399] in relative seconds
        halt = [(t_sec[99], t_sec[gap_idx])]

        lam_b_no_halt, _ = _replay(t_sec, sides)
        lam_b_paused, _ = _replay(t_sec, sides, halt_intervals=halt)

        assert lam_b_paused[gap_idx] > lam_b_no_halt[gap_idx] * 10, (
            f"Expected lam_buy[{gap_idx}] paused ({lam_b_paused[gap_idx]:.4f}) "
            f"> 10 × no-halt ({lam_b_no_halt[gap_idx]:.4f})"
        )

    def test_no_effect_on_pre_gap_ticks(self):
        """Ticks before the gap are unaffected by halt_intervals."""
        t_sec, sides = _gap_sequence(100, 300.0)
        halt = [(t_sec[99], t_sec[100])]

        lam_b_no_halt, _ = _replay(t_sec, sides)
        lam_b_paused, _ = _replay(t_sec, sides, halt_intervals=halt)

        np.testing.assert_array_equal(lam_b_no_halt[:100], lam_b_paused[:100])


# ---------------------------------------------------------------------------
# T3c — Gate λ_V (lam_buy + lam_sell) elevated post-gap
# ---------------------------------------------------------------------------

class TestT3cGateLambdaV:
    """Combined intensity elevated at post-gap tick — relevant to gate λ_V."""

    def test_total_lambda_elevated_5x(self):
        t_sec, sides = _gap_sequence(100, 300.0)
        halt = [(t_sec[99], t_sec[100])]

        lam_b_no, lam_s_no = _replay(t_sec, sides)
        lam_b_pause, lam_s_pause = _replay(t_sec, sides, halt_intervals=halt)

        total_no = lam_b_no[100] + lam_s_no[100]
        total_pause = lam_b_pause[100] + lam_s_pause[100]

        assert total_pause > total_no * 5, (
            f"Expected total λ paused ({total_pause:.4f}) > 5 × no-halt ({total_no:.4f})"
        )


# ---------------------------------------------------------------------------
# T3d — Gap below threshold NOT paused even with a covering interval
# ---------------------------------------------------------------------------

class TestT3dBelowThresholdNotPaused:
    """Gaps shorter than halt_gap_threshold are never paused."""

    def test_30s_gap_not_paused(self):
        # 30s gap, default halt_gap_threshold=60s → should NOT be paused
        t_sec, sides = _gap_sequence(100, 30.0)
        halt = [(t_sec[99], t_sec[100])]  # covers the 30s gap

        lam_b_no, _ = _replay(t_sec, sides)
        lam_b_halt, _ = _replay(t_sec, sides, halt_intervals=halt)

        # With 30s gap, decay = exp(-0.1*30) ≈ 0.05 (not 1.0), so both
        # paths should give the same result (gap ignored).
        np.testing.assert_array_equal(lam_b_no, lam_b_halt)

    def test_gap_exactly_at_threshold_not_paused(self):
        """Gap == halt_gap_threshold is not paused (strictly greater than required)."""
        t_sec, sides = _gap_sequence(100, 60.0)  # exactly 60s
        halt = [(t_sec[99], t_sec[100])]

        lam_b_no, _ = _replay(t_sec, sides)
        lam_b_halt, _ = _replay(t_sec, sides, halt_intervals=halt)

        np.testing.assert_array_equal(lam_b_no, lam_b_halt)

    def test_gap_just_above_threshold_is_paused(self):
        """Gap slightly above halt_gap_threshold IS paused."""
        t_sec, sides = _gap_sequence(100, 61.0)  # 61s > 60s threshold
        halt = [(t_sec[99], t_sec[100])]

        lam_b_no, _ = _replay(t_sec, sides)
        lam_b_halt, _ = _replay(t_sec, sides, halt_intervals=halt)

        # 61s gap: without pause, decay = exp(-6.1) ≈ 0.002; with pause decay ≈ 1.0
        assert lam_b_halt[100] > lam_b_no[100] * 5


# ---------------------------------------------------------------------------
# T3e — Multiple halt intervals in one event
# ---------------------------------------------------------------------------

class TestT3eMultipleHaltWindows:
    """Two halt-covered gaps in one sequence are both paused independently."""

    def test_both_gaps_paused(self):
        # Build: 50 buys, 300s gap, 50 buys, 300s gap, 20 buys
        t1 = np.arange(50, dtype=float)              # t=0..49
        t2_start = t1[-1] + 300.0                    # t=349
        t2 = t2_start + np.arange(1, 51, dtype=float)  # t=350..399
        t3_start = t2[-1] + 300.0                    # t=699
        t3 = t3_start + np.arange(1, 21, dtype=float)  # t=700..719
        t_sec = np.concatenate([t1, [t2_start], t2, [t3_start], t3])
        sides = np.ones(len(t_sec), dtype=np.int8)

        gap1_idx = 50    # post first-gap trade
        gap2_idx = 101   # post second-gap trade

        halt = [
            (t_sec[49], t_sec[gap1_idx]),   # covers first 300s gap
            (t_sec[100], t_sec[gap2_idx]),  # covers second 300s gap
        ]

        lam_b_no, _ = _replay(t_sec, sides)
        lam_b_pause, _ = _replay(t_sec, sides, halt_intervals=halt)

        assert lam_b_pause[gap1_idx] > lam_b_no[gap1_idx] * 10, (
            f"First gap not paused: paused={lam_b_pause[gap1_idx]:.4f} "
            f"no-halt={lam_b_no[gap1_idx]:.4f}"
        )
        assert lam_b_pause[gap2_idx] > lam_b_no[gap2_idx] * 10, (
            f"Second gap not paused: paused={lam_b_pause[gap2_idx]:.4f} "
            f"no-halt={lam_b_no[gap2_idx]:.4f}"
        )

    def test_non_overlapping_gap_not_paused(self):
        """A gap NOT covered by any halt interval is not paused."""
        t_sec, sides = _gap_sequence(100, 300.0, n_after=40)
        # halt interval covers a different region, NOT the actual gap
        halt = [(t_sec[50], t_sec[60])]  # short window away from the gap

        lam_b_no, _ = _replay(t_sec, sides)
        lam_b_halt, _ = _replay(t_sec, sides, halt_intervals=halt)

        # The 300s gap at idx=100 is not covered → outputs identical
        np.testing.assert_array_equal(lam_b_no, lam_b_halt)
