"""Whitelist-проверка по user_id."""
from __future__ import annotations

from functools import wraps
from typing import Callable

from loguru import logger
from telegram import Update
from telegram.ext import ContextTypes

from client.config import settings


def _is_allowed(uid: int) -> bool:
    allowed = settings.allowed_user_ids
    if not allowed:
        # Пусто = открытый доступ. Логируем уязвимость, но не блокируем.
        logger.warning("TELEGRAM_ALLOWED_USER_IDS пуст — бот открыт всем")
        return True
    return uid in allowed


def whitelist_only(func: Callable):
    """Декоратор: пускаем хендлер только если user_id в whitelist."""

    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        uid = user.id if user else 0
        if not _is_allowed(uid):
            logger.warning(f"DENY uid={uid} username=@{user.username if user else '?'}")
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"⛔ Доступ запрещён. Передай админу свой user_id: `{uid}`",
                    parse_mode="Markdown",
                )
            return
        return await func(update, ctx, *args, **kwargs)

    return wrapper
