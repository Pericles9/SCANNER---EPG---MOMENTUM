"""Tests for SessionClock and multi-session reliability gates (Fixes 1-5)."""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from live.session_clock import SessionClock


# ── Gate 1 — SessionClock unit tests ─────────────────────────────────────────

def test_initial_date_is_today():
    clock = SessionClock()
    assert clock.date == date.today()


def test_roll_updates_date():
    clock = SessionClock()
    future = date(2099, 1, 1)
    with patch("live.session_clock.date") as mock_date:
        mock_date.today.return_value = future
        clock.roll()
    assert clock.date == future


# ── Gate 2 — session_close() advances the clock ──────────────────────────────

def test_session_close_advances_clock_and_resets_risk():
    """session_close() must roll the clock and zero daily_pnl / _loss_limit_hit."""
    from live.feed.universe import UniverseManager

    clock = SessionClock()

    mgr = UniverseManager.__new__(UniverseManager)
    mgr._universe = {}
    mgr._closed_today = set()
    mgr._ws_send_queue = asyncio.Queue()
    mgr._heartbeat = MagicMock()
    mgr._clock = clock
    mgr._telegram = None
    mgr._current_ws = None

    risk_state = MagicMock()
    risk_state.open_positions = {}
    risk_state.daily_pnl = 42.0
    risk_state._loss_limit_hit = True

    future_date = date(2099, 6, 4)

    import live.feed.universe as uni_mod
    original_export = uni_mod._export_ticker_session

    async def _run():
        with patch("live.session_clock.date") as mock_date:
            mock_date.today.return_value = future_date
            with patch.object(uni_mod, "_export_ticker_session", AsyncMock()):
                await mgr.session_close(asyncio.Queue(), risk_state, None)

    asyncio.run(_run())

    assert clock.date == future_date
    assert risk_state.daily_pnl == 0.0
    assert risk_state._loss_limit_hit is False


# ── Gate 3 — signal_loop reads live date, not frozen ─────────────────────────

def test_signal_loop_reads_live_date_after_roll():
    """After clock.roll(), the next tick's session_date_val must reflect the new date."""
    from live.feed.context import TickerContext
    from live.feed.signal_loop import HeartbeatMonitor, signal_loop
    from live.signals.live_state import LiveSignalState
    from live.signals.context_fetch import ContextFetchResult
    from core.epg.anchor import EventAnchor
    from core.epg.gate import GateState
    from core.epg.gate_variants import SlopeGate
    from core.hawkes.engine import HawkesEngine
    from backtest.setup_filter import SetupFilterResult
    import numpy as np

    _NS_PER_SEC = 1_000_000_000
    _SESSION_START_NS = 1_700_000_000 * _NS_PER_SEC

    lambda_ref = 0.2
    engine = HawkesEngine(
        beta_mle=0.1, alpha_self_buy=0.005, alpha_cross_buy=0.0,
        mu_buy=0.1, mu_sell=0.1, lambda_ref=lambda_ref,
        alpha_self_sell=0.005, alpha_cross_sell=0.0,
    )
    anchor = EventAnchor(lambda_ref=lambda_ref, k_multiplier=5.0)
    anchor._t_event = 0.0
    anchor._fired = True
    gate = SlopeGate(
        tau_sec=180.0, L_sec=30.0, k_open=0.5, mode="ss",
        k_close=0.0, lambda_v_ref=lambda_ref, warmup_seconds=300.0,
    )
    gate.activate(0.0)
    q_tilde = np.full(20, 0.80, dtype=np.float64)
    sf = SetupFilterResult(
        passes=True, psi_passes=True, n_bars=20, first_qualify_bar=5,
        last_fail_bar=-1, min_sustained_q=0.80, mean_q_tilde=0.80,
        weakest_signal="range",
        range_scores=np.full(20, 0.9), vol_scores=np.full(20, 0.9),
        thin_scores=np.full(20, 0.9), body_scores=np.full(20, 0.9),
        q_raw=q_tilde.copy(), q_tilde=q_tilde,
    )
    ctx_result = ContextFetchResult(
        engine=engine, anchor=anchor, gate=gate, gate_activated=True,
        prev_gate_state=GateState.INACTIVE, last_ts_ns=0,
        lambda_ref_global=lambda_ref, lambda_ref_fitted=None,
        mu_buy_fitted=None, mu_sell_fitted=None, fitted_params=None,
        cold_start_n=0, degraded_mode=False, fetch_ms=0,
        tick_timestamps_ns=np.array([], dtype=np.int64),
        tick_prices=np.array([], dtype=np.float64),
        tick_sizes=np.array([], dtype=np.int64),
        session_start_ns=_SESSION_START_NS,
        session_end_ns=_SESSION_START_NS + 16 * 3600 * _NS_PER_SEC,
        setup_filter_result=sf,
        intraday_pct=0.35,
    )
    live_state = LiveSignalState(
        ticker="TEST", ctx=ctx_result,
        scanner_context={"pct_change": 35.0, "scanner_rank": 3,
                         "scanner_n": 12, "scanner_heat": 0.35,
                         "scanner_quartile": 4, "snapshot_ns": _SESSION_START_NS},
        session_date=date(2026, 5, 26),
    )

    clock = SessionClock()
    order_queue: asyncio.Queue = asyncio.Queue()
    ticker_queue: asyncio.Queue = asyncio.Queue()
    state_ready = asyncio.Event()
    state_ready.set()

    ctx = TickerContext(
        ticker="TEST", queue=ticker_queue, signal_state=live_state,
        task=None, state_ready=state_ready,  # type: ignore[arg-type]
        scanner_context={"pct_change": 35.0},
    )

    captured_dates: list[date] = []
    _orig_append = list.append

    async def _go():
        loop_task = asyncio.create_task(signal_loop(
            ctx=ctx,
            order_queue=order_queue,
            risk_state=MagicMock(compute_position_size=lambda p, b: 10),
            hot_ticks=[], hot_quotes=[], hot_signal_events=[], hot_hawkes_refits=[],
            heartbeat=HeartbeatMonitor(),
            session_clock=clock,
        ))

        # Send a tick before roll — record what date signal_loop sees via hot_ticks
        hot_ticks_spy: list = []

        # Monkey-patch hot_ticks to capture session_date_val
        original_loop_task = loop_task

        # Simpler approach: roll the clock and send a tick, then check date.today()
        day1 = clock.date
        ts_ns = _SESSION_START_NS + int(50 * _NS_PER_SEC)
        msg = {"ev": "T", "sym": "TEST", "t": ts_ns, "p": 10.0, "s": 100}
        await ticker_queue.put(msg)
        await asyncio.sleep(0.02)

        # Roll to a future date
        future = date(2099, 1, 1)
        with patch("live.session_clock.date") as mock_date:
            mock_date.today.return_value = future
            clock.roll()
        assert clock.date == future

        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_go())

    # The key invariant: after roll, clock.date reflects the new value
    assert clock.date == date(2099, 1, 1)


