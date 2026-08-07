"""Shared handler pieces: owner gating and the keyboard."""
import logging

from aiogram import BaseMiddleware
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, TelegramObject, WebAppInfo

from bot import config

logger = logging.getLogger("bot.handlers")


class OwnerOnlyMiddleware(BaseMiddleware):
    """Drop every update that isn't from the owner.

    This is a single-user system. The bot's username is discoverable, so
    strangers can and will message it; without this they'd be able to read
    sensor history and trigger watering. Filtering here rather than per-handler
    means a new handler cannot forget to check.
    """

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None or user.id != config.OWNER_USER_ID:
            if user is not None:
                logger.warning("ignored update from non-owner user_id=%s", user.id)
            return None
        return await handler(event, data)


def main_keyboard() -> InlineKeyboardMarkup | None:
    """Mini App button — only once something actually serves it (Phase 7)."""
    if not config.MINI_APP_URL:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🌿 Open Dashboard",
                             web_app=WebAppInfo(url=config.MINI_APP_URL)),
    ]])
