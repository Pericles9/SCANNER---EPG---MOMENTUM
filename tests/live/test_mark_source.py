"""Phase 1 — price-source hardening tests.

Covers the $0-mark fix: a dedicated last-trade field, the mark() priority ladder
(LIVE → MID → STALE/HALTED → NONE, never a silent 0.0), and the readout formatters
that must render a missing mark as '—' rather than '$0.00'.

Offline; no Polygon / IBKR / DB required.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from live.bot.formatters import format_mark, format_position_block
from live.signals.live_state import LiveSignalState
from live.signals.scanner_vwap import VwapSignalState

_NS_PER_SEC = 1_000_000_000
_NOW = 1_700_000_000 * _NS_PER_SEC


def _bare(cls):
    """A mark()-ready instance without the heavy __init__ (Hawkes/gate/ctx).

    mark() and last_price only read the fields set here, so this isolates the
    price-source logic from strategy construction.
    """
    obj = object.__new__(cls)
    obj._last_trade_price = None
    obj._last_trade_ts_ns = 0
    obj._last_bid = None
    obj._last_ask = None
    obj._halted = False
    obj._sf_prices = []        # LiveSignalState.last_price fallback
    obj._last_price = 0.0      # VwapSignalState.last_price fallback
    return obj


# ── mark() priority ladder (both signal states) ───────────────────────────────

@pytest.mark.parametrize("cls", [LiveSignalState, VwapSignalState])
class TestMarkLadder:

    def test_fresh_trade_is_live(self, cls):
        s = _bare(cls)
        s._last_trade_price = 12.34
        s._last_trade_ts_ns = _NOW
        price, src, age = s.mark(_NOW)
        assert src == "LIVE"
        assert price == 12.34
        assert age == pytest.approx(0.0)

    def test_stale_trade_prefers_fresh_mid(self, cls):
        s = _bare(cls)
        s._last_trade_price = 10.0
        s._last_trade_ts_ns = _NOW - 10 * _NS_PER_SEC   # 10s old > 5s stale window
        s._last_bid = 11.0
        s._last_ask = 11.2
        price, src, age = s.mark(_NOW)
        assert src == "MID"
        assert price == pytest.approx(11.1)

    def test_stale_trade_no_quote_is_stale(self, cls):
        s = _bare(cls)
        s._last_trade_price = 10.0
        s._last_trade_ts_ns = _NOW - 10 * _NS_PER_SEC
        price, src, age = s.mark(_NOW)
        assert src == "STALE"
        assert price == 10.0
        assert age == pytest.approx(10.0, abs=0.01)

    def test_stale_trade_halted_is_halted(self, cls):
        s = _bare(cls)
        s._last_trade_price = 10.0
        s._last_trade_ts_ns = _NOW - 30 * _NS_PER_SEC
        s._halted = True
        price, src, age = s.mark(_NOW)
        assert src == "HALTED"
        assert price == 10.0

    def test_no_trade_quote_only_is_mid(self, cls):
        s = _bare(cls)
        s._last_bid = 5.0
        s._last_ask = 5.5
        price, src, age = s.mark(_NOW)
        assert src == "MID"
        assert price == pytest.approx(5.25)

    def test_nothing_known_is_none(self, cls):
        s = _bare(cls)
        price, src, age = s.mark(_NOW)
        assert src == "NONE"
        assert price is None

    def test_never_returns_zero(self, cls):
        """A zero/absent trade with no quote must resolve to None, never 0.0."""
        s = _bare(cls)
        s._last_trade_price = 0.0     # bad/empty
        s._last_bid = 0.0
        s._last_ask = 0.0
        price, src, age = s.mark(_NOW)
        assert price is None
        assert src == "NONE"

    def test_last_price_property_never_silent_zero(self, cls):
        s = _bare(cls)
        assert s.last_price is None     # nothing known → None, not 0.0
        s._last_trade_price = 7.0
        assert s.last_price == 7.0


# ── update_trade wires the authoritative mark (VwapSignalState, cheap to build) ─

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
    ctx.session_start_ns = _NOW
    ctx.session_end_ns = _NOW + 16 * 3600 * _NS_PER_SEC
    ctx.setup_filter_result = None
    return ctx


def test_update_trade_sets_last_trade_fields():
    cfg = _make_cfg()
    risk = MagicMock()
    risk.open_positions = {}
    risk.has_position.side_effect = lambda t: False
    with patch("live.signals.scanner_vwap.CFG", cfg):
        s = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), risk)
        ts = _NOW + 120 * _NS_PER_SEC
        s.update_trade({"ev": "T", "t": ts, "p": 42.5, "s": 100})
        assert s._last_trade_price == 42.5
        assert s._last_trade_ts_ns == ts
        price, src, _ = s.mark(ts)
        assert src == "LIVE" and price == 42.5


# ── readout formatters render '—', never '$0.00' ──────────────────────────────

class TestFormatMark:

    def test_none_renders_dash(self):
        assert format_mark(None, "NONE", 0.0) == "—"

    def test_zero_renders_dash(self):
        assert format_mark(0.0, "LIVE", 0.0) == "—"

    def test_live_has_no_tag(self):
        assert format_mark(12.3, "LIVE", 0.0) == "$12.30"

    def test_mid_tagged(self):
        assert format_mark(12.3, "MID", 0.0) == "$12.30 (mid)"

    def test_rest_tagged(self):
        assert format_mark(12.3, "REST", 0.0) == "$12.30 (REST)"

    def test_stale_tagged_with_age(self):
        assert format_mark(12.3, "STALE", 42.0) == "$12.30 (stale 42s)"

    def test_halted_tagged(self):
        assert format_mark(12.3, "HALTED", 0.0) == "$12.30 (halted)"


class TestFormatPositionBlock:

    def test_none_mark_renders_dash_not_zero(self):
        block = format_position_block(
            ticker="ABCD", avg_cost=10.0, qty=100, entry_ns=None,
            current_price=None, epg_gate="inactive", lambda_hat=0.0, lambda_ref=0.0,
            scanner_context={}, mark_str="—",
        )
        assert "Current: —" in block
        assert "Unrealised: —" in block
        assert "$0.00" not in block

    def test_valid_mark_renders_pnl(self):
        block = format_position_block(
            ticker="ABCD", avg_cost=10.0, qty=100, entry_ns=None,
            current_price=12.0, epg_gate="inactive", lambda_hat=0.0, lambda_ref=0.0,
            scanner_context={}, mark_str="$12.00",
        )
        assert "Current: $12.00" in block
        assert "+$200.00" in block
