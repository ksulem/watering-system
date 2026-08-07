from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.handlers.common import main_keyboard

router = Router()

HELP = """🌿 <b>Plant Monitor</b>

/status — moisture and mode for every plant
/water — water a plant by hand
/help — this message

Threshold alerts arrive here automatically; you don't need to ask for them."""


@router.message(CommandStart())
async def start(message: Message):
    await message.answer(HELP, reply_markup=main_keyboard())


@router.message(Command("help"))
async def help_command(message: Message):
    await message.answer(HELP, reply_markup=main_keyboard())
