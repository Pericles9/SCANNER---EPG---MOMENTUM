"""
Unit tests for the CUSUM gate branch (gate_mode="cusum") of ParticipationGate.
Phase CPD-1, Task T5d.

The 11 required cases from the Phase CPD plan. All tests use sigma_log_fallback=1.0
so that `deviation == wji_log` and the arithmetic is transparent; warmup is driven by
timestamps relative to t_event=0.0 (warmup window = [0, warmup_seconds)).
"""
from __future__ import annotations

import math

import pytest

from core.epg.gate import ParticipationGate, GateState


def make_gate(k=1.0, h=4.0, sigma_fallback=1.0, warmup=300.0):
    """A cusum-mode gate activated at t_event=0.0."""
    g = ParticipationGate(
        half_life_seconds=300.0, peak_threshold_p=0.65, warmup_seconds=warmup,
        gate_mode="cusum", k=k, h=h, sigma_log_fallback=sigma_fallback,
    )
    g.activate(t_event=0.0)
    return g


def drive_warmup_exit(g, wji=1.0, bg=1.0, n_warmup=0, t_warmup_step=5.0):
    """
    Optionally feed n_warmup ticks inside the warmup window, then return the gate
    poised at the warmup boundary (caller issues the first post-warmup tick).
    """
    for i in range(n_warmup):
        g.update(wji=wji, timestamp=float(i) * t_warmup_step, wji_background=bg)


# ── log-ratio transform ────────────────────────────────────────────────

def test_log_ratio_at_background():
    g = make_gate()
    g.update(wji=5.0, timestamp=300.0, wji_background=5.0)  # WJI == background
    assert g.last_cusum_debug["wji_log"] == pytest.approx(0.0, abs=1e-12)


def test_log_ratio_elevated():
    g = make_gate()
    g.update(wji=10.0, timestamp=300.0, wji_background=1.0)  # 10× background
    assert g.last_cusum_debug["wji_log"] == pytest.approx(math.log(10.0), abs=1e-6)


# ── accumulation / drain ───────────────────────────────────────────────

def test_accumulator_builds():
    # wji_log = 3, k = 1 → net +2 per tick; high h so it never caps to PASS-only.
    g = make_gate(k=1.0, h=1000.0, sigma_fallback=1.0)
    wji = math.e ** 3
    prev = -1.0
    for i in range(20):
        g.update(wji=wji, timestamp=300.0 + i, wji_background=1.0)
        assert g.s_up > prev          # strictly increasing
        prev = g.s_up
    assert g.s_up == pytest.approx(40.0)  # 20 ticks × (+2)


def test_accumulator_drains():
    g = make_gate(k=1.0, h=4.0, sigma_fallback=1.0)
    for i in range(5):                # build well past h
        g.update(wji=math.e ** 3, timestamp=300.0 + i, wji_background=1.0)
    assert g.s_up > 0.0
    for i in range(50):               # feed background → drains by k each tick
        g.update(wji=1.0, timestamp=305.0 + i, wji_background=1.0)
    assert g.s_up == 0.0


# ── state transitions ──────────────────────────────────────────────────

def test_pass_triggers_at_h():
    # wji_log = 2, k = 1 → net +1 per tick. PASS must hold exactly when s_up > h.
    g = make_gate(k=1.0, h=5.0, sigma_fallback=1.0)
    wji = math.e ** 2
    for i in range(12):
        st = g.update(wji=wji, timestamp=300.0 + i, wji_background=1.0)
        if g.s_up > 5.0:
            assert st == GateState.PASS
        else:
            assert st == GateState.FAIL


def test_fail_on_drain():
    g = make_gate(k=1.0, h=4.0, sigma_fallback=1.0)
    for i in range(5):
        g.update(wji=math.e ** 3, timestamp=300.0 + i, wji_background=1.0)
    assert g._in_pass is True         # PASS achieved
    last_state = GateState.PASS
    for i in range(50):
        last_state = g.update(wji=1.0, timestamp=305.0 + i, wji_background=1.0)
    assert g.s_up == 0.0
    assert last_state == GateState.FAIL


def test_warmup_blocks_pass():
    g = make_gate(warmup=300.0, sigma_fallback=1.0)
    for t in (0.0, 50.0, 150.0, 299.0):
        st = g.update(wji=1e6, timestamp=t, wji_background=1.0)  # huge WJI
        assert st == GateState.WARMUP
    assert g.s_up == 0.0              # nothing accumulated during warmup


