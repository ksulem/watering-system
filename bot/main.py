"""Telegram bot (aiogram 3) behind an Nginx-terminated webhook.

Webhook rather than long polling because Nginx and a real certificate already
exist, and Telegram pushes updates the moment they happen instead of the bot
holding an open connection.

Push notifications for alerts and watering outcomes are NOT sent from here —
the Consumer posts them to Telegram directly when it processes the underlying
event, so an alert doesn't depend on this process being up.
"""
import asyncio
import contextlib
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from bot import api_client, config
from bot.handlers import start, status, water
from bot.handlers.common import OwnerOnlyMiddleware

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("bot")


async def health(request: web.Request) -> web.Response:
    """Liveness, not readiness.

    Reports 200 whenever the process is up, and includes API reachability as
    information only. Failing this when the API is down would make Docker
    restart the bot repeatedly for someone else's outage, which fixes nothing.
    """
    return web.json_response({"status": "ok", "api": await api_client.healthy()})


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # Registered on the dispatcher, not per-router, so a handler added later
    # cannot forget the owner check.
    dp.message.middleware(OwnerOnlyMiddleware())
    dp.include_router(start.router)
    dp.include_router(status.router)
    dp.include_router(water.router)
    return dp


async def main() -> None:
    if not config.webhook_configured():
        raise SystemExit(
            "WEBHOOK_URL and WEBHOOK_SECRET must both be set. Without the "
            "secret, anyone who guesses the webhook path could post forged "
            "updates.")

    bot = Bot(token=config.BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    await api_client.init_session()

    app = web.Application()
    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dp, bot=bot,
        # aiogram compares this against X-Telegram-Bot-Api-Secret-Token and
        # returns 401 on a mismatch, so forged posts never reach a handler.
        secret_token=config.WEBHOOK_SECRET,
    ).register(app, path=config.WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    logger.info("listening on :%d%s", config.PORT, config.WEBHOOK_PATH)

    await bot.set_webhook(
        config.WEBHOOK_URL,
        secret_token=config.WEBHOOK_SECRET,
        # Deliberately false: updates sent while the bot didn't exist are still
        # queued at Telegram for 24h, and answering them is friendlier than
        # silently discarding what the owner already asked for.
        drop_pending_updates=False,
        allowed_updates=dp.resolve_used_update_types(),
    )
    info = await bot.get_webhook_info()
    logger.info("webhook registered at %s (pending=%d)", info.url, info.pending_update_count)

    try:
        await asyncio.Event().wait()   # run until cancelled
    finally:
        await api_client.close_session()
        await runner.cleanup()
        await bot.session.close()
        logger.info("bot stopped")


if __name__ == "__main__":
    # SystemExit deliberately NOT suppressed: main() raises it with a message
    # when the webhook config is missing, and swallowing it would exit 0 with
    # the reason lost — a misconfigured bot would look like a clean shutdown.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
