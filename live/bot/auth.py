"""Authorization decorator for Telegram bot handlers."""
from __future__ import annotations

import os

from telegram import Update
from telegram.ext import ContextTypes


def authorised_only(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not chat_id:
            return
        if update.effective_chat is None:
            return
        if str(update.effective_chat.id) != chat_id:
            return
        await handler(update, context)
    return wrapper
