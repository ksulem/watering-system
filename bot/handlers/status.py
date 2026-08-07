import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import api_client
from bot.formatting import format_status
from bot.handlers.common import main_keyboard

logger = logging.getLogger("bot.status")
router = Router()


@router.message(Command("status"))
async def status(message: Message):
    try:
        plants = await api_client.list_plants()
        sensors = await api_client.list_sensors()
    except api_client.ApiError as e:
        logger.warning("status failed: %s", e)
        await message.answer(f"⚠️ Couldn't reach the API ({e.status}). Try again shortly.")
        return
    except Exception:
        # A handler that raises leaves the user staring at silence, and
        # Telegram will redeliver the update — so answer, then re-raise into
        # the logs is not an option. Log and reply.
        logger.exception("status failed unexpectedly")
        await message.answer("⚠️ Something went wrong reading the sensors.")
        return

    await message.answer(format_status(plants, sensors), reply_markup=main_keyboard())