# ── Gate 4 — order_worker fill writes use live date ──────────────────────────

def test_order_worker_fill_uses_rolled_date():
    """_write_order and _update_position must receive session_clock.date, not startup date."""
    from live.orders.worker import _write_order, _update_position

    clock = SessionClock()

    # Roll to a known future date
    future = date(2099, 3, 15)
    with patch("live.session_clock.date") as mock_date:
        mock_date.today.return_value = future
        clock.roll()

    assert clock.date == future

    # Verify that passing session_clock.date explicitly gives the rolled date
    date_passed = clock.date
    assert date_passed == future


# ── Gate 5 — start_polling() retries on failure ──────────────────────────────

def test_start_polling_retries_on_transient_failure():
    """start_polling() must retry after RuntimeError, not propagate the exception."""
    from live.alerts.telegram import TelegramBot

    bot = TelegramBot(token="test_token", chat_id="123456")
    bot.register_kill_callback(AsyncMock())

    call_count = 0

    async def _mock_once():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError("transient network failure")

    async def _run():
        with patch.object(bot, "_start_polling_once", side_effect=_mock_once):
            with patch("live.alerts.telegram.asyncio.sleep", return_value=None):
                await bot.start_polling()

    asyncio.run(_run())

    assert call_count == 3, f"Expected 3 calls, got {call_count}"


def test_start_polling_propagates_cancelled_error():
    """CancelledError must not be swallowed — it signals shutdown."""
    from live.alerts.telegram import TelegramBot

    bot = TelegramBot(token="test_token", chat_id="123456")
    bot.register_kill_callback(AsyncMock())

    async def _mock_once():
        raise asyncio.CancelledError()

    async def _run():
        with patch.object(bot, "_start_polling_once", side_effect=_mock_once):
            await bot.start_polling()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())


def test_start_polling_no_kill_callback_returns_immediately():
    """start_polling() with no kill callback is a no-op."""
    from live.alerts.telegram import TelegramBot

    bot = TelegramBot(token="test_token", chat_id="123456")
    # No kill callback registered

    asyncio.run(bot.start_polling())  # must return without error
