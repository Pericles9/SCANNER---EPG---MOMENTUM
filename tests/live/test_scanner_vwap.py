"""Unit tests for VwapSignalState — Gate 2 (VWAP engine) and Gate 3 (entry/exit/arming).

These tests run offline; no Polygon / IBKR / DB required.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from live.signals.scanner_vwap import VwapSignalState

_NS_PER_SEC = 1_000_000_000
_BAR_NS = 60 * _NS_PER_SEC   # 1 minute in ns
# Session start: arbitrary epoch treated as 04:00 ET (t_sec = 0).
# RTH_START_SEC = 19800 → ts_ns = SESSION_START + 19800 * NS_PER_SEC
_SESSION_START_NS = 1_700_000_000 * _NS_PER_SEC
_SESSION_END_NS = _SESSION_START_NS + 16 * 3600 * _NS_PER_SEC
_RTH_OFFSET_NS = 19800 * _NS_PER_SEC   # 09:30 ET offset from session start

# Pre-market bar minute (well before RTH): session_start is in pre_market
_PM_TICK_NS = _SESSION_START_NS + 60 * _NS_PER_SEC   # +1 min into session
_RTH_TICK_NS = _SESSION_START_NS + _RTH_OFFSET_NS     # exactly 09:30 ET


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_cfg(setup_filter_gate=False, vwap_anchor="per_bucket", hard_stop_pct=0.12,
              vwap_exit_mode="bar_close"):
    cfg = MagicMock()
    cfg.scanner_vwap.setup_filter_gate = setup_filter_gate
    cfg.scanner_vwap.vwap_anchor = vwap_anchor
    cfg.scanner_vwap.vwap_exit_mode = vwap_exit_mode
    cfg.scanner_vwap.hard_stop_pct = hard_stop_pct
    cfg.scanner_vwap.one_shot_per_session = True
    cfg.setup_filter.q_threshold = 0.65
    cfg.setup_filter.warmup_provisional_threshold = 0.75
    cfg.setup_filter.warmup_bars = 5
    cfg.setup_filter.removal_bars = 15
    cfg.strategy_id = "scanner_vwap"
    return cfg


def _make_ctx(ts_arr=(), p_arr=(), s_arr=(), last_ts_ns=0, sf=None):
    ctx = MagicMock()
    ctx.tick_timestamps_ns = np.array(list(ts_arr), dtype=np.int64)
    ctx.tick_prices = np.array(list(p_arr), dtype=np.float64)
    ctx.tick_sizes = np.array(list(s_arr), dtype=np.int64)
    ctx.last_ts_ns = last_ts_ns
    ctx.session_start_ns = _SESSION_START_NS
    ctx.session_end_ns = _SESSION_END_NS
    ctx.setup_filter_result = sf
    return ctx


def _make_risk(positions=None):
    risk = MagicMock()
    positions = positions or {}
    risk.open_positions = positions
    risk.has_position.side_effect = lambda ticker: ticker in positions
    return risk


def _build_state(cfg, ctx=None, risk=None):
    ctx = ctx or _make_ctx()
    risk = risk or _make_risk()
    with patch("live.signals.scanner_vwap.CFG", cfg):
        state = VwapSignalState("TEST", ctx, {"pct_change": 35.0}, date.today(), risk)
    # Rebind CFG on the module so subsequent method calls also see the mock.
    # We keep the patch alive per-test by wrapping the test body (see each test).
    return state


def _tick(ts_ns: int, price: float, size: int = 100) -> dict:
    return {"ev": "T", "t": ts_ns, "p": price, "s": size}


# ── Gate 2: VWAP accumulation ─────────────────────────────────────────────────

class TestVwapMath:

    def test_vwap_matches_hand_computed(self):
        """VWAP = Σ(p×s) / Σ(s) across seeded ticks."""
        prices = [10.0, 20.0, 30.0]
        sizes = [100, 50, 200]
        expected = sum(p * s for p, s in zip(prices, sizes)) / sum(sizes)

        ts = [_PM_TICK_NS + i * _NS_PER_SEC for i in range(3)]
        cfg = _make_cfg()
        ctx = _make_ctx(ts_arr=ts, p_arr=prices, s_arr=sizes, last_ts_ns=ts[-1])

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", ctx, {"pct_change": 35.0}, date.today(), _make_risk())
            assert abs(state._last_vwap - expected) < 1e-10

    def test_seeding_produces_same_vwap_as_streaming(self):
        """VWAP from seed equals VWAP from computing the same ticks inline."""
        prices = [50.0, 52.0, 48.0, 51.0]
        sizes = [200, 100, 300, 150]
        ts = [_PM_TICK_NS + i * _NS_PER_SEC for i in range(4)]
        expected = sum(p * s for p, s in zip(prices, sizes)) / sum(sizes)

        cfg = _make_cfg()
        ctx = _make_ctx(ts_arr=ts, p_arr=prices, s_arr=sizes, last_ts_ns=ts[-1])

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", ctx, {"pct_change": 35.0}, date.today(), _make_risk())
            assert abs(state._last_vwap - expected) < 1e-10

    def test_seeding_emits_no_order_signals(self):
        """Seeding from historical ticks must never emit an order signal."""
        prices = [100.0, 120.0]   # last price (120) > vwap — would trigger ENTRY if live
        sizes = [100, 100]
        ts = [_PM_TICK_NS, _PM_TICK_NS + _NS_PER_SEC]

        cfg = _make_cfg()
        ctx = _make_ctx(ts_arr=ts, p_arr=prices, s_arr=sizes, last_ts_ns=ts[-1])

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", ctx, {"pct_change": 35.0}, date.today(), _make_risk())
            # State should be FLAT with no pending signals
            assert state._state == "FLAT"
            assert not state._in_position

    def test_per_bucket_resets_at_rth_boundary(self):
        """per_bucket anchor resets VWAP accumulators on first tick after 09:30 ET."""
        # Seed two pre-market ticks at price=200 (high, to make contrast clear)
        pm_ts = [_PM_TICK_NS, _PM_TICK_NS + _NS_PER_SEC]
        cfg = _make_cfg(vwap_anchor="per_bucket")
        ctx = _make_ctx(
            ts_arr=pm_ts,
            p_arr=[200.0, 200.0],
            s_arr=[100, 100],
            last_ts_ns=pm_ts[-1],
        )

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", ctx, {"pct_change": 35.0}, date.today(), _make_risk())
            assert abs(state._last_vwap - 200.0) < 1e-10

            # First RTH tick at price=50 — should reset, VWAP collapses to 50
            rth_ts = _RTH_TICK_NS + _NS_PER_SEC   # +1s past 09:30 to avoid boundary issues
            result = state.update_trade(_tick(rth_ts, 50.0, 100))
            assert abs(state._last_vwap - 50.0) < 1e-10

    def test_rth_only_ignores_premarket_ticks(self):
        """rth_only anchor: VWAP stays 0 for pre-market ticks, updates only in RTH."""
        pm_ts = [_PM_TICK_NS, _PM_TICK_NS + _NS_PER_SEC]
        cfg = _make_cfg(vwap_anchor="rth_only")
        ctx = _make_ctx(
            ts_arr=pm_ts,
            p_arr=[200.0, 200.0],
            s_arr=[100, 100],
            last_ts_ns=pm_ts[-1],
        )

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", ctx, {"pct_change": 35.0}, date.today(), _make_risk())
            # Pre-market seed ticks should NOT have accumulated VWAP
            assert state._last_vwap == 0.0

            # First RTH tick updates VWAP
            rth_ts = _RTH_TICK_NS + _NS_PER_SEC
            state.update_trade(_tick(rth_ts, 55.0, 200))
            assert abs(state._last_vwap - 55.0) < 1e-10

    def test_bar_boundary_fires_exactly_once_per_bar(self):
        """Bar-close check fires once per minute boundary crossing, not per tick."""
        cfg = _make_cfg()  # gate=False → armed=True

        # Use timestamps in same pre-market region; offset from session start
        base_ns = _SESSION_START_NS + 120 * _NS_PER_SEC  # 2 minutes in
        bar_minute_A = base_ns // (_NS_PER_SEC * 60)
        # Place bar B start at exactly the next minute
        bar_B_ns = (bar_minute_A + 1) * _NS_PER_SEC * 60

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), _make_risk())

            # Send 3 ticks in bar A — no bar close should fire
            for i in range(3):
                ts = base_ns + i * _NS_PER_SEC
                r = state.update_trade(_tick(ts, 90.0 + i, 100))
                assert r.order_signal is None

            # Advance to bar B — bar close fires with last_price=92, last_vwap≈90.7
            r = state.update_trade(_tick(bar_B_ns, 99.0, 100))
            # last_price from bar A was 92 (90+0,91+1,92+2 → last=92)
            # VWAP of bar A = (90*100 + 91*100 + 92*100) / 300 = 273/3 = 91
            # 92 > 91 → ENTRY fires
            assert r.order_signal == "ENTRY"

            # Second tick in bar B — no second ENTRY
            state.record_entry("pre_market", 0.5)   # simulate fill
            r = state.update_trade(_tick(bar_B_ns + _NS_PER_SEC, 99.0, 100))
            assert r.order_signal is None


# ── Gate 3: Entry / exit / stop / arming ─────────────────────────────────────

class TestEntryExitLogic:

    def _armed_flat_state(self, cfg, last_vwap=100.0, last_price=110.0):
        """Return a state primed for entry (FLAT, armed, last_price > last_vwap)."""
        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), _make_risk())
        state._armed = True
        state._last_vwap = last_vwap
        state._last_price = last_price
        # Set a bar minute so the first live tick with a different minute triggers bar close
        state._last_bar_minute = 1000
        return state

    def test_entry_fires_on_first_bar_close_above_vwap(self):
        """FLAT + armed + close strictly above VWAP → ENTRY."""
        cfg = _make_cfg()
        state = self._armed_flat_state(cfg, last_vwap=100.0, last_price=110.0)

        # Tick in bar 1001 triggers bar close using last_price=110 and last_vwap=100
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 111.0))

        assert result.order_signal == "ENTRY"
        assert any(ev[4] == "VWAP_ENTRY" for ev in result.signal_events)

    def test_no_entry_when_close_equals_vwap(self):
        """close == VWAP → no signal (strict inequality required)."""
        cfg = _make_cfg()
        state = self._armed_flat_state(cfg, last_vwap=100.0, last_price=100.0)

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 100.0))

        assert result.order_signal is None

    def test_no_entry_when_close_below_vwap(self):
        """close < VWAP (downtrend) → no ENTRY."""
        cfg = _make_cfg()
        state = self._armed_flat_state(cfg, last_vwap=100.0, last_price=90.0)

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 90.0))

        assert result.order_signal is None

    def test_vwap_close_fires_when_long_and_close_below_vwap(self):
        """LONG + bar close strictly below VWAP → VWAP_CLOSE."""
        cfg = _make_cfg()
        state = self._armed_flat_state(cfg, last_vwap=100.0, last_price=90.0)
        state._state = "LONG"
        state._in_position = True

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 88.0))

        assert result.order_signal == "VWAP_CLOSE"
        assert any(ev[4] == "VWAP_EXIT" for ev in result.signal_events)

    def test_no_vwap_close_when_equal_to_vwap(self):
        """LONG + close == VWAP → no exit signal."""
        cfg = _make_cfg()
        state = self._armed_flat_state(cfg, last_vwap=100.0, last_price=100.0)
        state._state = "LONG"
        state._in_position = True

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 100.0))

        assert result.order_signal is None

    def test_hard_stop_fires_intra_bar_without_waiting_for_close(self):
        """LONG + intra-bar price ≤ entry × (1 − 0.12) → HARD_STOP immediately."""
        risk = _make_risk({"TEST": {"avg_cost": 100.0}})  # entry fill at 100
        cfg = _make_cfg(hard_stop_pct=0.12)

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), risk)
        state._state = "LONG"
        state._in_position = True
        state._last_bar_minute = 1000  # stay in same bar

        stop_level = 100.0 * (1 - 0.12)  # = 88.0
        with patch("live.signals.scanner_vwap.CFG", cfg):
            # Tick at exactly the stop level, same bar (no bar close fires)
            result = state.update_trade(_tick(1000 * _BAR_NS + 30 * _NS_PER_SEC, stop_level))

        assert result.order_signal == "HARD_STOP"
        assert any(ev[4] == "HARD_STOP" for ev in result.signal_events)

    def test_hard_stop_not_triggered_above_level(self):
        """Price just above stop level → no HARD_STOP."""
        risk = _make_risk({"TEST": {"avg_cost": 100.0}})
        cfg = _make_cfg(hard_stop_pct=0.12)

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), risk)
        state._state = "LONG"
        state._in_position = True
        state._last_bar_minute = 1000

        above_stop = 100.0 * (1 - 0.12) + 0.01  # just above 88.0
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1000 * _BAR_NS + 30 * _NS_PER_SEC, above_stop))

        assert result.order_signal is None

    def test_session_done_true_after_record_exit(self):
        """record_exit() → CLOSED; next update_trade returns disqualify=True, session_done=True."""
        cfg = _make_cfg()
        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), _make_risk())
        state._state = "LONG"
        state._in_position = True
        state._last_ts_ns = 0

        state.record_exit()

        assert state._state == "CLOSED"
        assert not state._in_position
        assert state._session_done

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(_NS_PER_SEC, 100.0))

        assert result.disqualify is True
        assert result.session_done is True
        assert result.order_signal is None

    def test_no_duplicate_exit_while_signaled(self):
        """_exit_signaled suppresses repeated VWAP_CLOSE until clear_exit_pending."""
        cfg = _make_cfg()
        state = self._armed_flat_state(cfg, last_vwap=100.0, last_price=90.0)
        state._state = "LONG"
        state._in_position = True
        state._exit_signaled = True   # already signaled

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 85.0))

        assert result.order_signal is None

    def test_clear_exit_pending_re_arms_exit(self):
        """clear_exit_pending() allows next qualifying bar close to re-signal."""
        cfg = _make_cfg()
        state = self._armed_flat_state(cfg, last_vwap=100.0, last_price=90.0)
        state._state = "LONG"
        state._in_position = True
        state._exit_signaled = True
        state.clear_exit_pending()    # re-arm

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 88.0))

        assert result.order_signal == "VWAP_CLOSE"

    def test_dedup_ignores_timestamps_at_or_before_last(self):
        """Ticks with ts_ns ≤ last_ts_ns are silently ignored (no signals)."""
        cfg = _make_cfg()
        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(last_ts_ns=500), {"pct_change": 35.0}, date.today(), _make_risk())
        state._armed = True
        state._last_price = 110.0
        state._last_vwap = 100.0
        state._last_bar_minute = 1000

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(500, 999.0))  # ts == last_ts_ns → dedup

        assert result.order_signal is None
        assert result.signal_events == []

    def test_no_hard_stop_without_fill_price(self):
        """HARD_STOP check skipped when avg_cost is not yet in risk_state (fill pending)."""
        risk = _make_risk({})   # no position entry yet
        cfg = _make_cfg()

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), risk)
        state._state = "LONG"
        state._in_position = True
        state._last_bar_minute = 1000

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1000 * _BAR_NS + 30 * _NS_PER_SEC, 0.01))

        assert result.order_signal is None  # price is extreme but no fill price → no stop


# ── Gate 3: Arming via setup filter ──────────────────────────────────────────

class TestSFArming:

    def _make_sf_result(self, q_value=0.80):
        from backtest.setup_filter import SetupFilterResult
        n = 20
        q = np.full(n, q_value, dtype=np.float64)
        return SetupFilterResult(
            passes=q_value >= 0.65, psi_passes=True, n_bars=n,
            first_qualify_bar=5, last_fail_bar=-1,
            min_sustained_q=float(q_value), mean_q_tilde=float(q_value),
            weakest_signal="range",
            range_scores=np.full(n, 0.9), vol_scores=np.full(n, 0.9),
            thin_scores=np.full(n, 0.9), body_scores=np.full(n, 0.9),
            q_raw=q.copy(), q_tilde=q,
        )

    def test_armed_when_sf_passes_at_init(self):
        """setup_filter_gate=True + passing SF result → armed=True at construction."""
        sf = self._make_sf_result(0.80)
        cfg = _make_cfg(setup_filter_gate=True)
        ctx = _make_ctx(sf=sf)

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", ctx, {"pct_change": 35.0}, date.today(), _make_risk())

        assert state._armed is True

    def test_not_armed_when_sf_fails_at_init(self):
        """setup_filter_gate=True + failing SF result → armed=False at construction."""
        sf = self._make_sf_result(0.50)   # below 0.65 threshold
        cfg = _make_cfg(setup_filter_gate=True)
        ctx = _make_ctx(sf=sf)

        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", ctx, {"pct_change": 35.0}, date.today(), _make_risk())

        assert state._armed is False

    def test_no_entry_when_not_armed(self):
        """_armed=False prevents entry even when close > VWAP."""
        cfg = _make_cfg()
        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), _make_risk())
        state._armed = False
        state._last_price = 110.0
        state._last_vwap = 100.0
        state._last_bar_minute = 1000

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 111.0))

        assert result.order_signal is None


# ── Gate 3: Tick-level VWAP cross exit (vwap_exit_mode="tick") ───────────────

class TestTickExitMode:
    """Tests for vwap_exit_mode='tick' — exit fires on the first trade below VWAP."""

    def _long_with_vwap(self, cfg, vwap=100.0):
        """LONG state with stable running VWAP ≈ vwap (large accumulated mass)."""
        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), _make_risk())
        state._state = "LONG"
        state._in_position = True
        state._armed = True
        state._last_vwap = vwap
        state._last_price = vwap
        # Large accumulated mass keeps VWAP stable across single test ticks.
        state._pv_sum = vwap * 100_000
        state._v_sum = 100_000
        state._last_bar_minute = 1000   # stay in known bar by default
        return state

    def test_tick_below_vwap_exits_intrabar(self):
        """Tick mode: LONG + tick price strictly below running VWAP → VWAP_CROSS (intra-bar)."""
        cfg = _make_cfg(vwap_exit_mode="tick")
        state = self._long_with_vwap(cfg, vwap=100.0)

        ts = 1000 * _BAR_NS + 30 * _NS_PER_SEC   # same bar, no bar-close fires
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(ts, 95.0))

        assert result.order_signal == "VWAP_CROSS"
        assert any(ev[4] == "VWAP_EXIT" for ev in result.signal_events)

    def test_tick_at_vwap_no_exit(self):
        """Tick mode: price == VWAP → no exit (strict < required)."""
        cfg = _make_cfg(vwap_exit_mode="tick")
        state = self._long_with_vwap(cfg, vwap=100.0)

        ts = 1000 * _BAR_NS + 30 * _NS_PER_SEC
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(ts, 100.0))

        assert result.order_signal is None

    def test_tick_above_vwap_no_exit(self):
        """Tick mode: price > VWAP → no exit."""
        cfg = _make_cfg(vwap_exit_mode="tick")
        state = self._long_with_vwap(cfg, vwap=100.0)

        ts = 1000 * _BAR_NS + 30 * _NS_PER_SEC
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(ts, 105.0))

        assert result.order_signal is None

    def test_hard_stop_wins_when_both_breach(self):
        """Price below both hard stop and VWAP: only HARD_STOP fires (checked first)."""
        risk = _make_risk({"TEST": {"avg_cost": 100.0}})
        cfg = _make_cfg(vwap_exit_mode="tick", hard_stop_pct=0.12)
        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), risk)
        state._state = "LONG"
        state._in_position = True
        state._last_vwap = 100.0
        state._pv_sum = 100.0 * 100_000
        state._v_sum = 100_000
        state._last_bar_minute = 1000

        # Price=85 is below stop_level=88 AND below VWAP=100
        ts = 1000 * _BAR_NS + 30 * _NS_PER_SEC
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(ts, 85.0))

        assert result.order_signal == "HARD_STOP"
        event_types = [ev[4] for ev in result.signal_events]
        assert "HARD_STOP" in event_types
        assert "VWAP_EXIT" not in event_types   # VWAP_CROSS suppressed by _exit_signaled

    def test_session_done_after_vwap_cross(self):
        """After VWAP_CROSS + record_exit, next tick returns disqualify=True, session_done=True."""
        cfg = _make_cfg(vwap_exit_mode="tick")
        state = self._long_with_vwap(cfg, vwap=100.0)

        ts = 1000 * _BAR_NS + 30 * _NS_PER_SEC
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(ts, 95.0))
        assert result.order_signal == "VWAP_CROSS"

        state.record_exit()
        assert state._state == "CLOSED"

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(ts + _NS_PER_SEC, 94.0))
        assert result.disqualify is True
        assert result.session_done is True

    def test_tick_mode_skips_bar_close_exit(self):
        """Tick mode: bar-close below VWAP does NOT emit VWAP_CLOSE."""
        cfg = _make_cfg(vwap_exit_mode="tick")
        state = self._long_with_vwap(cfg, vwap=100.0)
        state._last_price = 90.0   # previous bar closed below VWAP
        state._last_bar_minute = 1000

        # Tick in new bar at price=105 — bar-close fires (close=90<vwap=100) but skipped in tick mode;
        # tick price=105 > VWAP → no VWAP_CROSS either
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 105.0))

        assert result.order_signal is None

    def test_bar_close_mode_exit_unchanged(self):
        """bar_close mode: bar-close below VWAP still fires VWAP_CLOSE (regression)."""
        cfg = _make_cfg(vwap_exit_mode="bar_close")
        state = self._long_with_vwap(cfg, vwap=100.0)
        state._last_price = 90.0   # previous bar closed below VWAP
        state._last_bar_minute = 1000

        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1001 * _BAR_NS, 95.0))

        assert result.order_signal == "VWAP_CLOSE"


class TestEntryFillRace:
    """Regression: the entry-fill-in-flight window must NOT be mistaken for an
    external close (which orphaned the just-opened position out of the universe)."""

    def _long_inflight(self, cfg, positions):
        risk = _make_risk(positions)
        with patch("live.signals.scanner_vwap.CFG", cfg):
            state = VwapSignalState("TEST", _make_ctx(), {"pct_change": 35.0}, date.today(), risk)
        # Optimistic record_entry: LONG before the BUY fills.
        state._state = "LONG"
        state._in_position = True
        state._last_bar_minute = 1000          # stay in-bar; no bar-close exit
        state._last_vwap = 100.0
        state._last_price = 105.0
        state._pv_sum = 100.0 * 100_000        # keep VWAP ≈ 100 across the tick
        state._v_sum = 100_000
        return state, risk

    def test_inflight_entry_not_marked_external_close(self):
        """LONG but position not yet in risk_state (fill in flight) → must stay LONG,
        not session-done, not removed."""
        cfg = _make_cfg(vwap_exit_mode="bar_close")
        state, _risk = self._long_inflight(cfg, {})   # no position yet
        ts = 1000 * _BAR_NS + 30 * _NS_PER_SEC        # same bar, price above VWAP
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(ts, 105.0))
        assert state._state == "LONG"
        assert state._position_confirmed is False
        assert result.session_done is False
        assert result.disqualify is False

    def test_external_close_fires_only_after_confirmed(self):
        """Once the fill lands (confirmed), a later disappearance is a real external
        close → CLOSED → session_done on the next tick."""
        cfg = _make_cfg(vwap_exit_mode="bar_close")
        positions = {"TEST": {"avg_cost": 100.0, "qty": 100}}
        state, _risk = self._long_inflight(cfg, positions)

        # Tick 1: position present → confirmed, still LONG.
        with patch("live.signals.scanner_vwap.CFG", cfg):
            state.update_trade(_tick(1000 * _BAR_NS + 30 * _NS_PER_SEC, 105.0))
        assert state._position_confirmed is True
        assert state._state == "LONG"

        # Position flattened externally (kill switch / EOD) → disappears from risk.
        positions.clear()
        with patch("live.signals.scanner_vwap.CFG", cfg):
            state.update_trade(_tick(1000 * _BAR_NS + 31 * _NS_PER_SEC, 105.0))
        assert state._state == "CLOSED"

        # Next tick surfaces session_done for the one-shot cleanup.
        with patch("live.signals.scanner_vwap.CFG", cfg):
            result = state.update_trade(_tick(1000 * _BAR_NS + 32 * _NS_PER_SEC, 105.0))
        assert result.session_done is True
        assert result.disqualify is True
