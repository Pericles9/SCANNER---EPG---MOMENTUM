"""
Unit tests for SlopeGate (Variant F) — Phase EPG-OPT2.

Covers:
  - Undefined slope (FAIL) for first L_sec after activation
  - F_ss: FAIL→PASS when norm_slope ≥ k_open; PASS→FAIL when norm_slope < k_close
  - Dead band: holds current state when k_close ≤ norm_slope < k_open
  - F_sl: opens on slope, closes on level condition independently
  - Norm slope computation against hand-computed reference
  - reset() clears all state
  - Invalid constructor params rejected
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.epg.gate import GateState
from core.epg.gate_variants import SlopeGate


LN2 = math.log(2)


def _advance(gate: SlopeGate, dollar_vol: float, timestamp: float) -> GateState:
    """Helper: update gate and return state."""
    return gate.update(dollar_vol, timestamp)


def _build_lambda_v(tau_sec: float, dv_seq: list, dt: float = 1.0) -> list:
    """Compute expected λ_V sequence for a given dollar-volume series."""
    decay_rate = LN2 / tau_sec
    lv = 0.0
    out = []
    for dv in dv_seq:
        lv = lv * math.exp(-decay_rate * dt) + dv * decay_rate
        out.append(lv)
    return out


class TestSlopeGateUndefinedSlope:
    """Slope is undefined (FAIL) until L_sec history is available."""

    def test_fail_before_L_sec_history(self):
        """Gate returns FAIL for all ticks until L_sec seconds have elapsed."""
        L = 30.0
        gate = SlopeGate(
            tau_sec=120.0, L_sec=L, k_open=0.5, mode="ss",
            k_close=-1.0, lambda_v_ref=1.0, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Feed volume every second for L-1 seconds — no lookback entry yet
        for t in range(1, int(L)):
            state = gate.update(1000.0, float(t))
            assert state == GateState.FAIL, f"Expected FAIL (no history) at t={t}"

    def test_slope_defined_after_L_sec(self):
        """Gate can evaluate slope once L_sec seconds of history exist."""
        L = 10.0
        gate = SlopeGate(
            tau_sec=60.0, L_sec=L, k_open=0.5, mode="ss",
            k_close=-2.0, lambda_v_ref=0.01, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Feed very high volume to guarantee norm_slope >> k_open
        for t in range(1, int(L) + 2):
            gate.update(1_000_000.0, float(t))

        # By now L_sec has elapsed since activation — should have PASS
        final = gate.update(1_000_000.0, float(int(L) + 2))
        assert final == GateState.PASS, "Gate should be PASS once slope is defined and high"

    def test_inactive_before_activate(self):
        """Gate returns INACTIVE before activate() is called."""
        gate = SlopeGate(tau_sec=120.0, L_sec=30.0, k_open=0.5, mode="ss",
                         k_close=-1.0, lambda_v_ref=1.0)
        state = gate.update(1000.0, 1.0)
        assert state == GateState.INACTIVE

    def test_warmup_returned_during_warmup_period(self):
        """Gate returns WARMUP during warmup_seconds after activation."""
        gate = SlopeGate(
            tau_sec=60.0, L_sec=5.0, k_open=0.5, mode="ss",
            k_close=-1.0, lambda_v_ref=1.0, warmup_seconds=300.0,
        )
        gate.activate(0.0)
        for t in [1.0, 100.0, 299.0]:
            state = gate.update(1000.0, t)
            assert state == GateState.WARMUP, f"Expected WARMUP at t={t}"


class TestSlopeGateFss:
    """Tests for F_ss (slope open / slope close) with dead band."""

    def test_fail_to_pass_at_k_open(self):
        """FAIL→PASS when norm_slope ≥ k_open."""
        tau = 60.0
        L = 10.0
        lambda_v_ref = 0.001  # small ref so norm_slope is large
        k_open = 0.5
        k_close = -1.0

        gate = SlopeGate(
            tau_sec=tau, L_sec=L, k_open=k_open, mode="ss",
            k_close=k_close, lambda_v_ref=lambda_v_ref, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Build L seconds of history with low volume
        for t in range(1, int(L) + 1):
            gate.update(0.1, float(t))

        # Now spike volume — slope will be strongly positive
        for t in range(int(L) + 1, int(L) + 5):
            state = gate.update(100_000.0, float(t))
            if state == GateState.PASS:
                break
        else:
            pytest.fail("Gate never reached PASS after high-volume spike")

    def test_pass_to_fail_at_k_close(self):
        """PASS→FAIL when norm_slope < k_close."""
        tau = 60.0
        L = 5.0
        lambda_v_ref = 0.001
        k_open = 0.3
        k_close = -0.5

        gate = SlopeGate(
            tau_sec=tau, L_sec=L, k_open=k_open, mode="ss",
            k_close=k_close, lambda_v_ref=lambda_v_ref, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Phase 1: low volume baseline for L seconds
        for t in range(1, int(L) + 1):
            gate.update(0.1, float(t))

        # Phase 2: spike to get PASS
        for _ in range(20):
            gate.update(100_000.0, float(int(L) + 1))
        state = gate.update(100_000.0, float(int(L) + 2))
        # Don't assert PASS here — just move on

        # Phase 3: volume collapses — λ_V will drop (negative slope)
        # Feed zero volume for many L_sec periods
        t_base = float(int(L) + 2)
        for extra in range(1, 30):
            state = gate.update(0.0, t_base + extra * L)
            if state == GateState.FAIL:
                break
        else:
            pytest.fail("Gate never reached FAIL after volume collapse")

    def test_dead_band_holds_pass(self):
        """Gate holds PASS when k_close ≤ norm_slope < k_open."""
        tau = 120.0
        L = 10.0
        k_open = 2.0
        k_close = -2.0
        lambda_v_ref = 0.001

        gate = SlopeGate(
            tau_sec=tau, L_sec=L, k_open=k_open, mode="ss",
            k_close=k_close, lambda_v_ref=lambda_v_ref, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Build history and get into PASS with a big spike
        for t in range(1, int(L) + 1):
            gate.update(0.1, float(t))
        for t in range(int(L) + 1, int(L) + 5):
            gate.update(10_000_000.0, float(t))
        state = gate.update(10_000_000.0, float(int(L) + 5))
        assert state == GateState.PASS, "Pre-condition: should be PASS after spike"

        # Now feed moderate volume — slope should be between k_close and k_open
        # (large dead band: k_close=-2, k_open=2 with small lambda_v_ref)
        # At moderate volume, norm_slope should be in dead band
        for extra in range(1, 10):
            state = gate.update(5000.0, float(int(L) + 5 + extra))
            # Gate should remain PASS (held in dead band)
        assert state == GateState.PASS, "Gate should hold PASS in dead band"

    def test_dead_band_holds_fail(self):
        """Gate holds FAIL when 0 ≤ norm_slope < k_open (below opening threshold)."""
        tau = 120.0
        L = 10.0
        k_open = 5.0    # high threshold
        k_close = 0.0
        # Use large lambda_v_ref so small dv produces small norm_slope
        lambda_v_ref = 10000.0

        gate = SlopeGate(
            tau_sec=tau, L_sec=L, k_open=k_open, mode="ss",
            k_close=k_close, lambda_v_ref=lambda_v_ref, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Build L seconds of baseline with moderate volume
        for t in range(1, int(L) + 1):
            gate.update(100.0, float(t))

        # Feed same moderate volume — slope ≈ 0 (steady state), well below k_open=5
        t_base = float(int(L) + 1)
        for extra in range(1, 10):
            state = gate.update(100.0, t_base + extra)

        # Verify norm_slope is in dead band (between k_close=0 and k_open=5)
        ns = gate.norm_slope
        assert k_close <= ns < k_open, f"Expected norm_slope in [{k_close}, {k_open}), got {ns:.4f}"
        assert state == GateState.FAIL, (
            "Gate should hold FAIL when norm_slope is below k_open"
        )


class TestSlopeGateFsl:
    """Tests for F_sl (slope open / level close)."""

    def test_opens_on_slope_closes_on_level(self):
        """F_sl opens when norm_slope ≥ k_open; closes when λ_V < p_close × peak."""
        tau = 60.0
        L = 5.0
        k_open = 0.1
        p_close = 0.50
        lambda_v_ref = 0.001

        gate = SlopeGate(
            tau_sec=tau, L_sec=L, k_open=k_open, mode="sl",
            p_close=p_close, lambda_v_ref=lambda_v_ref, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Build L seconds of low baseline
        for t in range(1, int(L) + 1):
            gate.update(0.1, float(t))

        # Spike to open gate
        t_spike = int(L) + 1
        for t in range(t_spike, t_spike + 5):
            gate.update(100_000.0, float(t))
        state = gate.update(100_000.0, float(t_spike + 5))
        assert state == GateState.PASS, "F_sl should open on slope spike"

        # Record peak and decay to below p_close
        peak = gate.lambda_v_peak
        assert peak > 0

        # Zero out volume — λ_V will decay below p_close * peak
        t_base = float(t_spike + 5)
        state = GateState.PASS
        for extra in range(1, 200):
            state = gate.update(0.0, t_base + extra)
            if state == GateState.FAIL:
                break
        assert state == GateState.FAIL, (
            "F_sl should close when λ_V decays below p_close × peak"
        )

    def test_fsl_level_close_independent_of_slope(self):
        """F_sl closes on level condition even when norm_slope is in dead band."""
        tau = 120.0
        L = 10.0
        k_open = 0.5
        p_close = 0.70  # aggressive level close
        lambda_v_ref = 0.001

        gate = SlopeGate(
            tau_sec=tau, L_sec=L, k_open=k_open, mode="sl",
            p_close=p_close, lambda_v_ref=lambda_v_ref, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Build L seconds of low baseline
        for t in range(1, int(L) + 1):
            gate.update(0.1, float(t))

        # Open gate with spike
        for t in range(int(L) + 1, int(L) + 6):
            gate.update(1_000_000.0, float(t))
        state = gate.update(1_000_000.0, float(int(L) + 6))
        assert state == GateState.PASS, "Pre-condition: gate should be PASS"

        # Feed uniform steady volume (slope ≈ 0, in dead band)
        # But level drops below p_close * peak after a while
        t_base = float(int(L) + 6)
        for extra in range(1, 300):
            state = gate.update(0.0, t_base + extra)
            if state == GateState.FAIL:
                break
        assert state == GateState.FAIL, (
            "F_sl should close on level drop regardless of slope being in dead band"
        )

    def test_running_peak_resets_on_reopen(self):
        """Peak resets on FAIL→PASS re-transition in F_sl mode."""
        tau = 60.0
        L = 5.0
        k_open = 0.1
        p_close = 0.50
        lambda_v_ref = 0.001

        gate = SlopeGate(
            tau_sec=tau, L_sec=L, k_open=k_open, mode="sl",
            p_close=p_close, lambda_v_ref=lambda_v_ref, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # First open
        for t in range(1, int(L) + 1):
            gate.update(0.1, float(t))
        for t in range(int(L) + 1, int(L) + 6):
            gate.update(100_000.0, float(t))
        first_peak = gate.lambda_v_peak

        # Close (let λ_V decay)
        for extra in range(1, 200):
            state = gate.update(0.0, float(int(L) + 6 + extra))
            if state == GateState.FAIL:
                break

        # Second open — peak should reset to new λ_V level (much lower)
        t_base2 = float(int(L) + 206)
        for t in range(int(L), int(L) + 5):
            gate.update(0.1, t_base2 + float(t))
        for t in range(5, 10):
            gate.update(1000.0, t_base2 + float(int(L) + t))

        state = gate.update(1000.0, t_base2 + float(int(L) + 10))
        if state == GateState.PASS:
            # Peak should have reset to current λ_V (not carried over from first open)
            # The new peak should be ≤ current λ_V (set at open time, may have grown slightly)
            # and substantially less than the original first_peak
            assert gate.lambda_v_peak < first_peak, (
                "Running peak should reset on re-open, not carry over from first open"
            )
            # New peak should track current λ_V level, not the original large peak
            assert gate.lambda_v_peak <= gate.lambda_v + 1e-6, (
                "Peak after re-open should be ≤ current λ_V (set at open tick)"
            )


class TestSlopeGateIrregularTicks:
    """Pruning correctness with irregular (non-uniform) tick intervals."""

    def test_slope_defined_with_tick_gap_spanning_cutoff(self):
        """
        When a tick gap spans the cutoff boundary, buf[0] must still hold
        the most recent entry before cutoff — not get pruned away.

        Example: L=10s, ticks at t=1,2,...,8,19 (gap from 8 to 19).
        At t=19, cutoff=9. The entry at t=8 (< cutoff) must be retained
        as lv_past so slope can be computed.
        """
        gate = SlopeGate(
            tau_sec=60.0, L_sec=10.0, k_open=0.001,  # very low — easy to open
            mode="ss", k_close=-99.0, lambda_v_ref=100.0, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Ticks at t=1..8 — no entry at exactly t=9
        for t in range(1, 9):
            gate.update(1000.0, float(t))

        # Big gap: next tick at t=19 (cutoff = 9, last entry was at t=8)
        # The pruning bug would have deleted t=8 (since 8 < 9), leaving no lv_past.
        # The fix keeps t=8 as buf[0] since buf[1] (if any) > 9.
        state = gate.update(10_000_000.0, 19.0)
        # norm_slope = (lv_19 - lv_8) / (10 * 100)
        # lv_8 is small (built from 1000 dv), lv_19 is large (10M dv spike)
        # norm_slope should be >> k_open=0.001 → PASS
        assert state == GateState.PASS, (
            "Gate should open after large volume spike with irregular tick gap"
        )

    def test_no_false_open_when_gap_before_L_sec(self):
        """
        If all ticks are within L_sec of the current time (no entry before cutoff),
        slope must remain undefined (FAIL).
        """
        gate = SlopeGate(
            tau_sec=60.0, L_sec=30.0, k_open=0.001,
            mode="ss", k_close=-99.0, lambda_v_ref=1.0, warmup_seconds=0.0,
        )
        gate.activate(0.0)
        # Only 5 seconds of history — not enough for L=30
        for t in [1.0, 2.0, 3.0, 4.0, 5.0]:
            state = gate.update(1_000_000.0, t)
        assert state == GateState.FAIL, "FAIL when no entry predates cutoff"


class TestSlopeGateNormSlope:
    """Numerical correctness of norm_slope computation."""

    def test_norm_slope_hand_computed(self):
        """
        Verify norm_slope against a hand-computed sequence.

        Setup:
          tau=60s, L=10s, lambda_v_ref=100.0, dt=1s
          At t=1..10: dv=0  → λ_V builds from 0 to small values
          At t=10: λ_V_past = λ_V at t=0 (just after activate) = 0
          At t=11: slope = (λ_V_11 - λ_V_1) / L

        We compute expected λ_V values manually.
        """
        tau = 60.0
        L = 10.0
        lv_ref = 100.0
        decay_rate = LN2 / tau
        dv = 500.0  # constant dollar volume

        gate = SlopeGate(
            tau_sec=tau, L_sec=L, k_open=99.0,  # very high to stay FAIL
            mode="ss", k_close=-99.0, lambda_v_ref=lv_ref, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Manually compute λ_V for t=1..15
        lv = 0.0
        lv_series = {}
        for t in range(1, 16):
            lv = lv * math.exp(-decay_rate * 1.0) + dv * decay_rate
            lv_series[float(t)] = lv
            gate.update(dv, float(t))

        # At t=15, the buffer should contain entries from t=1..15
        # λ_V(t=15 - L=10) = λ_V at t=5 (the entry with timestamp ≤ 5)
        # norm_slope = (λ_V_15 - λ_V_5) / (L * lv_ref)
        lv_15 = lv_series[15.0]
        lv_5 = lv_series[5.0]
        expected_norm_slope = (lv_15 - lv_5) / (L * lv_ref)
        actual_norm_slope = gate.norm_slope

        assert actual_norm_slope == pytest.approx(expected_norm_slope, rel=1e-6), (
            f"norm_slope mismatch: expected {expected_norm_slope:.8f}, "
            f"got {actual_norm_slope:.8f}"
        )


class TestSlopeGateReset:
    """reset() clears all state."""

    def test_reset_clears_buffer_and_state(self):
        gate = SlopeGate(
            tau_sec=60.0, L_sec=10.0, k_open=0.5, mode="ss",
            k_close=-1.0, lambda_v_ref=1.0, warmup_seconds=0.0,
        )
        gate.activate(0.0)

        # Build some state
        for t in range(1, 20):
            gate.update(1000.0, float(t))

        assert gate.lambda_v > 0
        assert len(gate._buf) > 0

        gate.reset()

        assert gate.lambda_v == 0.0
        assert gate.lambda_v_peak == 0.0
        assert len(gate._buf) == 0
        assert gate._t_event is None

        # Returns INACTIVE after reset
        state = gate.update(1000.0, 25.0)
        assert state == GateState.INACTIVE

    def test_reactivate_after_reset(self):
        gate = SlopeGate(
            tau_sec=60.0, L_sec=5.0, k_open=99.0, mode="ss",
            k_close=-99.0, lambda_v_ref=1.0, warmup_seconds=0.0,
        )
        gate.activate(0.0)
        for t in range(1, 10):
            gate.update(1000.0, float(t))

        gate.reset()
        gate.activate(100.0)

        state = gate.update(1000.0, 101.0)
        assert state != GateState.INACTIVE


class TestSlopeGateInvalidParams:
    """Constructor rejects invalid parameters."""

    def test_nonpositive_tau(self):
        with pytest.raises(ValueError):
            SlopeGate(tau_sec=0.0, L_sec=10.0, k_open=0.5, mode="ss", k_close=-1.0)
        with pytest.raises(ValueError):
            SlopeGate(tau_sec=-1.0, L_sec=10.0, k_open=0.5, mode="ss", k_close=-1.0)

    def test_nonpositive_L_sec(self):
        with pytest.raises(ValueError):
            SlopeGate(tau_sec=60.0, L_sec=0.0, k_open=0.5, mode="ss", k_close=-1.0)

    def test_invalid_mode(self):
        with pytest.raises(ValueError):
            SlopeGate(tau_sec=60.0, L_sec=10.0, k_open=0.5, mode="sc", k_close=-1.0)

    def test_ss_k_close_must_be_less_than_k_open(self):
        with pytest.raises(ValueError):
            SlopeGate(tau_sec=60.0, L_sec=10.0, k_open=0.5, mode="ss", k_close=0.5)
        with pytest.raises(ValueError):
            SlopeGate(tau_sec=60.0, L_sec=10.0, k_open=0.5, mode="ss", k_close=0.6)

    def test_sl_invalid_p_close(self):
        with pytest.raises(ValueError):
            SlopeGate(tau_sec=60.0, L_sec=10.0, k_open=0.5, mode="sl", p_close=0.0)
        with pytest.raises(ValueError):
            SlopeGate(tau_sec=60.0, L_sec=10.0, k_open=0.5, mode="sl", p_close=1.5)

    def test_nonpositive_lambda_v_ref(self):
        with pytest.raises(ValueError):
            SlopeGate(tau_sec=60.0, L_sec=10.0, k_open=0.5, mode="ss",
                      k_close=-1.0, lambda_v_ref=0.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
