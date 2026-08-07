import logging
import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import api_client
from bot.formatting import format_water_targets, plant_title

logger = logging.getLogger("bot.water")
router = Router()

# /water_3 — the per-plant shortcut offered by the /water menu.
WATER_SHORTCUT = re.compile(r"^/water_(\d+)$")

# Maps the API's refusals onto something a human can act on. The API is the
# single authority on whether a watering may happen; the bot only translates.
REFUSALS = {
    404: "🔧 That plant has no relay assigned, so it can't be watered yet.",
    400: "🔄 That plant is in AUTO mode. Switch it to MANUAL first — a manual "
         "command would otherwise race the automatic threshold logic.",
    409: "⏳ A watering command for that plant is already in flight. Wait for it "
         "to finish.",
}


async def _water(message: Message, plant_id: int) -> None:
    try:
        result = await api_client.water_plant(plant_id)
    except api_client.ApiError as e:
        await message.answer(REFUSALS.get(e.status, f"⚠️ Couldn't water that ({e.status}): {e.detail}"))
        return
    except Exception:
        logger.exception("water failed unexpectedly")
        await message.answer("⚠️ Something went wrong queuing that watering.")
        return

    # Queued, not done. The Pi decides whether the relay actually fires, and it
    # reports back through MQTT — the Consumer will push the outcome here.
    await message.answer(
        f"💦 Watering queued.\n\nCommand <code>{result['command_id']}</code> is "
        f"{result['status']}. You'll get a message when it completes.")


@router.message(Command("water"))
async def water_menu(message: Message):
    """/water with no argument lists what can be watered; /water 3 acts."""
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].isdigit():
        await _water(message, int(parts[1]))
        return

    try:
        plants = await api_client.list_plants()
    except api_client.ApiError as e:
        await message.answer(f"⚠️ Couldn't reach the API ({e.status}).")
        return
    await message.answer(format_water_targets(plants))


@router.message(F.text.regexp(WATER_SHORTCUT))
async def water_shortcut(message: Message):
    match = WATER_SHORTCUT.match(message.text or "")
    if match:
        await _water(message, int(match.group(1)))


def describe_target(plant: dict) -> str:
    return plant_title(plant)
