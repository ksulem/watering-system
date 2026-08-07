"""Telegram push notifications.

One shared aiohttp session for the process lifetime. No retry: Telegram 429s
compound under retry, and an alert that's already late is worth less than a
stable Consumer. Every failure is logged so a dropped alert stays recoverable
from `docker compose logs`.
"""
import logging
import os

import aiohttp

logger = logging.getLogger("consumer.notify")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "")

_session: aiohttp.ClientSession | None = None


def enabled() -> bool:
    return bool(BOT_TOKEN and OWNER_CHAT_ID)


async def init_session() -> None:
    global _session
    if not enabled():
        # The bot arrives in Phase 6. Until then alerts still land in the
        # `alerts` table and the metrics counter — only the push is skipped.
        logger.info("BOT_TOKEN/OWNER_CHAT_ID unset — Telegram notifications disabled")
        return
    _session = aiohttp.ClientSession()


async def close_session() -> None:
    global _session
    if _session is not None:
        await _session.close()
        _session = None


async def notify_telegram(text: str) -> None:
    if _session is None:
        logger.info("notification (telegram disabled): %s", text)
        return
    try:
        resp = await _session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": OWNER_CHAT_ID, "text": text},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if resp.status >= 400:
            body = await resp.text()
            logger.warning("telegram send failed status=%s body=%s text=%r",
                           resp.status, body[:200], text)
    except (TimeoutError, aiohttp.ClientError) as e:
        logger.warning("telegram send error: %s text=%r", e, text)
