"""EPG live paper trading entry point. Launches all 3 processes in one asyncio loop."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from live.config import CFG
from live.db.pool import close_pool, init_pool
from live.db.writer import BatchWriter
from live.session_clock import SessionClock


def validate_config() -> None:
    """Abort immediately if any required environment variable is missing."""
    required = [
        "DB_URL",
        "POLYGON_API_KEY",
        "IBKR_HOST",
        "IBKR_PORT",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DATA_ROOT",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")


def _check_numba_cache() -> None:
    """Clear stale Numba .nbi/.nbc files when @nb.njit source has changed."""
    log = logging.getLogger(__name__)
    backtest_dir = Path(__file__).parent.parent / "backtest"
    if not backtest_dir.is_dir():
        return

    njit_files: list[Path] = []
    for py_file in sorted(backtest_dir.rglob("*.py")):
        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            if "@nb.njit" in content or "@numba.njit" in content:
                njit_files.append(py_file)
        except OSError:
            pass

    if not njit_files:
        return

    h = hashlib.sha256()
    for f in njit_files:
        h.update(f.read_bytes())
    current_hash = h.hexdigest()

    hash_file = backtest_dir / ".numba_cache_hash"
    if hash_file.exists():
        stored = hash_file.read_text().strip()
        if stored == current_hash:
            return
        log.warning("Numba source changed — clearing stale cache files")
        cleared = 0
        for ext in ("*.nbi", "*.nbc"):
            for f in backtest_dir.rglob(ext):
                try:
                    f.unlink()
                    cleared += 1
                except OSError:
                    pass
        log.info("Numba cache cleared: %d files removed", cleared)

    hash_file.write_text(current_hash)


async def _sentinel_heartbeat() -> None:
    """Touch /tmp/epg_alive every 10s so Docker HEALTHCHECK can verify process is alive."""
    sentinel = Path("/tmp/epg_alive")
    while True:
        try:
            sentinel.touch()
        except OSError:
            pass
        await asyncio.sleep(10)


async def _session_close_scheduler(universe_mgr, order_queue, risk_state, telegram) -> None:
    """Sleep until 20:00 ET each day, then run session-close sweep."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
    _log = logging.getLogger(__name__)
    while True:
        now_et = datetime.now(_ET)
        target_et = now_et.replace(hour=20, minute=0, second=0, microsecond=0)
        if now_et >= target_et:
            target_et += timedelta(days=1)
        secs = (target_et - now_et).total_seconds()
        _log.info("Session close scheduler: sleeping %.0fs until 20:00 ET", secs)
        await asyncio.sleep(secs)
        await universe_mgr.session_close(order_queue, risk_state, telegram)


async def _ibkr_watchdog(ibkr, telegram) -> None:
    """Poll IBKR connection every 15s. Reconnect and alert via Telegram on failure."""
    _log = logging.getLogger(__name__)
    _down = False
    while True:
        await asyncio.sleep(15)
        if ibkr.is_connected():
            if _down:
                _down = False
                await telegram.send_silent("IBKR reconnected")
            continue
        if not _down:
            _down = True
            _log.critical("IBKR connection lost — attempting reconnect")
            await telegram.send_silent("IBKR disconnected — attempting reconnect")
        else:
            _log.warning("IBKR still disconnected — retrying")
        try:
            await ibkr.connect()
            _down = False
            _log.info("IBKR reconnected successfully")
            await telegram.send_silent("IBKR reconnected")
        except Exception:
            _log.exception("IBKR reconnect failed — will retry in 15s")


def _setup_logging() -> None:
    from logging.handlers import TimedRotatingFileHandler
    log_dir = Path(CFG.logging.log_dir)
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{CFG.logging.log_prefix}.log"

    handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=CFG.logging.backup_count,
        encoding="utf-8",
    )
    handler.suffix = "%Y-%m-%d"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )


