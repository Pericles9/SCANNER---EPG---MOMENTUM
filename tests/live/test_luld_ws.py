"""Phase 2 — Massive LULD WS integration tests.

Covers: ms→ns timestamp normalisation for LULD, `T`-field ticker routing in the WS
dispatch, band + indicator storage, is_halted() flipping on indicator 17 / clearing
on 18, no order signal produced, and non-Nasdaq band breaches NOT setting _halted.

Offline; no Polygon / IBKR / DB required.
"""
from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from live.feed.universe import UniverseManager, _normalize_ws_timestamps
from live.signals.live_state import LiveSignalState
from live.signals.scanner_vwap import VwapSignalState

_NS_PER_SEC = 1_000_000_000
_SESSION_START_NS = 1_700_000_000 * _NS_PER_SEC


# ── VwapSignalState builder (cheap — no Hawkes) ───────────────────────────────

def _make_cfg():
    cfg = MagicMock()
    cfg.scanner_vwap.setup_filter_gate = False
    cfg.scanner_vwap.vwap_anchor = "per_bucket"
    cfg.scanner_vwap.vwap_exit_mode = "tick"
    cfg.scanner_vwap.hard_stop_pct = 0.12
    cfg.scanner_vwap.one_shot_per_session = True
    cfg.setup_filter.q_threshold = 0.65
    cfg.setup_filter.warmup_provisional_threshold = 0.75
    cfg.setup_filter.warmup_bars = 5
    cfg.setup_filter.removal_bars = 15
    cfg.strategy_id = "scanner_vwap"
    return cfg


def _make_ctx():
    ctx = MagicMock()
    ctx.tick_timestamps_ns = np.array([], dtype=np.int64)
    ctx.tick_prices = np.array([], dtype=np.float64)
    ctx.tick_sizes = np.array([], dtype=np.int64)
    ctx.last_ts_ns = 0
    ctx.session_start_ns = _SESSION_START_NS
    ctx.session_end_ns = _SESSION_START_NS + 16 * 3600 * _NS_PER_SEC
    ctx.setup_filter_result = None
    return ctx


def _make_vwap():
    risk = MagicMock()
    risk.open_positions = {}
    risk.has_position.side_effect = lambda t: False
    with patch("live.signals.scanner_vwap.CFG", _make_cfg()):
        return VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), risk)


def _bare_live():
    """LiveSignalState with just the attributes update_luld/is_halted touch."""
    s = object.__new__(LiveSignalState)
    s._ticker = "TEST"
    s._halted = False
    s._luld_bands = (None, None)
    s._luld_indicators = []
    s._halt_started_ns = 0
    s._luld_last_seen = 0.0
    s._luld_frozen = False
    s._engine = MagicMock()
    return s


def _luld(ticker, h, l, indicators, ts_ns, z=3, seq=1):
    return {"ev": "LULD", "T": ticker, "h": h, "l": l, "i": list(indicators),
            "z": z, "t": ts_ns, "q": seq}


# ── timestamp normalisation ───────────────────────────────────────────────────

def test_normalize_luld_ms_to_ns():
    item = _luld("ABCD", 10.5, 9.5, [], ts_ns=1_700_000_000_000)  # ms
    out = _normalize_ws_timestamps(item)
    assert out["t"] == 1_700_000_000_000 * 1_000_000   # ms → ns


# ── update_luld state machine (both signal states) ────────────────────────────

@pytest.mark.parametrize("make", [_make_vwap, _bare_live])
class TestUpdateLuld:

    def test_stores_bands_and_indicators(self, make):
        s = make()
        s.update_luld(_luld("TEST", 10.5, 9.5, [12], ts_ns=_SESSION_START_NS))
        assert s.luld_bands == (10.5, 9.5)
        assert s._luld_indicators == [12]
        assert s.is_halted() is False

    def test_halt_on_17_resume_on_18(self, make):
        s = make()
        t1 = _SESSION_START_NS
        t2 = t1 + 90 * _NS_PER_SEC
        halt = s.update_luld(_luld("TEST", 10.5, 9.5, [17], ts_ns=t1))
        assert s.is_halted() is True
        assert halt == ("HALT", 0.0)
        resume = s.update_luld(_luld("TEST", 10.5, 9.5, [18], ts_ns=t2))
        assert s.is_halted() is False
        assert resume[0] == "RESUME"
        assert resume[1] == pytest.approx(90.0, abs=0.01)

    def test_no_order_signal_produced(self, make):
        """update_luld returns only a halt-transition marker or None — never an order
        signal string consumed by the order path."""
        s = make()
        out = s.update_luld(_luld("TEST", 10.5, 9.5, [17], ts_ns=_SESSION_START_NS))
        assert out is None or (isinstance(out, tuple) and out[0] in ("HALT", "RESUME"))

    def test_duplicate_halt_no_retrigger(self, make):
        s = make()
        first = s.update_luld(_luld("TEST", 10.5, 9.5, [17], ts_ns=_SESSION_START_NS))
        second = s.update_luld(_luld("TEST", 10.5, 9.5, [17], ts_ns=_SESSION_START_NS + 1))
        assert first == ("HALT", 0.0)
        assert second is None   # already halted — no repeat transition
        assert s.is_halted() is True

    def test_non_nasdaq_band_breach_no_halt(self, make):
        """NYSE/AMEX tape (z != 3) carries bands but no 17/18 indicator — must NOT halt."""
        s = make()
        s.update_luld(_luld("TEST", 10.5, 9.5, [], ts_ns=_SESSION_START_NS, z=1))
        assert s.is_halted() is False
        assert s.luld_bands == (10.5, 9.5)


# ── WS dispatch routes LULD by field `T` ──────────────────────────────────────

def _make_manager():
    return UniverseManager(
        order_queue=MagicMock(), risk_state=MagicMock(), polygon_api_key="k",
        hot_ticks=[], hot_quotes=[], hot_signal_events=[], hot_hawkes_refits=[],
        session_clock=MagicMock(), telegram=None,
    )


def test_dispatch_luld_routes_by_T_field():
    mgr = _make_manager()
    ss = _make_vwap()
    mgr._universe["ABCD"] = SimpleNamespace(signal_state=ss)
    asyncio.run(mgr._dispatch_luld(_luld("ABCD", 10.5, 9.5, [17], ts_ns=_SESSION_START_NS)))
    assert ss.is_halted() is True


def test_dispatch_luld_unknown_ticker_drops_cleanly():
    mgr = _make_manager()
    ss = _make_vwap()
    mgr._universe["ABCD"] = SimpleNamespace(signal_state=ss)
    # 'WXYZ' not in universe — must not raise and must not touch ABCD's state.
    asyncio.run(mgr._dispatch_luld(_luld("WXYZ", 10.5, 9.5, [17], ts_ns=_SESSION_START_NS)))
    assert ss.is_halted() is False


def test_dispatch_luld_missing_T_drops_cleanly():
    mgr = _make_manager()
    ss = _make_vwap()
    mgr._universe["ABCD"] = SimpleNamespace(signal_state=ss)
    msg = {"ev": "LULD", "sym": "ABCD", "h": 10.5, "l": 9.5, "i": [17], "t": _SESSION_START_NS}
    asyncio.run(mgr._dispatch_luld(msg))   # no 'T' field → drops
    assert ss.is_halted() is False
