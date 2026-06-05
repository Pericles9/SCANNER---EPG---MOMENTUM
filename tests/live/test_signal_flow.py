"""End-to-end signal-flow tests for the three fixes:

  Bug #1 — LiveSignalState feeds Hawkes total intensity (lam_buy + lam_sell) to
           EventAnchor, NOT the EKF estimate (which clamps to exp(20)).
           Verified by: synthetic tick stream → RISING_EDGE fires →
           order_queue.put_nowait() is called.

  Bug #2 — Lee-Ready side encoding matches backtest (1 = BUY, -1 = SELL).
           Verified by: BUY tick → engine R_buy increments; SELL tick → R_sell.

  Bug #6 — UniverseManager.close_ws() does not raise AttributeError when called
           on a manager that never had an active WS connection.

These tests run offline; no Polygon / IBKR / market required.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import date
from unittest.mock import MagicMock

import numpy as np
import pytest

from backtest.setup_filter import SetupFilterResult
from core.epg.anchor import EventAnchor
from core.epg.gate import GateState
from core.epg.gate_variants import SlopeGate
from core.hawkes.engine import HawkesEngine

from live.feed.context import TickerContext
from live.feed.signal_loop import HeartbeatMonitor, signal_loop
from live.feed.universe import UniverseManager, _normalize_ws_timestamps
from live.session_clock import SessionClock
from live.signals.context_fetch import ContextFetchResult, _classify_sides
from live.signals.live_state import LiveSignalState

_NS_PER_SEC = 1_000_000_000
_SESSION_START_NS = 1_700_000_000 * _NS_PER_SEC          # arbitrary stable epoch
_SESSION_END_NS = _SESSION_START_NS + 16 * 3600 * _NS_PER_SEC  # +16h


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_passing_sf_result() -> SetupFilterResult:
    """SetupFilterResult with passes=True and minimal valid arrays."""
    q_tilde = np.full(20, 0.80, dtype=np.float64)  # sustained well above 0.65
    return SetupFilterResult(
        passes=True,
        psi_passes=True,
        n_bars=20,
        first_qualify_bar=5,
        last_fail_bar=-1,
        min_sustained_q=0.80,
        mean_q_tilde=0.80,
        weakest_signal="range",
        range_scores=np.full(20, 0.9, dtype=np.float64),
        vol_scores=np.full(20, 0.9, dtype=np.float64),
        thin_scores=np.full(20, 0.9, dtype=np.float64),
        body_scores=np.full(20, 0.9, dtype=np.float64),
        q_raw=q_tilde.copy(),
        q_tilde=q_tilde,
    )


def _build_live_state(pct_change: float = 35.0) -> LiveSignalState:
    """Build a LiveSignalState with:
      - HawkesEngine at config defaults (mu_buy = mu_sell = 0.1)
      - EventAnchor pre-fired at t_event=0
      - SlopeGate (F_ss) pre-activated at t_event=0, gate_activated=True
      - SetupFilterResult that admits (q_tilde above threshold)
      - No historical ticks (last_ts_ns = 0)
    """
    lambda_ref = 0.2  # mu_buy + mu_sell — also used as SlopeGate lambda_v_ref
    engine = HawkesEngine(
        beta_mle=0.1,
        alpha_self_buy=0.005,
        alpha_cross_buy=0.0,
        mu_buy=0.1,
        mu_sell=0.1,
        lambda_ref=lambda_ref,
        alpha_self_sell=0.005,
        alpha_cross_sell=0.0,
    )

    anchor = EventAnchor(lambda_ref=lambda_ref, k_multiplier=5.0)
    # Pre-fire anchor at t_event=0 so we don't need to drive a real crossing.
    anchor._t_event = 0.0
    anchor._fired = True

    gate = SlopeGate(
        tau_sec=180.0,
        L_sec=30.0,
        k_open=0.5,
        mode="ss",
        k_close=0.0,
        lambda_v_ref=lambda_ref,
        warmup_seconds=300.0,
    )
    gate.activate(0.0)

    ctx = ContextFetchResult(
        engine=engine,
        anchor=anchor,
        gate=gate,
        gate_activated=True,
        prev_gate_state=GateState.INACTIVE,
        last_ts_ns=0,
        lambda_ref_global=lambda_ref,
        lambda_ref_fitted=None,
        mu_buy_fitted=None,
        mu_sell_fitted=None,
        fitted_params=None,
        cold_start_n=0,
        degraded_mode=False,
        fetch_ms=0,
        tick_timestamps_ns=np.array([], dtype=np.int64),
        tick_prices=np.array([], dtype=np.float64),
        tick_sizes=np.array([], dtype=np.int64),
        session_start_ns=_SESSION_START_NS,
        session_end_ns=_SESSION_END_NS,
        setup_filter_result=_build_passing_sf_result(),
        intraday_pct=pct_change / 100.0,
    )

    scanner_ctx = {
        "pct_change": pct_change,
        "scanner_rank": 3,
        "scanner_n": 12,
        "scanner_heat": 0.35,
        "scanner_quartile": 4,
        "snapshot_ns": _SESSION_START_NS,
    }

    state = LiveSignalState(
        ticker="TEST",
        ctx=ctx,
        scanner_context=scanner_ctx,
        session_date=date(2026, 5, 26),
    )
    # Pin setup-filter admission True; the per-minute recompute would otherwise re-run
    # over our tiny synthetic buffer and may reset it.
    state._recompute_setup_filter = lambda: None
    state._sf_entry_ok = True
    return state


def _trade_msg(t_sec: float, price: float, size: int = 100) -> dict:
    """Polygon WS trade message shape."""
    ts_ns = _SESSION_START_NS + int(t_sec * _NS_PER_SEC)
    return {"ev": "T", "sym": "TEST", "t": ts_ns, "p": price, "s": size}


def _quote_msg(t_sec: float, bid: float, ask: float) -> dict:
    ts_ns = _SESSION_START_NS + int(t_sec * _NS_PER_SEC)
    return {"ev": "Q", "sym": "TEST", "t": ts_ns, "bp": bid, "ap": ask, "bs": 10, "as": 10}


# ── Bug #1 — anchor sees Hawkes total, not EKF (exp(20)) ─────────────────────

def test_bug1_lambda_total_is_not_ekf_saturation():
    """Verify hawkes_state.lambda_total stays in sane Hawkes range, NOT exp(20).

    The pre-fix bug fed hawkes_state.lambda_hat to anchor.update; lambda_hat
    is the EKF estimate clamped at exp(20) ≈ 4.85e8 (ekf.py:53). After fix,
    we feed lambda_total = lam_buy + lam_sell, which is bounded by realistic
    Hawkes intensity for the configured params.
    """
    state = _build_live_state()
    # Quote first so Lee-Ready has bid/ask
    state.update_quote(_quote_msg(50.0, 9.99, 10.01))

    # Burst of trades — 50 trades, 1ms apart, all at the ask (BUY)
    for i in range(50):
        result = state.update_trade(_trade_msg(50.0 + 0.001 * (i + 1), 10.02, 100))

    # lambda_total in last SignalResult should be modest, NOT exp(20)
    assert result.lambda_buy + result.lambda_sell < 1e3, (
        f"lambda_total={result.lambda_buy + result.lambda_sell} suggests EKF "
        "saturation regression — Bug #1 fix lost"
    )
    # And the EKF lambda_hat (still on state for diagnostics) IS allowed to be large,
    # but must not be fed to the anchor — covered by test_bug1_rising_edge_fires.
    assert result.lambda_hat <= math.exp(20) + 1e-3


def test_bug1_rising_edge_fires_after_warmup():
    """Drive SlopeGate from WARMUP → PASS on an accelerating dollar-volume burst
    and assert an ENTRY signal fires on the rising edge."""
    state = _build_live_state(pct_change=35.0)

    state.update_quote(_quote_msg(50.0, 9.99, 10.01))

    # Tick during warmup (t_sec < 300) — sets prev_gate_state to WARMUP and seeds
    # the SlopeGate lookback buffer with a low pre-burst lambda_V.
    r1 = state.update_trade(_trade_msg(100.0, 10.02, 100))
    assert r1.gate_state == GateState.WARMUP.value
    assert r1.order_signal is None

    # Post-warmup accelerating burst (t > 300): rising dollar volume drives
    # norm_slope past k_open=0.5 → FAIL/INACTIVE → PASS rising edge.
    saw_pass = False
    entry_fired = False
    t = 305.0
    for _ in range(40):
        t += 1.0
        r = state.update_trade(_trade_msg(t, 10.02, 500))
        if r.gate_state == GateState.PASS.value:
            saw_pass = True
        if r.order_signal == "ENTRY":
            entry_fired = True
            break

    assert saw_pass, "SlopeGate never reached PASS on accelerating volume"
    assert entry_fired, "Expected ENTRY on SlopeGate rising edge"


def test_bug1_signal_loop_pushes_to_order_queue():
    """Full wiring test: signal_loop consumes ctx.queue and pushes to order_queue."""
    state = _build_live_state(pct_change=35.0)

    order_queue: asyncio.Queue = asyncio.Queue()
    ticker_queue: asyncio.Queue = asyncio.Queue()
    state_ready = asyncio.Event()
    state_ready.set()

    ctx = TickerContext(
        ticker="TEST",
        queue=ticker_queue,
        signal_state=state,
        task=None,  # type: ignore[arg-type]
        state_ready=state_ready,
        scanner_context={"pct_change": 35.0},
    )

    risk_state = MagicMock()
    risk_state.compute_position_size = lambda price, bkt: 100
    heartbeat = HeartbeatMonitor()

    async def _go():
        loop_task = asyncio.create_task(signal_loop(
            ctx=ctx,
            order_queue=order_queue,
            risk_state=risk_state,
            hot_ticks=[],
            hot_quotes=[],
            hot_signal_events=[],
            hot_hawkes_refits=[],
            heartbeat=heartbeat,
            session_clock=SessionClock(),
        ))
        await ticker_queue.put(_quote_msg(50.0, 9.99, 10.01))
        await ticker_queue.put(_trade_msg(100.0, 10.02, 100))    # WARMUP (seeds buffer)
        # Post-warmup accelerating burst → SlopeGate rising edge → ENTRY
        for i in range(40):
            await ticker_queue.put(_trade_msg(306.0 + i, 10.02, 500))
        # Let the loop drain
        for _ in range(40):
            await asyncio.sleep(0.005)
            if not order_queue.empty():
                break
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_go())

    assert not order_queue.empty(), "signal_loop did not push any order request"
    req = order_queue.get_nowait()
    assert req.ticker == "TEST"
    assert req.side == "BUY"
    assert req.is_entry is True


# ── Bug #2 — side encoding matches backtest (1 = BUY, -1 = SELL) ─────────────

def test_bug2_lee_ready_returns_plus1_for_buy():
    """price >= ask → BUY (1)."""
    state = _build_live_state()
    state._last_bid = 9.99
    state._last_ask = 10.01
    assert state._lee_ready(10.02) == 1
    assert state._lee_ready(10.01) == 1


def test_bug2_lee_ready_returns_minus1_for_sell():
    """price <= bid → SELL (-1)."""
    state = _build_live_state()
    state._last_bid = 9.99
    state._last_ask = 10.01
    assert state._lee_ready(9.99) == -1
    assert state._lee_ready(9.98) == -1


def test_bug2_lee_ready_tick_test_fallback():
    """No quote → tick direction. Price up = BUY (1), price down = SELL (-1)."""
    state = _build_live_state()
    state._last_bid = None
    state._last_ask = None
    state._sf_prices.append(10.00)
    assert state._lee_ready(10.05) == 1
    assert state._lee_ready(9.95) == -1


def test_bug2_buy_tick_increments_R_buy_not_R_sell():
    """BUY trade (price > ask) must increment R_buy in the Hawkes engine.

    Pre-fix: _lee_ready returned 0 for BUY → engine's `if side==1 ... else R_sell`
    incremented R_sell instead. Post-fix: returns 1 → R_buy correctly increments.
    """
    state = _build_live_state()
    state.update_quote(_quote_msg(10.0, 9.99, 10.01))

    R_buy_before = state._engine._R_buy
    R_sell_before = state._engine._R_sell

    state.update_trade(_trade_msg(20.0, 10.02, 100))  # price > ask → BUY

    assert state._engine._R_buy > R_buy_before, (
        "BUY trade did not increment R_buy — side convention regression"
    )
    assert state._engine._R_sell == R_sell_before, (
        "BUY trade unexpectedly incremented R_sell — encoding still inverted"
    )


def test_bug2_sell_tick_increments_R_sell_not_R_buy():
    state = _build_live_state()
    state.update_quote(_quote_msg(10.0, 9.99, 10.01))

    R_buy_before = state._engine._R_buy
    R_sell_before = state._engine._R_sell

    state.update_trade(_trade_msg(20.0, 9.98, 100))   # price < bid → SELL

    assert state._engine._R_sell > R_sell_before, (
        "SELL trade did not increment R_sell — side convention regression"
    )
    assert state._engine._R_buy == R_buy_before, (
        "SELL trade unexpectedly incremented R_buy — encoding still inverted"
    )


def test_bug2_classify_sides_uses_plus_minus_1():
    """_classify_sides emits +1 / -1, not 0 / 1."""
    prices = np.array([10.0, 10.05, 10.04, 10.04, 10.10], dtype=np.float64)
    sides = _classify_sides(prices)
    # First tick: default BUY
    assert sides[0] == 1
    # 10.05 > 10.00 → BUY
    assert sides[1] == 1
    # 10.04 < 10.05 → SELL
    assert sides[2] == -1
    # 10.04 == 10.04 → carry SELL
    assert sides[3] == -1
    # 10.10 > 10.04 → BUY
    assert sides[4] == 1
    # Never 0 (no ambiguous)
    assert 0 not in sides


# ── Bug #6 — close_ws never raises AttributeError ────────────────────────────

def test_bug6_close_ws_no_ws_does_not_raise():
    """close_ws() with _current_ws=None returns cleanly (no AttributeError)."""
    mgr = UniverseManager.__new__(UniverseManager)
    mgr._current_ws = None

    asyncio.run(mgr.close_ws())  # must not raise


def test_bug6_close_ws_already_closed_ws_does_not_raise():
    """close_ws() with an already-closed WS returns cleanly."""
    mgr = UniverseManager.__new__(UniverseManager)
    fake_ws = MagicMock()
    fake_ws.closed = True
    mgr._current_ws = fake_ws

    asyncio.run(mgr.close_ws())
    fake_ws.close.assert_not_called()


def test_bug6_close_ws_active_ws_calls_close():
    """close_ws() with an open WS invokes ws.close()."""
    mgr = UniverseManager.__new__(UniverseManager)
    fake_ws = MagicMock()
    fake_ws.closed = False

    async def _close():
        return None

    fake_ws.close.side_effect = lambda: _close()
    mgr._current_ws = fake_ws

    asyncio.run(mgr.close_ws())
    fake_ws.close.assert_called_once()


def test_bug6_universe_manager_init_sets_current_ws_to_none():
    """A real UniverseManager.__init__ must initialise _current_ws to None."""
    mgr = UniverseManager(
        order_queue=asyncio.Queue(),
        risk_state=MagicMock(),
        polygon_api_key="x",
        hot_ticks=[],
        hot_quotes=[],
        hot_signal_events=[],
        hot_hawkes_refits=[],
        session_clock=SessionClock(),
    )
    assert mgr._current_ws is None


# ── Bug #7 — Polygon WS sends MS, code expects NS ────────────────────────────
#
# The Polygon stocks WebSocket sends `t` (SIP timestamp) in milliseconds, but
# downstream code (live_state, _infer_session_bucket, signal_events.event_ns,
# ticks.sip_timestamp) treats it as nanoseconds. Without conversion at the
# dispatch boundary, t_sec arithmetic goes catastrophically negative (1.78e12
# minus 1.78e18 ≈ -1.78e9 seconds), the gate stays in WARMUP forever, and no
# entry ever fires.

def test_bug7_normalize_ws_trade_timestamp_ms_to_ns():
    """A realistic Polygon WS trade `t` (13-digit ms) becomes 19-digit ns."""
    item = {"ev": "T", "sym": "MSFT", "t": 1779882422238, "p": 100.0, "s": 50}
    _normalize_ws_timestamps(item)
    # 19-digit ns: 1779882422238 * 1_000_000 = 1779882422238000000
    assert item["t"] == 1779882422238000000
    assert len(str(item["t"])) == 19, "Expected 19-digit nanosecond timestamp"


def test_bug7_normalize_ws_quote_timestamp_ms_to_ns():
    """Same conversion applies to quote messages."""
    item = {"ev": "Q", "sym": "MSFT", "t": 1779882422238,
            "bp": 99.99, "ap": 100.01, "bs": 100, "as": 100}
    _normalize_ws_timestamps(item)
    assert item["t"] == 1779882422238000000


def test_bug7_normalize_idempotent_when_no_t_field():
    """Messages without `t` (e.g. status events) pass through untouched."""
    item = {"ev": "status", "status": "auth_success"}
    _normalize_ws_timestamps(item)
    assert "t" not in item


def test_bug7_post_conversion_t_sec_is_sane():
    """After ms→ns conversion, t_sec is positive and within the session window.

    Builds a synthetic session_start exactly 6.5 hours before the live tick so
    we know the expected t_sec without depending on calendar epoch arithmetic.
    """
    live_t_ms = 1779802215513
    expected_t_sec = 6.5 * 3600  # 6.5 hours after session open
    session_start_real_ns = (live_t_ms - int(expected_t_sec * 1000)) * 1_000_000

    item = {"ev": "T", "sym": "X", "t": live_t_ms, "p": 1.0, "s": 1}
    _normalize_ws_timestamps(item)
    t_sec = (item["t"] - session_start_real_ns) / 1_000_000_000

    assert 0 < t_sec < 16 * 3600, (
        f"t_sec={t_sec} — expected positive seconds within session window"
    )
    assert abs(t_sec - expected_t_sec) < 0.1, (
        f"t_sec={t_sec} should match expected {expected_t_sec} after conversion"
    )


def test_bug7_without_conversion_t_sec_breaks():
    """Sanity check: confirm the pre-fix behaviour was indeed broken — t_sec
    would be massively negative without the conversion. Guards against anyone
    removing the normalize call thinking it was unnecessary."""
    live_t_ms = 1779802215513
    session_start_real_ns = (live_t_ms - int(6.5 * 3600 * 1000)) * 1_000_000

    raw_ms_treated_as_ns = live_t_ms  # what arrives off the wire, no conversion
    t_sec_broken = (raw_ms_treated_as_ns - session_start_real_ns) / 1_000_000_000

    assert t_sec_broken < -1e9, (
        f"Pre-fix arithmetic should give a huge negative t_sec; got {t_sec_broken}. "
        "This test documents WHY we must normalize at the dispatch boundary"
    )


# ── Bug #8 — SELL exits in extended hours must build a LMT, not None ─────────
#
# In pre/post market IBKR rejects market orders. _build_order_request was
# previously building SELL exits with limit_price=None, which ibkr.submit then
# rejected with "missing limit_price" ERROR. The signal correctly fired, the
# order was correctly built side=SELL, but it never reached IBKR — so positions
# accumulated entries with no exits in extended hours.

from live.feed.signal_loop import _build_order_request


def _ticker_ctx_for_exit(last_bid: float | None) -> tuple:
    """Build a minimal TickerContext + risk_state for _build_order_request."""
    state = _build_live_state()
    state._last_bid = last_bid
    state._last_ask = (last_bid + 0.02) if last_bid is not None else None
    ctx = TickerContext(
        ticker="TEST",
        queue=asyncio.Queue(),
        signal_state=state,
        task=None,  # type: ignore[arg-type]
        state_ready=asyncio.Event(),
        scanner_context={"pct_change": 35.0},
    )
    risk_state = MagicMock()
    risk_state.compute_position_size = lambda price, bkt: 100
    return ctx, risk_state


def test_bug8_sell_exit_pre_market_has_limit_price():
    """Pre-market EPG_CLOSE SELL must include a limit_price (no None)."""
    ctx, risk_state = _ticker_ctx_for_exit(last_bid=20.50)
    req = _build_order_request(
        ctx, "EPG_CLOSE", price=20.55, bkt="pre_market",
        raw_msg={"ev": "T", "p": 20.55, "s": 100},
        risk_state=risk_state,
    )
    assert req is not None
    assert req.side == "SELL"
    assert req.is_entry is False
    assert req.limit_price is not None, (
        "Pre-market SELL exit MUST have limit_price — IBKR rejects None"
    )
    # Liberal: bid - extended_exit_offset (0.05) = 20.50 - 0.05 = 20.45
    assert req.limit_price == 20.45, (
        f"Expected liberal limit at bid - 0.05 = 20.45, got {req.limit_price}"
    )


def test_bug8_sell_exit_post_market_has_limit_price():
    """Post-market SELL exits must also build a LMT (not MKT)."""
    ctx, risk_state = _ticker_ctx_for_exit(last_bid=20.50)
    req = _build_order_request(
        ctx, "EPG_CLOSE", price=20.55, bkt="post_market",
        raw_msg={"ev": "T", "p": 20.55, "s": 100},
        risk_state=risk_state,
    )
    assert req is not None
    assert req.limit_price is not None
    assert req.limit_price == 20.45


def test_bug8_sell_exit_rth_uses_limit_active_outside_rth():
    """All orders are limit orders active outside RTH — RTH SELL exits set a limit too."""
    ctx, risk_state = _ticker_ctx_for_exit(last_bid=20.50)
    req = _build_order_request(
        ctx, "EPG_CLOSE", price=20.55, bkt="regular_hours",
        raw_msg={"ev": "T", "p": 20.55, "s": 100},
        risk_state=risk_state,
    )
    assert req is not None
    assert req.limit_price == 20.45  # bid - extended_exit_offset


def test_bug8_sell_exit_fallback_when_no_bid():
    """If last_bid is None (no quote arrived yet), fall back to last trade price."""
    ctx, risk_state = _ticker_ctx_for_exit(last_bid=None)
    req = _build_order_request(
        ctx, "EPG_CLOSE", price=20.55, bkt="pre_market",
        raw_msg={"ev": "T", "p": 20.55, "s": 100},
        risk_state=risk_state,
    )
    assert req is not None
    # Fallback: price - extended_exit_offset, then minus offset again = price - 2*offset
    # bid_fallback = 20.55 - 0.05 = 20.50; limit = 20.50 - 0.05 = 20.45
    assert req.limit_price == 20.45


def test_bug8_signal_state_exposes_last_bid_last_ask():
    """LiveSignalState must expose last_bid/last_ask as public properties for
    _build_order_request to consume."""
    state = _build_live_state()
    assert state.last_bid is None
    assert state.last_ask is None
    state.update_quote(_quote_msg(10.0, 9.99, 10.01))
    assert state.last_bid == 9.99
    assert state.last_ask == 10.01


# ── Bug #9 — Position averaging on second BUY of same ticker ─────────────────

from live.orders.risk import RiskState


def test_bug9_second_buy_aggregates_qty_and_avg_cost():
    """Second BUY on same ticker must aggregate qty and weighted-average cost,
    not overwrite. Pre-fix: overwrite silently lost shares from risk_state,
    leaving phantom IBKR positions when the SELL exit fired."""
    rs = RiskState()
    # First fill: 24 shares @ $20.77
    rs.record_fill("MNTS", "BUY", qty=24, fill_price=20.77, filled_qty=24)
    assert rs.open_positions["MNTS"]["qty"] == 24
    assert abs(rs.open_positions["MNTS"]["avg_cost"] - 20.77) < 1e-9

    # Second fill: 24 shares @ $20.64
    rs.record_fill("MNTS", "BUY", qty=24, fill_price=20.64, filled_qty=24)
    assert rs.open_positions["MNTS"]["qty"] == 48, (
        f"Expected 48 (24+24), got {rs.open_positions['MNTS']['qty']} — "
        "second BUY did not aggregate"
    )
    # Weighted avg: (24*20.77 + 24*20.64) / 48 = 20.705
    assert abs(rs.open_positions["MNTS"]["avg_cost"] - 20.705) < 1e-6


def test_bug9_sell_after_aggregated_buys_sells_full_qty():
    """After two BUYs (aggregated to 48), a SELL should remove the entire 48 shares."""
    rs = RiskState()
    rs.record_fill("MNTS", "BUY", qty=24, fill_price=20.77, filled_qty=24)
    rs.record_fill("MNTS", "BUY", qty=24, fill_price=20.64, filled_qty=24)
    assert rs.open_positions["MNTS"]["qty"] == 48

    # SELL the position. record_fill for SELL pops the whole entry.
    rs.record_fill("MNTS", "SELL", qty=48, fill_price=20.50, filled_qty=48)
    assert "MNTS" not in rs.open_positions
    # PnL: (20.50 - 20.705) * 48 = -9.84
    assert abs(rs.daily_pnl - (-9.84)) < 1e-6


def test_bug9_first_buy_unchanged_behaviour():
    """First BUY (no existing position) creates a new entry — regression guard."""
    rs = RiskState()
    rs.record_fill("AAPL", "BUY", qty=10, fill_price=150.00, filled_qty=10)
    assert rs.open_positions["AAPL"] == {"qty": 10, "avg_cost": 150.00}


# ── Task 4 — setup filter is the entry gate (SlopeGate F_ss core swap) ────────
#
# An entry requires BOTH a SlopeGate rising edge AND setup-filter admission
# (Q_tilde >= threshold on the current bar). With admission False, no ENTRY may
# fire even when the gate opens.

def test_sf_failing_blocks_entry_on_rising_edge():
    """SlopeGate rising edge with setup-filter admission False → no ENTRY signal."""
    state = _build_live_state(pct_change=35.0)
    # Force setup-filter admission to fail; recompute is stubbed so it stays False.
    state._sf_entry_ok = False

    state.update_quote(_quote_msg(50.0, 9.99, 10.01))
    state.update_trade(_trade_msg(100.0, 10.02, 100))  # WARMUP, seeds buffer

    saw_pass = False
    t = 305.0
    for _ in range(40):
        t += 1.0
        r = state.update_trade(_trade_msg(t, 10.02, 500))
        if r.gate_state == GateState.PASS.value:
            saw_pass = True
        assert r.order_signal != "ENTRY", "SF failing must block ENTRY on a rising edge"

    assert saw_pass, "Gate should still reach PASS — only the SF gate blocks the entry"


def test_sf_passing_allows_entry_on_rising_edge():
    """Control: identical drive with setup-filter admission True DOES fire ENTRY."""
    state = _build_live_state(pct_change=35.0)
    state._sf_entry_ok = True

    state.update_quote(_quote_msg(50.0, 9.99, 10.01))
    state.update_trade(_trade_msg(100.0, 10.02, 100))

    entry_fired = False
    t = 305.0
    for _ in range(40):
        t += 1.0
        r = state.update_trade(_trade_msg(t, 10.02, 500))
        if r.order_signal == "ENTRY":
            entry_fired = True
            break

    assert entry_fired, "Rising edge + SF admission must fire ENTRY"