async def main() -> None:
    validate_config()
    _setup_logging()
    _check_numba_cache()
    log = logging.getLogger(__name__)
    log.info("EPG live system starting — strategy_id=%s", CFG.strategy_id)
    if CFG.epg_gate.variant == "participation_gate":
        log.info(
            "EPG gate: participation_gate (half_life=%.0fs peak_threshold_p=%.2f warmup=%.0fs)",
            CFG.epg.window_close_sec, CFG.epg.lambda_v_threshold, CFG.epg_gate.warmup_seconds,
        )
    else:
        log.info(
            "EPG gate: %s (mode=%s tau=%.0fs L=%.0fs k_open=%.2f k_close=%.2f warmup=%.0fs) "
            "[HEURISTIC/UNVALIDATED]",
            CFG.epg_gate.variant, CFG.epg_gate.mode, CFG.epg_gate.tau_sec, CFG.epg_gate.L_sec,
            CFG.epg_gate.k_open, CFG.epg_gate.k_close, CFG.epg_gate.warmup_seconds,
        )
    log.info(
        "Setup filter is the entry gate: q_threshold=%.2f admission_bars=%d removal_bars=%d "
        "warmup_provisional=%.2f/%d bars",
        CFG.setup_filter.q_threshold, CFG.setup_filter.admission_bars, CFG.setup_filter.removal_bars,
        CFG.setup_filter.warmup_provisional_threshold, CFG.setup_filter.warmup_bars,
    )
    log.info(
        "Exits: EXIT_D enabled=%s, LULD enabled=%s — strategy exit is EPG_CLOSE",
        CFG.exit_d.enabled, CFG.luld.enabled,
    )
    log.info("Scanner: quartile gate removed — gap >= %.0f%% admits all quartiles",
             CFG.scanner.gap_threshold * 100)

    # Register SIGTERM/SIGINT so Docker stop / Ctrl-C flushes cleanly.
    # add_signal_handler is Linux-only; on Windows (dev) it raises NotImplementedError.
    loop = asyncio.get_running_loop()
    _shutdown = asyncio.Event()
    try:
        loop.add_signal_handler(signal.SIGTERM, _shutdown.set)
        loop.add_signal_handler(signal.SIGINT, _shutdown.set)
    except NotImplementedError:
        pass

    # Check kill flag at startup
    kill_flag = Path(__file__).parent / "kill.flag"
    if kill_flag.exists():
        log.critical("kill.flag exists at startup — aborting")
        sys.exit(1)

    # Init DB pool
    pool = await init_pool()

    # Init IBKR — retry with backoff so the container survives a slow Gateway start
    from live.orders.ibkr import IBKRClient
    ibkr = IBKRClient()
    _IBKR_MAX_ATTEMPTS = 12
    _IBKR_RETRY_DELAY_S = 10
    for _attempt in range(1, _IBKR_MAX_ATTEMPTS + 1):
        try:
            await ibkr.connect()
            break
        except Exception as _exc:
            if _attempt == _IBKR_MAX_ATTEMPTS:
                log.critical(
                    "IBKR connection failed after %d attempts — aborting. "
                    "Make sure IB Gateway is running and API port %s is open.",
                    _IBKR_MAX_ATTEMPTS,
                    os.environ.get("IBKR_PORT", "4002"),
                )
                await close_pool()
                sys.exit(1)
            log.warning(
                "IBKR connect attempt %d/%d failed (%s) — retrying in %ds",
                _attempt, _IBKR_MAX_ATTEMPTS, _exc, _IBKR_RETRY_DELAY_S,
            )
            await asyncio.sleep(_IBKR_RETRY_DELAY_S)

    # Init risk state
    from live.orders.risk import RiskState
    risk_state = RiskState()

    # Init Telegram (constructed early so crash recovery can alert; kill/bot
    # callbacks are registered later once the order_queue exists).
    from live.alerts.telegram import TelegramBot, execute_kill_sequence, kill_flag_watcher
    telegram = TelegramBot(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        chat_id=os.environ["TELEGRAM_CHAT_ID"],
    )

    clock = SessionClock()

    # ── Crash recovery — the single startup path back to a flat state ──
    # Runs after IBKR connects but before any signal loops / scanner polling.
    # A crash is functionally a dead man's switch trigger: cancel all open
    # orders, flatten all open positions, reconcile the DB to flat. No smart
    # resume — EPG windows are 30-120s and any restart outlives them.
    from live.recovery import run_crash_recovery
    try:
        recovery = await run_crash_recovery(ibkr.ib, pool, telegram, clock.date)
    except Exception:
        log.critical("Crash recovery raised — halting startup", exc_info=True)
        await telegram.send_silent("🔴 STARTUP HALTED — crash recovery raised an exception.")
        await close_pool()
        await ibkr.disconnect()
        sys.exit(1)

    if recovery.stuck_tickers or recovery.error_tickers:
        log.critical(
            "Crash recovery could not reach flat — stuck=%s errors=%s. HALTING startup; "
            "manual intervention required.",
            recovery.stuck_tickers, recovery.error_tickers,
        )
        await telegram.send_silent(
            "🔴 STARTUP HALTED — crash recovery left open positions "
            f"(stuck={recovery.stuck_tickers}, errors={recovery.error_tickers}). "
            "Manual intervention required."
        )
        await close_pool()
        await ibkr.disconnect()
        sys.exit(1)
    if recovery.deferred_tickers:
        log.warning(
            "Crash recovery deferred (market closed): %s — flagged for manual review",
            recovery.deferred_tickers,
        )
        risk_state.manual_review_required.update(recovery.deferred_tickers)
    elif recovery.closed_tickers:
        log.warning("Crash recovery closed %d position(s): %s",
                    len(recovery.closed_tickers), recovery.closed_tickers)
    elif not recovery.had_open_positions:
        log.info("Crash recovery: no open positions found — clean start")

    # Seed account equity and buying power.
    risk_state.account_equity = await ibkr.get_account_equity()
    risk_state.account_buying_power = await ibkr.get_buying_power()
    if risk_state.account_equity > 0:
        risk_state.theoretical_equity = risk_state.account_equity
        log.info("Account equity: $%.2f  Buying power: $%.2f",
                 risk_state.account_equity, risk_state.account_buying_power)
    else:
        log.warning("Account equity unavailable at startup — buying_power mode will use equity * leverage fallback")

    # Shared state
    universe_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    order_queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    # Shared hot buffers: signal_loop appends, BatchWriter flushes (same list objects)
    hot_ticks: list = []
    hot_quotes: list = []
    hot_signal_events: list = []
    hot_hawkes_refits: list = []
    writer = BatchWriter(hot_ticks, hot_quotes, hot_signal_events, hot_hawkes_refits)

    # Kill sequence callback
    async def kill_callback() -> None:
        await execute_kill_sequence(ibkr, risk_state, telegram, order_queue)

    telegram.register_kill_callback(kill_callback)

    # Universe manager (Process 2 — WebSocket + signal loops)
    from live.feed.universe import UniverseManager
    universe_mgr = UniverseManager(
        order_queue=order_queue,
        risk_state=risk_state,
        polygon_api_key=os.environ["POLYGON_API_KEY"],
        hot_ticks=hot_ticks,
        hot_quotes=hot_quotes,
        hot_signal_events=hot_signal_events,
        hot_hawkes_refits=hot_hawkes_refits,
        session_clock=clock,
        telegram=telegram,
    )

    # Wire bot state (bot reads shared objects — never writes)
    from live.scanner_monitor import _last_poll_t as scanner_last_poll_t
    from live.orders.worker import _last_wake_t as worker_last_wake_t
    from live.bot.bot import BotState
    bot_state = BotState(
        universe=universe_mgr._universe,
        closed_today=universe_mgr._closed_today,
        risk_state=risk_state,
        order_queue=order_queue,
        pool=pool,
        ibkr=ibkr,
        polygon_api_key=os.environ["POLYGON_API_KEY"],
        heartbeat=universe_mgr._heartbeat,
        scanner_last_poll_t=scanner_last_poll_t,
        ws_last_msg_t=universe_mgr.ws_last_msg_t,
        worker_last_wake_t=worker_last_wake_t,
        session_clock=clock,
    )
    telegram.register_bot_state(bot_state)

    # Order worker (Process 3)
    from live.orders.worker import hourly_pnl_alert, order_worker, pending_close_monitor

    # Account equity + buying power refresher — updates every 5 minutes
    async def equity_refresher() -> None:
        import asyncio as _asyncio
        while True:
            await _asyncio.sleep(300)
            try:
                equity = await ibkr.get_account_equity()
                buying_power = await ibkr.get_buying_power()
                if equity > 0:
                    risk_state.account_equity = equity
                if buying_power > 0:
                    risk_state.account_buying_power = buying_power
                log.debug("Equity refreshed: $%.2f  Buying power: $%.2f", equity, buying_power)
            except Exception:
                log.exception("Failed to refresh account equity/buying power from IBKR")

    # ── Critical tasks — failure halts the system ──
    critical_tasks = [
        asyncio.create_task(
            universe_mgr.run(universe_queue),
            name="universe_manager",
        ),
        asyncio.create_task(
            order_worker(order_queue, risk_state, ibkr, telegram, clock),
            name="order_worker",
        ),
        asyncio.create_task(writer.run(), name="batch_writer"),
        asyncio.create_task(
            __import__("live.scanner_monitor", fromlist=["scanner_loop"]).scanner_loop(
                universe_queue,
                os.environ["POLYGON_API_KEY"],
                universe_mgr=universe_mgr,
                closed_today=universe_mgr.closed_today,
            ),
            name="scanner_monitor",
        ),
        asyncio.create_task(
            _session_close_scheduler(universe_mgr, order_queue, risk_state, telegram),
            name="session_close_scheduler",
        ),
    ]

    # ── Supervised tasks — retry handled internally; failure logged, not system-halting ──
    supervised_tasks = [
        asyncio.create_task(telegram.start_polling(), name="telegram_bot"),
        asyncio.create_task(kill_flag_watcher(kill_callback), name="kill_watcher"),
        asyncio.create_task(_ibkr_watchdog(ibkr, telegram), name="ibkr_watchdog"),
        asyncio.create_task(equity_refresher(), name="equity_refresher"),
        asyncio.create_task(_sentinel_heartbeat(), name="sentinel_heartbeat"),
        asyncio.create_task(
            pending_close_monitor(risk_state, order_queue, telegram, ibkr),
            name="pending_close_monitor",
        ),
        asyncio.create_task(
            hourly_pnl_alert(risk_state, telegram, universe_mgr._universe),
            name="pnl_reporter",
        ),
    ]

    await telegram.send_silent(
        f"EPG live system started — {clock.date.isoformat()} — paper trading"
    )

    async def _await_shutdown() -> None:
        await _shutdown.wait()
        raise SystemExit(0)

    _shutdown_task = asyncio.create_task(_await_shutdown(), name="shutdown_signal")

    try:
        # FIRST_EXCEPTION on critical tasks only — supervised failures are handled internally
        done, _ = await asyncio.wait(
            critical_tasks + [_shutdown_task],
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            if task is _shutdown_task:
                log.info("Shutdown signal received — beginning clean shutdown")
                continue
            if not task.cancelled() and task.exception():
                log.critical(
                    "Task %s raised: %s", task.get_name(), task.exception(),
                    exc_info=task.exception(),
                )
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown signal received")
    finally:
        for task in critical_tasks + supervised_tasks + [_shutdown_task]:
            task.cancel()
        await asyncio.gather(
            *critical_tasks, *supervised_tasks, _shutdown_task,
            return_exceptions=True,
        )
        await writer.flush()
        log.info("clean shutdown — buffer flushed")
        await close_pool()
        await ibkr.disconnect()
        log.info("EPG live system stopped")


if __name__ == "__main__":
    asyncio.run(main())
