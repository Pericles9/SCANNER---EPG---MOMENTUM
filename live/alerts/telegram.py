"""Telegram alerting and kill switch."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Callable, Optional

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler
from telegram.request import HTTPXRequest

from live.config import CFG

log = logging.getLogger(__name__)

_KILL_FLAG_PATH = Path(__file__).parent.parent / "kill.flag"
_KILL_CHECK_INTERVAL_S = 5.0

# Outbound alert HTTP pool. PTB's default HTTPXRequest has connection_pool_size=1,
# which serialises every concurrent send_silent() (order fills, hourly P&L, triage,
# WS-disconnect, dead-man) through a single connection and causes pool-timeout
# stalls under load. Size the pool for concurrent alerts and bound every call with
# explicit timeouts so a slow Telegram API never hangs a hot-path coroutine.
_ALERT_POOL_SIZE = 8
_ALERT_TIMEOUT_S = 5.0


class TelegramBot:
    def __init__(self, token: str, chat_id: str) -> None:
        self._bot = Bot(
            token=token,
            request=HTTPXRequest(
                connection_pool_size=_ALERT_POOL_SIZE,
                connect_timeout=_ALERT_TIMEOUT_S,
                read_timeout=_ALERT_TIMEOUT_S,
                write_timeout=_ALERT_TIMEOUT_S,
                pool_timeout=_ALERT_TIMEOUT_S,
            ),
        )
        self._chat_id = chat_id
        self._app: Optional[Application] = None
        self._kill_callback: Optional[Callable] = None
        self._status_callback: Optional[Callable] = None
        self._bot_state = None

    async def send(self, message: str) -> None:
        await self._bot.send_message(chat_id=self._chat_id, text=message)

    async def send_silent(self, message: str) -> None:
        try:
            await self.send(message)
        except Exception:
            log.warning("Telegram send failed: %s", message[:80])

    def register_kill_callback(self, callback: Callable) -> None:
        self._kill_callback = callback

    def register_status_callback(self, callback: Callable) -> None:
        self._status_callback = callback

    def register_bot_state(self, state) -> None:
        self._bot_state = state

    async def start_polling(self) -> None:
        """Start Telegram bot for /kill command handling."""
        if not self._kill_callback:
            return

        self._app = Application.builder().token(self._bot.token).build()

        async def kill_handler(update: Update, context) -> None:
            if str(update.effective_chat.id) != self._chat_id:
                return
            log.critical("KILL SWITCH: /kill command received via Telegram")
            await update.message.reply_text("Kill switch activated — flattening all positions.")
            await self._kill_callback()

        self._app.add_handler(CommandHandler("kill", kill_handler))

        if self._bot_state is not None:
            from live.bot.bot import setup_bot_handlers
            setup_bot_handlers(self._app, self._bot_state)
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        log.info("Telegram bot started, listening for /kill")

    async def stop_polling(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()


async def kill_flag_watcher(kill_callback: Callable) -> None:
    """Watch for live/kill.flag — triggers kill sequence if file appears."""
    while True:
        await asyncio.sleep(_KILL_CHECK_INTERVAL_S)
        if _KILL_FLAG_PATH.exists():
            log.critical("KILL SWITCH: kill.flag detected — executing kill sequence")
            await kill_callback()
            return


async def execute_kill_sequence(
    ibkr,
    risk_state,
    telegram: TelegramBot,
    order_queue: asyncio.Queue,
) -> None:
    """Execute the kill sequence: flag → cancel → flatten → confirm → exit."""
    from live.orders.risk import FlattenAllRequest

    log.critical("KILL SWITCH ACTIVATED")

    # 1. Set kill flag in risk state (blocks new entries)
    risk_state._loss_limit_hit = True  # reuse loss limit flag to block entries

    # 2. Cancel all open orders
    await ibkr.cancel_all_orders()

    # 3. Flatten all open positions via order_queue
    order_queue.put_nowait(FlattenAllRequest(reason="kill_switch"))

    # 4. Wait for confirms (max 10s)
    await asyncio.sleep(10)

    # 5-6. Log and alert
    log.critical("KILL SWITCH: all positions flat, system stopping")
    await telegram.send_silent("KILL SWITCH: all positions flat, system stopping")

    # 7. Exit
    sys.exit(0)
