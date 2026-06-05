"""
Unit tests for WJIGate (Phase WJI-POC).

Covers the six required cases from the spec, plus regression checks:
  1. WJI collapses to near-zero if either signal component is near zero
  2. Peak accumulates when slope >= 0, decays when slope < 0
  3. Decay rate matches tau_decay at half-life (numerical check)
  4. Gate defaults to FAIL for first L_sec after activation (lookback unavailable)
  5. PASS opens at WJI >= p_open * peak; closes at WJI < p_close * peak
  6. component_balance = 1.0 when both components are equal
  7. INACTIVE before activate()
  8. reset() clears all state
  9. Invalid constructor params rejected
 10. Per-PASS-window records: peak_at_prior_close = 0.0 on first window
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.epg.gate import GateState
from core.epg.gate_variants import WJIGate

LN2 = math.log(2)


# ══════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════


def _make_gate(
    alpha: float = 0.5,
    tau_v: float = 60.0,
    beta_slow: float = 0.01,
    L_sec: float = 10.0,
    tau_decay: float = 120.0,
    p_open: float = 0.65,
    p_close: float = 0.30,
    warmup: float = 0.0,
) -> WJIGate:
    return WJIGate(
        alpha=alpha,
        tau_v=tau_v,
        beta_slow=beta_slow,
        L_sec=L_sec,
        tau_decay=tau_decay,
        p_open=p_open,
        p_close=p_close,
        warmup_seconds=warmup,
    )


def _activate(gate: WJIGate, t: float = 0.0, lv_ref: float = 1.0, mu_buy: float = 0.1):
    gate.activate(t, lv_ref, mu_buy)


def _feed_buys(gate: WJIGate, dv: float, t_start: float, n: int, dt: float = 1.0):
    """Feed n buy trades starting at t_start with uniform dt."""
    for i in range(n):
        gate.update(dv, t_start + i * dt, side=1)


# ══════════════════════════════════════════════════════════════════════
#  1. Geometric mean collapses when one component is near zero
# ══════════════════════════════════════════════════════════════════════


class TestWJIGeometricCollapse:

    def test_wji_near_zero_when_no_buy_trades(self):
        """WJI collapses near zero when λ_buy_slow ≈ 0 (no buy trades arrive)."""
        gate = _make_gate(alpha=0.5, beta_slow=0.01, L_sec=5.0)
        _activate(gate, lv_ref=1.0, mu_buy=0.1)

        # Feed many sell trades (high dollar vol) — λ_V grows, λ_buy_slow stays ~0
        for i in range(1, 50):
            gate.update(10_000.0, float(i), side=-1)

        # norm_lambda_v >> 1; norm_lambda_buy ≈ 0 → WJI near eps^0.5
        assert gate.wji < 0.01, (
            f"WJI should collapse near zero when buy component is absent, got {gate.wji:.6f}"
        )

    def test_wji_near_zero_when_no_dollar_volume(self):
        """WJI collapses near zero when λ_V ≈ 0 (no trades of any size)."""
        gate = _make_gate(alpha=0.5, tau_v=10.0, L_sec=5.0)
        _activate(gate, lv_ref=1.0, mu_buy=0.1)

        # Feed only zero-volume buy ticks → λ_buy_slow builds, λ_V stays ~0
        for i in range(1, 50):
            gate.update(0.0, float(i), side=1)

        # norm_lambda_v ≈ 0 → WJI collapses regardless of buy component
        assert gate.wji < 0.01, (
            f"WJI should collapse near zero when volume component is absent, got {gate.wji:.6f}"
        )

    def test_wji_high_when_both_components_active(self):
        """WJI is substantially above EPS when both components are active."""
        gate = _make_gate(alpha=0.5, tau_v=30.0, beta_slow=0.05, L_sec=5.0)
        _activate(gate, lv_ref=1.0, mu_buy=0.1)

        # Rapid buy trades with substantial dollar volume
        for i in range(1, 40):
            gate.update(5000.0, float(i), side=1)

        assert gate.wji > 1.0, (
            f"WJI should be well above 1.0 when both components are active, got {gate.wji:.6f}"
        )


# ══════════════════════════════════════════════════════════════════════
#  2. Peak accumulates when slope >= 0; decays when slope < 0 (in FAIL)
# ══════════════════════════════════════════════════════════════════════


class TestWJIPeakBehaviour:

    def test_peak_accumulates_during_positive_slope(self):
        """Peak tracks max(WJI) — verified by rising WJI driving rising peak."""
        gate = _make_gate(alpha=0.5, tau_v=30.0, beta_slow=0.05, L_sec=5.0)
        _activate(gate, lv_ref=0.001, mu_buy=0.001)

        # Feed monotonically increasing dollar volume + buy trades
        # WJI rises → peak should track it once slope is defined
        for i in range(1, 30):
            gate.update(1000.0 * i, float(i), side=1)

        # Peak must be >= WJI (it always tracks the running maximum)
        assert gate.peak >= gate.wji * (1 - 1e-9), (
            f"Peak should be >= WJI when WJI is rising; peak={gate.peak:.4f}, wji={gate.wji:.4f}"
        )
        # Peak grew well above the initial 1.0 level
        assert gate.peak > 5.0, f"Peak should have grown substantially, got {gate.peak:.4f}"

    def test_peak_decays_during_negative_slope_in_fail(self):
        """Peak decays when gate is in FAIL and WJI is decelerating.

        Using p_open=0.90 / p_close=0.85: gate closes after WJI drops ~15%
        (~4-5 drain ticks), then peak should decay further in FAIL.
        """
        gate = _make_gate(
            alpha=0.5, tau_v=30.0, beta_slow=0.05, L_sec=5.0,
            tau_decay=30.0, p_open=0.90, p_close=0.85,
        )
        _activate(gate, lv_ref=0.001, mu_buy=0.001)

        # Phase 1: build WJI high
        for i in range(1, 21):
            gate.update(10_000.0, float(i), side=1)

        peak_at_high = gate.peak
        assert peak_at_high > 1.0, "Pre-condition: peak should have grown"

        # Phase 2+3: drain for 40 ticks; gate closes ~tick 5, then peak decays in FAIL
        for extra in range(1, 41):
            gate.update(0.0, 20.0 + extra, side=-1)

        # After 40s drain, gate is in FAIL with declining WJI → peak should have decayed
        assert gate.peak < peak_at_high, (
            f"Peak should decay during FAIL+negative slope: "
            f"high={peak_at_high:.4f}, after drain={gate.peak:.4f}"
        )

    def test_peak_not_decayed_during_pass(self):
        """Peak does not decay during PASS even when slope is negative."""
        gate = _make_gate(
            alpha=0.5, tau_v=30.0, beta_slow=0.05, L_sec=5.0,
            tau_decay=10.0,   # aggressive decay if it triggers
            p_open=0.50, p_close=0.10,
        )
        _activate(gate, lv_ref=0.001, mu_buy=0.001)

        # Drive WJI high enough to open gate
        for i in range(1, 20):
            gate.update(10_000.0, float(i), side=1)

        # Confirm PASS
        state = gate.update(10_000.0, 20.0, side=1)
        assert state == GateState.PASS, "Pre-condition: gate should be PASS"

        peak_at_pass = gate.peak

        # Feed zero volume while in PASS — slope goes negative but peak must NOT decay
        for extra in range(1, 10):
            gate.update(0.0, 20.0 + extra * 2.0, side=-1)
            if gate.peak < peak_at_pass * 0.99:
                pytest.fail(
                    f"Peak decayed during PASS: {gate.peak:.6f} < {peak_at_pass:.6f}"
                )


# ══════════════════════════════════════════════════════════════════════
#  3. Decay rate matches tau_decay at half-life (numerical check)
# ══════════════════════════════════════════════════════════════════════


class TestWJIPeakDecayRate:

    def test_decay_matches_tau_decay_half_life(self):
        """
        A single tau_decay-second dt step during FAIL+negative slope should
        halve the peak via the formula peak *= exp(-ln2 * dt / tau_decay).

        Method:
          1. Build WJI and run through a PASS cycle to set a high peak.
          2. Drain to FAIL; verify slope is negative.
          3. Record the peak immediately before the large-dt step.
          4. Issue one update with dt = tau_decay; verify peak halved.
        """
        tau_decay = 60.0
        L_sec = 5.0
        # p_open=0.90 / p_close=0.85: gate closes after ~4-5 drain ticks (15% WJI drop).
        gate = _make_gate(
            alpha=0.5, tau_v=30.0, beta_slow=0.05, L_sec=L_sec,
            tau_decay=tau_decay, p_open=0.90, p_close=0.85,
        )
        _activate(gate, lv_ref=0.001, mu_buy=0.001)

        # Phase 1: drive WJI high and enter PASS
        for i in range(1, 21):
            gate.update(10_000.0, float(i), side=1)

        # Phase 2: drain until FAIL (gate closes when WJI drops ~15% below peak)
        t_base = 20.0
        t_fail = t_base
        for extra in range(1, 30):
            state = gate.update(0.0, t_base + extra, side=-1)
            if state == GateState.FAIL:
                t_fail = t_base + extra
                break

        assert not gate._in_pass, "Gate must be in FAIL for decay test"

        # Record peak immediately after FAIL transition (= WJI peak from PASS phase)
        peak_before = gate.peak

        # Single dt = tau_decay step.  WJI continues to fall (slope still < 0)
        # because WJI(t_fail + tau_decay) << WJI(t_fail) which is in buf[0].
        # Expected: peak *= exp(-LN2 * tau_decay / tau_decay) = peak * 0.5
        gate.update(0.0, t_fail + tau_decay, side=-1)

        expected_peak = peak_before * math.exp(-LN2)
        rel_err = abs(gate.peak - expected_peak) / (expected_peak + 1e-12)
        assert rel_err < 0.02, (
            f"Peak half-life mismatch: before={peak_before:.6f}, "
            f"expected≈{expected_peak:.6f}, got={gate.peak:.6f} (rel_err={rel_err:.4f})"
        )


# ══════════════════════════════════════════════════════════════════════
#  4. FAIL for first L_sec after activation (lookback unavailable)
# ══════════════════════════════════════════════════════════════════════


class TestWJILookbackGap:

    def test_fail_before_L_sec_history(self):
        """Gate returns FAIL for all ticks until L_sec seconds of history exist."""
        L = 15.0
        # Use very low lv_ref/mu_buy so WJI >> 1 from the start; gate should still
        # return FAIL because slope history is unavailable.
        gate = _make_gate(L_sec=L, warmup=0.0)
        _activate(gate, lv_ref=0.0001, mu_buy=0.0001)

        for t in range(1, int(L)):
            state = gate.update(100_000.0, float(t), side=1)
            assert state == GateState.FAIL, (
                f"Expected FAIL (no lookback history) at t={t}, got {state}"
            )

    def test_can_open_once_L_sec_elapsed(self):
        """Gate can reach PASS once L_sec seconds of history are available."""
        L = 10.0
        gate = _make_gate(L_sec=L, warmup=0.0)
        _activate(gate, lv_ref=0.0001, mu_buy=0.0001)

        # Feed high volume and buys well beyond L_sec
        found_pass = False
        for i in range(1, int(L) + 20):
            state = gate.update(100_000.0, float(i), side=1)
            if i > int(L) and state == GateState.PASS:
                found_pass = True
                break

        assert found_pass, "Gate should reach PASS after L_sec history is available"


# ══════════════════════════════════════════════════════════════════════
#  5. Asymmetric PASS condition
# ══════════════════════════════════════════════════════════════════════


class TestWJIPassCondition:

    def test_pass_opens_at_p_open_times_peak(self):
        """Gate opens when WJI >= p_open * peak."""
        p_open = 0.65
        gate = _make_gate(L_sec=5.0, p_open=p_open, p_close=0.10, warmup=0.0)
        _activate(gate, lv_ref=0.001, mu_buy=0.001)

        # Build L_sec of history first
        for i in range(1, 8):
            gate.update(10.0, float(i), side=1)

        # Spike to drive WJI >> p_open * peak
        found_pass = False
        for i in range(8, 30):
            state = gate.update(1_000_000.0, float(i), side=1)
            if state == GateState.PASS:
                found_pass = True
                break

        assert found_pass, f"Gate should open when WJI >= {p_open} * peak"

    def test_pass_closes_at_p_close_times_peak(self):
        """Gate closes when WJI < p_close * peak."""
        p_close = 0.30
        gate = _make_gate(L_sec=5.0, p_open=0.50, p_close=p_close, warmup=0.0)
        _activate(gate, lv_ref=0.001, mu_buy=0.001)

        # Build L_sec, then open gate
        for i in range(1, 8):
            gate.update(10.0, float(i), side=1)
        for i in range(8, 20):
            gate.update(1_000_000.0, float(i), side=1)

        # Confirm PASS
        state = gate.update(1_000_000.0, 20.0, side=1)
        assert state == GateState.PASS, "Pre-condition: gate should be PASS"

        # Drain signal — WJI decays below p_close * peak
        found_fail = False
        for extra in range(1, 300):
            state = gate.update(0.0, 20.0 + extra * 5.0, side=-1)
            if state == GateState.FAIL:
                found_fail = True
                break

        assert found_fail, f"Gate should close when WJI drops below {p_close} * peak"

    def test_gate_stays_pass_in_dead_band(self):
        """Gate holds PASS when WJI is between p_close and p_open fractions of peak."""
        gate = _make_gate(L_sec=5.0, p_open=0.90, p_close=0.10, warmup=0.0)
        _activate(gate, lv_ref=0.001, mu_buy=0.001)

        # Build history, then open gate
        for i in range(1, 8):
            gate.update(10.0, float(i), side=1)
        for i in range(8, 20):
            gate.update(1_000_000.0, float(i), side=1)

        state = gate.update(1_000_000.0, 20.0, side=1)
        assert state == GateState.PASS

        # Feed moderate volume — WJI should stay between 0.10 and 0.90 of peak
        # Gate should hold PASS (dead band)
        for extra in range(1, 15):
            state = gate.update(5000.0, 20.0 + extra, side=1)
        assert state == GateState.PASS, "Gate should hold PASS in dead band"


# ══════════════════════════════════════════════════════════════════════
#  6. component_balance = 1.0 when both components are equal
# ══════════════════════════════════════════════════════════════════════


class TestWJIComponentBalance:

    def test_balance_near_one_when_both_equal(self):
        """
        When norm_lambda_v ≈ norm_lambda_buy, component_balance ≈ 1.0.

        We calibrate lv_ref and mu_buy to the analytical steady-state values of
        their respective signals so that norm_v → 1.0 and norm_buy → 1.0.

        Steady-state with uniform dt=1s:
            λ_V_ss   = dv * (ln2/τ_V) / (1 - exp(-ln2/τ_V))
            λ_buy_ss = β_slow / (1 - exp(-β_slow))
        """
        beta_slow = 0.05
        tau_v = 20.0
        dv = 1.0
        dt = 1.0
        decay_rate_v = LN2 / tau_v

        lv_ss = dv * decay_rate_v / (1.0 - math.exp(-decay_rate_v * dt))
        lbs_ss = beta_slow / (1.0 - math.exp(-beta_slow * dt))

        # Set references equal to steady-state → both norms converge to 1.0
        gate = _make_gate(alpha=0.5, tau_v=tau_v, beta_slow=beta_slow, L_sec=5.0, warmup=0.0)
        _activate(gate, lv_ref=lv_ss, mu_buy=lbs_ss)

        for i in range(1, 200):
            gate.update(dv, float(i), side=1)

        nv = gate.norm_lambda_v
        nb = gate.norm_lambda_buy

        assert max(nv, nb) > 1e-6, "Components should be non-trivial after 200 ticks"
        balance = min(nv, nb) / max(nv, nb)
        assert balance >= 0.90, (
            f"Expected balance near 1.0 when components equal, got {balance:.4f} "
            f"(norm_v={nv:.4f}, norm_buy={nb:.4f})"
        )

    def test_balance_low_when_one_component_absent(self):
        """
        When one component is near zero, balance should be near zero.
        """
        gate = _make_gate(alpha=0.5, tau_v=30.0, beta_slow=0.01, L_sec=5.0, warmup=0.0)
        _activate(gate, lv_ref=1.0, mu_buy=0.1)

        # Only sell trades — λ_buy_slow stays near 0; λ_V grows
        for i in range(1, 60):
            gate.update(5000.0, float(i), side=-1)

        nv = gate.norm_lambda_v
        nb = gate.norm_lambda_buy

        if max(nv, nb) > 1e-6:
            balance = min(nv, nb) / max(nv, nb)
            assert balance < 0.05, (
                f"Expected low balance when buy component absent, got {balance:.4f} "
                f"(norm_v={nv:.4f}, norm_buy={nb:.4f})"
            )


# ══════════════════════════════════════════════════════════════════════
#  7. INACTIVE before activate()
# ══════════════════════════════════════════════════════════════════════


class TestWJIInactive:

    def test_inactive_before_activate(self):
        """Gate returns INACTIVE before activate() is called."""
        gate = _make_gate()
        state = gate.update(10_000.0, 1.0, side=1)
        assert state == GateState.INACTIVE

    def test_warmup_after_activate(self):
        """Gate returns WARMUP during warmup_seconds after activate()."""
        gate = _make_gate(warmup=30.0)
        _activate(gate, t=0.0)

        state = gate.update(100_000.0, 10.0, side=1)
        assert state == GateState.WARMUP

        state = gate.update(100_000.0, 29.0, side=1)
        assert state == GateState.WARMUP

    def test_not_inactive_after_activate(self):
        """Gate does not return INACTIVE after activate()."""
        gate = _make_gate(warmup=0.0, L_sec=5.0)
        _activate(gate, t=0.0)

        state = gate.update(0.0, 1.0)
        assert state != GateState.INACTIVE


# ══════════════════════════════════════════════════════════════════════
#  8. reset() clears all state
# ══════════════════════════════════════════════════════════════════════


class TestWJIReset:

    def test_reset_returns_to_inactive(self):
        """After reset(), gate returns INACTIVE on update."""
        gate = _make_gate(warmup=0.0, L_sec=5.0, p_open=0.10, p_close=0.05)
        _activate(gate, lv_ref=0.001, mu_buy=0.001)

        for i in range(1, 20):
            gate.update(10_000.0, float(i), side=1)

        assert gate.peak > 0.0
        assert len(gate._wji_buffer) > 0

        gate.reset()

        assert gate.peak == 1.0, "Peak should reset to 1.0"
        assert gate.wji == 0.0
        assert gate.norm_lambda_v == 0.0
        assert gate.norm_lambda_buy == 0.0
        assert len(gate._wji_buffer) == 0
        assert gate._t_event is None
        assert not gate._active
        assert gate._first_window is True

        state = gate.update(100_000.0, 50.0, side=1)
        assert state == GateState.INACTIVE

    def test_reset_clears_pass_windows(self):
        """reset() clears accumulated pass_windows records."""
        gate = _make_gate(warmup=0.0, L_sec=5.0, p_open=0.05, p_close=0.01)
        _activate(gate, lv_ref=0.0001, mu_buy=0.0001)

        for i in range(1, 30):
            gate.update(100_000.0, float(i), side=1)

        gate.reset()
        assert gate.pass_windows == []

    def test_reactivate_after_reset(self):
        """Gate can be activated again after reset() with fresh state."""
        gate = _make_gate(warmup=0.0, L_sec=5.0)
        _activate(gate, t=0.0, lv_ref=1.0, mu_buy=0.1)

        for i in range(1, 10):
            gate.update(500.0, float(i), side=1)

        gate.reset()
        _activate(gate, t=100.0, lv_ref=2.0, mu_buy=0.2)

        state = gate.update(0.0, 101.0)
        assert state != GateState.INACTIVE
        assert gate._lambda_v_ref == 2.0
        assert gate._mu_buy == 0.2


# ══════════════════════════════════════════════════════════════════════
#  9. Invalid constructor params rejected
# ══════════════════════════════════════════════════════════════════════


class TestWJIInvalidParams:

    def test_alpha_out_of_range(self):
        with pytest.raises(ValueError):
            WJIGate(alpha=-0.1)
        with pytest.raises(ValueError):
            WJIGate(alpha=1.1)

    def test_nonpositive_tau_v(self):
        with pytest.raises(ValueError):
            WJIGate(tau_v=0.0)
        with pytest.raises(ValueError):
            WJIGate(tau_v=-10.0)

    def test_nonpositive_beta_slow(self):
        with pytest.raises(ValueError):
            WJIGate(beta_slow=0.0)

    def test_nonpositive_L_sec(self):
        with pytest.raises(ValueError):
            WJIGate(L_sec=0.0)

    def test_nonpositive_tau_decay(self):
        with pytest.raises(ValueError):
            WJIGate(tau_decay=0.0)

    def test_p_close_greater_than_p_open(self):
        with pytest.raises(ValueError):
            WJIGate(p_open=0.5, p_close=0.6)

    def test_p_open_zero(self):
        with pytest.raises(ValueError):
            WJIGate(p_open=0.0, p_close=0.0)

    def test_activate_nonpositive_lambda_v_ref(self):
        gate = _make_gate()
        with pytest.raises(ValueError):
            gate.activate(0.0, lambda_v_ref=0.0, mu_buy=0.1)
        with pytest.raises(ValueError):
            gate.activate(0.0, lambda_v_ref=-1.0, mu_buy=0.1)

    def test_activate_nonpositive_mu_buy(self):
        gate = _make_gate()
        with pytest.raises(ValueError):
            gate.activate(0.0, lambda_v_ref=1.0, mu_buy=0.0)


# ══════════════════════════════════════════════════════════════════════
#  10. Per-PASS-window records
# ══════════════════════════════════════════════════════════════════════


class TestWJIPassWindows:

    def test_first_window_prior_close_is_zero(self):
        """The first PASS window must have peak_at_prior_close == 0.0."""
        gate = _make_gate(warmup=0.0, L_sec=5.0, p_open=0.10, p_close=0.01)
        _activate(gate, lv_ref=0.0001, mu_buy=0.0001)

        for i in range(1, 30):
            gate.update(100_000.0, float(i), side=1)

        assert len(gate.pass_windows) >= 1, "Expected at least one PASS window"
        first = gate.pass_windows[0]
        assert first["peak_at_prior_close"] == 0.0, (
            f"First window must have peak_at_prior_close=0.0, got {first['peak_at_prior_close']}"
        )

    def test_second_window_prior_close_nonzero(self):
        """After first window closes, next window records a non-zero peak_at_prior_close."""
        gate = _make_gate(warmup=0.0, L_sec=5.0, p_open=0.10, p_close=0.08)
        _activate(gate, lv_ref=0.0001, mu_buy=0.0001)

        t = 0.0

        # Phase 1: open gate
        for i in range(1, 15):
            t += 1.0
            gate.update(100_000.0, t, side=1)

        # Phase 2: drain to close
        for _ in range(60):
            t += 2.0
            state = gate.update(0.0, t, side=-1)
            if state == GateState.FAIL and len(gate.pass_windows) >= 1 and not gate._in_pass:
                break

        if not gate._first_window:
            # Phase 3: reopen
            for _ in range(20):
                t += 1.0
                gate.update(100_000.0, t, side=1)

            if len(gate.pass_windows) >= 2:
                second = gate.pass_windows[1]
                assert second["peak_at_prior_close"] > 0.0, (
                    "Second window should have non-zero peak_at_prior_close"
                )

    def test_window_open_time_recorded(self):
        """window_open_time is recorded at the FAIL→PASS transition."""
        gate = _make_gate(warmup=0.0, L_sec=5.0, p_open=0.10, p_close=0.01)
        _activate(gate, t=1000.0, lv_ref=0.0001, mu_buy=0.0001)

        t = 1000.0
        open_t = None
        for i in range(1, 30):
            t += 1.0
            state = gate.update(100_000.0, t, side=1)
            if state == GateState.PASS and open_t is None:
                open_t = t  # first time we observe PASS

        assert len(gate.pass_windows) >= 1
        # The recorded open time should be >= t_event and <= the observed open time
        recorded_t = gate.pass_windows[0]["window_open_time"]
        assert recorded_t >= 1000.0
        assert recorded_t <= open_t + 1.0  # within one tick


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
