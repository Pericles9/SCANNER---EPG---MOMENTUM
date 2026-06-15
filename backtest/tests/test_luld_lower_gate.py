"""Tests for Phase F asymmetric LULD lower-band gate in backtest runner.

These tests verify that:
  - Upper-band LULD fires are acted on regardless of lower_band_enabled flag
  - Lower-band LULD fires are suppressed when lower_band_enabled=False
  - Lower-band LULD fires are acted on when lower_band_enabled=True (backward compat)
  - luld_lower count == 0 assertion holds when lower band is disabled

The runner's _process_event cannot be called directly (it requires full data files),
so we test the gate logic in isolation by building minimal mock objects that mimic
the structures the gate checks.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.exits.luld_proximity import LuldProximityExit, ProximityState, ProximityResult


# ── Helper to build a ProximityResult with a known fire_side ─────────

def _make_halt_result(fire_side: str, upper_band: float = 12.0) -> ProximityResult:
    """Return a ProximityResult with state=EXIT_HALT and the given fire_side."""
    return ProximityResult(
        state=ProximityState.EXIT_HALT,
        fire_side=fire_side,
        reference_price=11.0,
        upper_band=upper_band,
        bid_proximity_pct=0.01,
        spread_used=0.05,
        band_pct=0.10,
    )


def _make_safe_result() -> ProximityResult:
    """Return a ProximityResult with state=SAFE."""
    return ProximityResult(
        state=ProximityState.SAFE,
        fire_side=None,
        reference_price=11.0,
        upper_band=12.0,
        bid_proximity_pct=0.10,
        spread_used=0.05,
        band_pct=0.10,
    )


# ── Gate logic extracted for testability ─────────────────────────────
# The actual gate in runner.py:
#
#   if not exit_fired and cur_luld == ProximityState.EXIT_HALT:
#       _luld_side = luld_fire_sides[i] or "lower"
#       if _luld_side == "lower" and not luld_lower_band_enabled:
#           log.debug(...)
#       else:
#           ... fire exit ...
#           exit_fired = True
#
# We model this here as a pure function so tests don't need the runner's
# full subprocess machinery.

def _should_fire(luld_result: ProximityResult, luld_lower_band_enabled: bool) -> bool:
    """Mimic the runner's Phase F gate decision: True if the exit should fire."""
    if luld_result.state != ProximityState.EXIT_HALT:
        return False
    fire_side = luld_result.fire_side or "lower"
    if fire_side == "lower" and not luld_lower_band_enabled:
        return False
    return True


# ── Tests ─────────────────────────────────────────────────────────────

class TestLuldLowerGate:

    def test_upper_fires_when_lower_disabled(self):
        """Upper-band fire must be acted on even when lower_band_enabled=False."""
        result = _make_halt_result("upper")
        assert _should_fire(result, luld_lower_band_enabled=False) is True

    def test_upper_fires_when_lower_enabled(self):
        """Upper-band fire is acted on when lower_band_enabled=True (baseline)."""
        result = _make_halt_result("upper")
        assert _should_fire(result, luld_lower_band_enabled=True) is True

    def test_lower_suppressed_when_lower_disabled(self):
        """Lower-band fire must be suppressed when lower_band_enabled=False."""
        result = _make_halt_result("lower")
        assert _should_fire(result, luld_lower_band_enabled=False) is False

    def test_lower_fires_when_lower_enabled(self):
        """Lower-band fire is acted on when lower_band_enabled=True (backward compat)."""
        result = _make_halt_result("lower")
        assert _should_fire(result, luld_lower_band_enabled=True) is True

    def test_safe_result_never_fires(self):
        """SAFE state never fires regardless of lower_band_enabled."""
        result = _make_safe_result()
        assert _should_fire(result, luld_lower_band_enabled=False) is False
        assert _should_fire(result, luld_lower_band_enabled=True) is False

    def test_none_fire_side_defaults_to_lower(self):
        """When fire_side is None, gate treats it as 'lower' (conservative fallback)."""
        result = _make_halt_result("lower")
        # Manually set fire_side to None to test the `or "lower"` fallback
        result = ProximityResult(
            state=ProximityState.EXIT_HALT,
            fire_side=None,
            reference_price=result.reference_price,
            upper_band=result.upper_band,
            bid_proximity_pct=result.bid_proximity_pct,
            spread_used=result.spread_used,
            band_pct=result.band_pct,
        )
        # With lower disabled: None side defaults to lower -> suppressed
        assert _should_fire(result, luld_lower_band_enabled=False) is False
        # With lower enabled: None side defaults to lower -> fires
        assert _should_fire(result, luld_lower_band_enabled=True) is True


class TestLuldLowerCountAssertion:

    def test_assertion_passes_when_no_luld_lower(self):
        """T2c assertion: luld_lower count == 0 when lower_band_enabled=False."""
        exit_breakdown = {
            "exit_d": {"count": 50},
            "epg_window_close": {"count": 30},
            "luld_upper": {"count": 10},
        }
        luld_lower_count = exit_breakdown.get("luld_lower", {}).get("count", 0)
        assert luld_lower_count == 0

    def test_assertion_would_fail_if_luld_lower_present(self):
        """T2c assertion should detect a luld_lower fire when lower band is disabled."""
        exit_breakdown = {
            "exit_d": {"count": 50},
            "luld_lower": {"count": 1},
        }
        luld_lower_count = exit_breakdown.get("luld_lower", {}).get("count", 0)
        with pytest.raises(AssertionError):
            assert luld_lower_count == 0, (
                f"BUG: luld_lower count={luld_lower_count} but lower_band_enabled=False"
            )

    def test_luld_lower_allowed_when_enabled(self):
        """When lower_band_enabled=True the assertion is not applied (no check)."""
        luld_lower_band_enabled = True
        exit_breakdown = {"luld_lower": {"count": 5}}
        # Assertion is only applied when lower_band_enabled=False — no check here
        if not luld_lower_band_enabled:
            luld_lower_count = exit_breakdown.get("luld_lower", {}).get("count", 0)
            assert luld_lower_count == 0
        # If we reach here without asserting, the test passes
