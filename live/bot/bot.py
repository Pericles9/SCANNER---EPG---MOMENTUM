"""BotState dataclass and handler registration for the Telegram bot."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telegram.ext import Application, CommandHandler

from live.bot import handlers


@dataclass
class BotState:
    universe: dict
    closed_today: set
    risk_state: Any
    order_queue: Any
    pool: Any
    ibkr: Any
    polygon_api_key: str
    heartbeat: Any
    scanner_last_poll_t: list   # [float] — mutable box
    ws_last_msg_t: list         # [float] — mutable box
    worker_last_wake_t: list    # [float] — mutable box


def setup_bot_handlers(app: Application, state: BotState) -> None:
    """Register all bot command handlers on the Application."""
    app.bot_data["state"] = state
    app.add_handler(CommandHandler("universe", handlers.universe))
    app.add_handler(CommandHandler("trades", handlers.trades))
    app.add_handler(CommandHandler("status", handlers.status))
    app.add_handler(CommandHandler("services", handlers.services))
    app.add_handler(CommandHandler("position", handlers.position))
    app.add_handler(CommandHandler("risk", handlers.risk))
    app.add_handler(CommandHandler("scanner", handlers.scanner))
    app.add_handler(CommandHandler("help", handlers.help_cmd))
