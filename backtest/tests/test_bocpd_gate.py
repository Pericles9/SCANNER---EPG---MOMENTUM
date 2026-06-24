"""
Unit tests for the BOCPD gate branch (gate_mode="bocpd") of ParticipationGate.
Phase CPD-BOCPD, Task T2a.

Directional surge-aware Bayesian Online Changepoint Detection. Required cases from the
phase plan:
  - flat WJI_log = 0      -> P_regime stays low, no PASS
  - step-up to +3 sigma   -> P_regime rises to >= p_enter within 10 ticks of the step
  - step-down to 0        -> gate transitions to FAIL within 10 ticks of return to background
  - run-length truncation -> 700-tick trace: no index error, R stays normalized throughout
  - halt                  -> R unchanged across a halt, first post-halt tick updates normally

Tests drive warmup with a constant WJI so sigma_log falls back to a known value, making the
arithmetic transparent. Warmup window = [t_event, t_event + warmup_seconds).
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from core.epg.gate import ParticipationGate, GateState


SIGMA = 0.20  # warmup fallback sigma_log used across tests (constant warmup -> fallback)


def make_gate(lambda_h=0.01, p_enter=0.70, sigma_fallback=SIGMA, warmup=300.0,
              prior_mean_std=1.0, dir_thresh_mult=1.0, max_run_length=600):
    g = ParticipationGate(
        half_life_seconds=300.0, peak_threshold_p=0.65, warmup_seconds=warmup,
        gate_mode="bocpd", lambda_h=lambda_h, p_enter=p_enter,
        sigma_log_fallback=sigma_fallback, prior_mean_std=prior_mean_std,
        dir_thresh_mult=dir_thresh_mult, max_run_length=max_run_length,
    )
    g.activate(t_event=0.0)
    return g


def drive_warmup(g, wji=1.0, n=25, step=5.0):
    """Feed n constant-WJI ticks inside the warmup window (zero variance -> sigma fallback)."""
    for i in range(n):
        st = g.update(wji=wji, timestamp=float(i) * step, wji_background=1.0)
        assert st == GateState.WARMUP


# ── construction / validation ──────────────────────────────────────────

def test_p_exit_derived_with_fixed_gap():
    g = make_gate(p_enter=0.80)
    assert g.p_enter == pytest.approx(0.80)
    assert g.p_exit == pytest.approx(0.70)  # hard-coded 0.10 gap


def test_invalid_params_raise():
    with pytest.raises(ValueError):
        ParticipationGate(300.0, 0.65, gate_mode="bocpd", lambda_h=0.0)
    with pytest.raises(ValueError):
        ParticipationGate(300.0, 0.65, gate_mode="bocpd", lambda_h=1.0)
    with pytest.raises(ValueError):
        ParticipationGate(300.0, 0.65, gate_mode="bocpd", p_enter=0.10)  # p_exit would be 0
    with pytest.raises(ValueError):
        ParticipationGate(300.0, 0.65, gate_mode="bocpd", prior_mean_std=0.0)
    with pytest.raises(ValueError):
        ParticipationGate(300.0, 0.65, gate_mode="bocpd", max_run_length=0)


# ── flat trace ─────────────────────────────────────────────────────────

def test_flat_trace_no_pass():
    """Constant WJI_log = 0 (background): P_regime stays low, gate never opens."""
    g = make_gate(p_enter=0.70)
    drive_warmup(g, wji=1.0, n=25)
    for i in range(200):
        st = g.update(wji=1.0, timestamp=300.0 + i, wji_background=1.0)
        assert st in (GateState.PASS, GateState.FAIL)
        assert st == GateState.FAIL                  # never opens on background
        assert g.p_regime < 0.70                     # below p_enter throughout
    assert g.p_regime < 0.10                          # essentially zero elevated mass


def test_flat_trace_posterior_normalized():
    g = make_gate()
    drive_warmup(g, wji=1.0, n=25)
    for i in range(100):
        g.update(wji=1.0, timestamp=300.0 + i, wji_background=1.0)
        R = g.bocpd_R
        assert R is not None
        assert R.sum() == pytest.approx(1.0, abs=1e-9)


# ── step-up ────────────────────────────────────────────────────────────

def test_step_up_opens_within_10_ticks():
    """WJI_log steps to +3 sigma after a settled background run -> PASS within 10 ticks."""
    g = make_gate(p_enter=0.70)
    drive_warmup(g, wji=1.0, n=25)
    # settle on background after warmup
    for i in range(60):
        g.update(wji=1.0, timestamp=300.0 + i, wji_background=1.0)
    assert not g._in_pass
    wji_surge = math.exp(3.0 * SIGMA)  # WJI_log = +3 sigma
    opened_at = None
    for j in range(10):
        st = g.update(wji=wji_surge, timestamp=360.0 + j, wji_background=1.0)
        if st == GateState.PASS:
            opened_at = j
            break
    assert opened_at is not None, f"gate did not open within 10 ticks (P_regime={g.p_regime:.3f})"
    assert g.p_regime >= 0.70


# ── step-down ──────────────────────────────────────────────────────────

def test_step_down_closes_within_10_ticks():
    """After a sustained surge regime, return to background -> FAIL within 10 ticks."""
    g = make_gate(p_enter=0.70)
    drive_warmup(g, wji=1.0, n=25)
    for i in range(20):
        g.update(wji=1.0, timestamp=300.0 + i, wji_background=1.0)
    wji_surge = math.exp(3.0 * SIGMA)
    last = None
    for i in range(80):  # sustained surge -> establish elevated regime, gate opens
        last = g.update(wji=wji_surge, timestamp=320.0 + i, wji_background=1.0)
    assert last == GateState.PASS and g._in_pass
    closed_at = None
    for j in range(10):  # return to background
        st = g.update(wji=1.0, timestamp=400.0 + j, wji_background=1.0)
        if st == GateState.FAIL:
            closed_at = j
            break
    assert closed_at is not None, f"gate did not close within 10 ticks (P_regime={g.p_regime:.3f})"
    assert g.p_regime < g.p_exit


# ── run-length truncation ──────────────────────────────────────────────

def test_truncation_no_index_error_and_normalized():
    """700-tick trace past a small cap: no index error; R stays normalized and capped."""
    cap = 50
    g = make_gate(p_enter=0.70, max_run_length=cap)
    drive_warmup(g, wji=1.0, n=25)
    wji_surge = math.exp(2.0 * SIGMA)
    for i in range(700):
        g.update(wji=wji_surge, timestamp=300.0 + i, wji_background=1.0)
        R = g.bocpd_R
        assert R is not None
        assert len(R) <= cap + 1                       # never exceeds the cap
        assert R.sum() == pytest.approx(1.0, abs=1e-9) # mass preserved (folded, not dropped)
    assert len(g.bocpd_R) == cap + 1                    # reached and held the cap


def test_truncation_default_cap_600():
    g = make_gate(p_enter=0.70)  # default max_run_length=600
    drive_warmup(g, wji=1.0, n=25)
    wji_surge = math.exp(2.0 * SIGMA)
    for i in range(700):
        g.update(wji=wji_surge, timestamp=300.0 + i, wji_background=1.0)
    R = g.bocpd_R
    assert len(R) == 601                                # 0..600
    assert R.sum() == pytest.approx(1.0, abs=1e-9)


# ── halt handling ──────────────────────────────────────────────────────

def test_halt_freezes_posterior():
    g = make_gate(p_enter=0.70)
    drive_warmup(g, wji=1.0, n=25)
    wji_surge = math.exp(3.0 * SIGMA)
    for i in range(40):
        g.update(wji=wji_surge, timestamp=300.0 + i, wji_background=1.0)
    R_before = g.bocpd_R.copy()
    p_before = g.p_regime
    st = g.update(wji=math.exp(9.0 * SIGMA), timestamp=340.0, wji_background=1.0, is_halted=True)
    assert st in (GateState.PASS, GateState.FAIL)
    np.testing.assert_array_equal(g.bocpd_R, R_before)   # posterior untouched
    assert g.p_regime == p_before
    # First post-halt tick updates normally (posterior length grows by 1).
    g.update(wji=wji_surge, timestamp=341.0, wji_background=1.0)
    assert len(g.bocpd_R) == len(R_before) + 1


# ── sigma reuse / warmup ───────────────────────────────────────────────

def test_warmup_blocks_pass_and_sigma_fallback():
    g = make_gate(p_enter=0.70, sigma_fallback=0.33)
    for t in (0.0, 50.0, 150.0, 299.0):
        st = g.update(wji=1e6, timestamp=t, wji_background=1.0)  # huge WJI during warmup
        assert st == GateState.WARMUP
    g.update(wji=math.e, timestamp=300.0, wji_background=1.0)     # first post-warmup tick
    assert g.sigma_log == pytest.approx(0.33)                     # fallback (< 20 obs)


def test_zero_background_guard():
    g = make_gate()
    drive_warmup(g, wji=1.0, n=25)
    st = g.update(wji=5.0, timestamp=300.0, wji_background=0.0)   # bg == 0
    assert st in (GateState.PASS, GateState.FAIL)                 # no crash
    st2 = g.update(wji=0.0, timestamp=301.0, wji_background=1.0)  # wji <= 0
    assert st2 in (GateState.PASS, GateState.FAIL)


# ── cusum branch untouched (regression) ────────────────────────────────

def test_cusum_branch_still_works():
    g = ParticipationGate(
        half_life_seconds=300.0, peak_threshold_p=0.65, warmup_seconds=300.0,
        gate_mode="cusum", k=1.0, h=4.0, sigma_log_fallback=1.0,
    )
    g.activate(t_event=0.0)
    for i in range(5):
        g.update(wji=math.e ** 3, timestamp=300.0 + i, wji_background=1.0)
    assert g.s_up == pytest.approx(10.0)  # unchanged CUSUM arithmetic
