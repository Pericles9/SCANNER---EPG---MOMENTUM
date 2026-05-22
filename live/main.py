"""EPG live paper trading entry point. Launches all 3 processes in one asyncio loop."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
from datetime import date
from pathlib import Path

from live.config import CFG
from live.db.pool import close_pool, init_pool
from live.db.writer import BatchWriter


def _setup_logging() -> None:
    log_dir = Path(CFG.logging.log_dir)
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"{CFG.logging.log_prefix}_{date.today().isoformat()}.log"

    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=CFG.logging.max_bytes,
        backupCount=CFG.logging.backup_count,
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )


async def _reconcile_positions(ibkr, risk_state, pool) -> bool:
    """Compare IBKR positions to DB. Return False if mismatch (halt signal).

    Set SKIP_POSITION_CHECK=true to downgrade halt to a warning — use only on
    initial setup or after manually reconciling stale paper-account positions.
    """
    skip = os.environ.get("SKIP_POSITION_CHECK", "").lower() in ("1", "true", "yes")

    ibkr_positions = ibkr.get_open_positions()
    session_date = date.today()

    async with pool.acquire() as conn:
        db_rows = await conn.fetch(
            """
            SELECT ticker, qty FROM positions
            WHERE strategy_id=$1 AND session_date=$2
            """,
            CFG.strategy_id, session_date,
        )

    db_positions = {r["ticker"]: r["qty"] for r in db_rows}

    all_tickers = set(ibkr_positions) | set(db_positions)
    mismatches: list[str] = []
    for ticker in all_tickers:
        ibkr_qty, _ = ibkr_positions.get(ticker, (0, 0.0))
        db_qty = db_positions.get(ticker, 0)
        if ibkr_qty != db_qty:
            logging.critical(
                "POSITION MISMATCH: %s IBKR=%d DB=%d", ticker, ibkr_qty, db_qty
            )
            mismatches.append(ticker)

    if mismatches:
        if skip:
            logging.warning(
                "SKIP_POSITION_CHECK=true — %d mismatch(es) ignored, seeding IBKR as truth. "
                "Resolve stale positions before next live session.",
                len(mismatches),
            )
        else:
            return False

    # Seed risk_state with IBKR positions (source of truth on startup)
    for ticker, (qty, avg_cost) in ibkr_positions.items():
        risk_state.open_positions[ticker] = {"qty": qty, "avg_cost": avg_cost}

    return True


async def main() -> None:
    _setup_logging()
    log = logging.getLogger(__name__)
    log.info("EPG live system starting — strategy_id=%s", CFG.strategy_id)

    # Check kill flag at startup
    kill_flag = Path(__file__).parent / "kill.flag"
    if kill_flag.exists():
        log.critical("kill.flag exists at startup — aborting")
        sys.exit(1)

    # Init DB pool
    pool = await init_pool()

    # Init IBKR
    from live.orders.ibkr import IBKRClient
    ibkr = IBKRClient()
    await ibkr.connect()

    # Init risk state
    from live.orders.risk import RiskState
    risk_state = RiskState()

    # Reconcile positions
    if not await _reconcile_positions(ibkr, risk_state, pool):
        log.critical("IBKR position mismatch — halting. Reconcile manually before restarting.")
        await close_pool()
        sys.exit(1)

    # Seed account equity (theoretical equity starts equal to account equity)
    risk_state.account_equity = await ibkr.get_account_equity()
    if risk_state.account_equity > 0:
        risk_state.theoretical_equity = risk_state.account_equity
        log.info("Account equity at startup: $%.2f", risk_state.account_equity)
    else:
        log.warning("Account equity unavailable at startup — Kelly sizing will use flat fallback")

    # Init Telegram
    from live.alerts.telegram import TelegramBot, execute_kill_sequence, kill_flag_watcher
    telegram = TelegramBot(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        chat_id=os.environ["TELEGRAM_CHAT_ID"],
    )

    # Shared state
    session_date = date.today()
    universe_queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    order_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    writer = BatchWriter()

    # Shared hot buffers (signal_loop appends; writer flushes)
    hot_ticks: list = []
    hot_quotes: list = []
    hot_signal_events: list = []
    hot_hawkes_refits: list = []

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
        session_date=session_date,
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
    )
    telegram.register_bot_state(bot_state)

    # Order worker (Process 3)
    from live.orders.worker import hourly_pnl_alert, order_worker

    # Account equity refresher — updates every 5 minutes for Kelly sizing
    async def equity_refresher() -> None:
        import asyncio as _asyncio
        while True:
            await _asyncio.sleep(300)
            try:
                equity = await ibkr.get_account_equity()
                if equity > 0:
                    risk_state.account_equity = equity
                    log.debug("Account equity refreshed: $%.2f", equity)
            except Exception:
                log.exception("Failed to refresh account equity from IBKR")

    # Launch all tasks
    tasks = [
        asyncio.create_task(
            __import__("live.scanner_monitor", fromlist=["scanner_loop"]).scanner_loop(
                universe_queue, os.environ["POLYGON_API_KEY"]
            ),
            name="scanner_monitor",
        ),
        asyncio.create_task(
            universe_mgr.run(universe_queue),
            name="universe_manager",
        ),
        asyncio.create_task(
            order_worker(order_queue, risk_state, ibkr, telegram, session_date),
            name="order_worker",
        ),
        asyncio.create_task(writer.run(), name="batch_writer"),
        asyncio.create_task(kill_flag_watcher(kill_callback), name="kill_watcher"),
        asyncio.create_task(telegram.start_polling(), name="telegram_bot"),
        asyncio.create_task(
            hourly_pnl_alert(risk_state, telegram, universe_mgr._universe),
            name="pnl_reporter",
        ),
        asyncio.create_task(equity_refresher(), name="equity_refresher"),
    ]

    await telegram.send_silent(
        f"EPG live system started — {session_date.isoformat()} — paper trading"
    )

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exc = task.exception()
            if exc:
                log.critical("Task %s raised: %s", task.get_name(), exc, exc_info=exc)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown signal received")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_pool()
        await ibkr.disconnect()
        log.info("EPG live system stopped")


if __name__ == "__main__":
    asyncio.run(main())