# ── sigma estimation ───────────────────────────────────────────────────

def test_sigma_fallback():
    g = make_gate(sigma_fallback=0.5, warmup=300.0)
    drive_warmup_exit(g, wji=2.0, bg=1.0, n_warmup=5)   # < 20 warmup obs
    g.update(wji=2.0, timestamp=300.0, wji_background=1.0)  # first post-warmup tick
    assert g.sigma_log == pytest.approx(0.5)


def test_pre_event_ticks_excluded_from_sigma():
    # A full-trace replay feeds pre-event ticks (timestamp < t_event). They must NOT
    # enter the warmup sample. Here pre-event ticks are wild (huge spread); the warmup
    # window itself is constant (zero variance → sigma falls back). If pre-event ticks
    # leaked in, sigma would be large, not the fallback.
    g = ParticipationGate(
        half_life_seconds=300.0, peak_threshold_p=0.65, warmup_seconds=300.0,
        gate_mode="cusum", k=1.0, h=4.0, sigma_log_fallback=0.5,
    )
    g.activate(t_event=1000.0)
    for i in range(30):  # pre-event, wildly varying WJI
        st = g.update(wji=math.e ** (10 * (i % 2)), timestamp=float(i * 10), wji_background=1.0)
        assert st == GateState.WARMUP
    for i in range(25):  # warmup window [1000,1300): constant → zero variance
        g.update(wji=math.e, timestamp=1000.0 + i, wji_background=1.0)
    g.update(wji=math.e, timestamp=1300.0, wji_background=1.0)  # finalise sigma
    assert g.sigma_log == pytest.approx(0.5)  # fallback, untainted by pre-event spread


def test_sigma_estimated_from_warmup():
    # ≥ 20 warmup obs with non-zero spread → sigma_log is the sample std, not fallback.
    g = make_gate(sigma_fallback=99.0, warmup=300.0)
    # alternate two WJI levels so log-ratios have a known, finite spread
    for i in range(40):
        wji = math.e if i % 2 == 0 else math.e ** 2   # wji_log alternates 1.0 / 2.0
        g.update(wji=wji, timestamp=float(i), wji_background=1.0)
    g.update(wji=math.e, timestamp=300.0, wji_background=1.0)
    assert g.sigma_log is not None and g.sigma_log != pytest.approx(99.0)
    assert g.sigma_log == pytest.approx(0.5063, abs=1e-3)  # std of 20×{1.0} + 20×{2.0}


# ── halt handling ──────────────────────────────────────────────────────

def test_halt_holds_s_up():
    g = make_gate(k=1.0, h=1000.0, sigma_fallback=1.0)
    for i in range(5):
        g.update(wji=math.e ** 3, timestamp=300.0 + i, wji_background=1.0)
    s_before = g.s_up
    st = g.update(wji=math.e ** 5, timestamp=310.0, wji_background=1.0, is_halted=True)
    assert g.s_up == s_before          # untouched during halt
    assert st in (GateState.PASS, GateState.FAIL)


# ── no reset between PASS/FAIL cycles ──────────────────────────────────

def test_no_reset_between_cycles():
    # Build to PASS, then feed background: s_up must drain by exactly k each tick
    # (continuous), never snapping to 0 on the state change.
    g = make_gate(k=1.0, h=4.0, sigma_fallback=1.0)
    for i in range(5):                 # s_up = 2,4,6,8,10
        g.update(wji=math.e ** 3, timestamp=300.0 + i, wji_background=1.0)
    assert g.s_up == pytest.approx(10.0)
    g.update(wji=1.0, timestamp=305.0, wji_background=1.0)
    assert g.s_up == pytest.approx(9.0)   # -k, not reset to 0
    g.update(wji=1.0, timestamp=306.0, wji_background=1.0)
    assert g.s_up == pytest.approx(8.0)


# ── zero-background guard ───────────────────────────────────────────────

def test_zero_background_guard():
    g = make_gate(sigma_fallback=1.0)
    st = g.update(wji=5.0, timestamp=300.0, wji_background=0.0)  # bg == 0
    assert st in (GateState.PASS, GateState.FAIL)   # no crash
    assert g.s_up == 0.0                            # untouched
    # WJI <= 0 is likewise guarded
    st2 = g.update(wji=0.0, timestamp=301.0, wji_background=1.0)
    assert st2 in (GateState.PASS, GateState.FAIL)
    assert g.s_up == 0.0
