"""The bot's only access control.

@WateringKLBot is publicly addressable — anyone can find it and message it.
Without this gate a stranger could read the owner's sensor history and trigger
watering.
"""
from types import SimpleNamespace

from bot.handlers.common import OwnerOnlyMiddleware, main_keyboard
from bot.tests.conftest import OWNER_ID, STRANGER_ID


async def _run(user_id: int | None):
    """Push an update through the middleware; report whether it reached a handler."""
    reached = []

    async def handler(event, data):
        reached.append(True)
        return "handled"

    data = {}
    if user_id is not None:
        data["event_from_user"] = SimpleNamespace(id=user_id)
    result = await OwnerOnlyMiddleware()(handler, SimpleNamespace(), data)
    return result, bool(reached)


async def test_owner_reaches_the_handler():
    result, reached = await _run(OWNER_ID)
    assert reached and result == "handled"


async def test_stranger_is_dropped():
    result, reached = await _run(STRANGER_ID)
    assert not reached
    # Returning None rather than answering: a silent drop tells a prober nothing
    # about whether the bot is live or who owns it.
    assert result is None


async def test_update_without_a_user_is_dropped():
    # Channel posts and some service updates carry no from_user.
    result, reached = await _run(None)
    assert not reached and result is None


def test_dashboard_button_hidden_until_the_mini_app_exists(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "MINI_APP_URL", "")
    assert main_keyboard() is None


def test_dashboard_button_appears_once_configured(monkeypatch):
    from bot import config
    monkeypatch.setattr(config, "MINI_APP_URL", "https://wateringkl.org/app")
    kb = main_keyboard()
    assert kb is not None
    assert kb.inline_keyboard[0][0].web_app.url == "https://wateringkl.org/app"
