"""Bot configuration.

The bot deliberately holds no database credentials. Everything it needs comes
from the API over the Docker network, so watering rules, mode checks and the
command lifecycle have exactly one implementation.
"""
import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])

# Shared with the API. The bot mints its own session tokens with this rather
# than going through /auth/session — that endpoint validates Telegram initData,
# which only a Mini App can produce. Both processes are server-side and equally
# trusted, so signing directly is the honest way to say "the owner asked".
SESSION_SECRET = os.environ["SESSION_SECRET"]

API_BASE = os.environ.get("API_BASE", "http://api:8000")

# Public HTTPS URL Telegram posts updates to.
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

# Telegram echoes this back in X-Telegram-Bot-Api-Secret-Token on every update.
# Without it, anyone who guesses the webhook path (and /webhook on a known
# domain is very guessable) could POST forged updates straight into the
# dispatcher and drive the bot as if they were Telegram.
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

WEBHOOK_PATH = "/webhook"
PORT = int(os.environ.get("PORT", "8001"))

# Only set once the Mini App is actually served (Phase 7). While empty, the
# dashboard button is omitted rather than pointing at a 404.
MINI_APP_URL = os.environ.get("MINI_APP_URL", "")


def webhook_configured() -> bool:
    return bool(WEBHOOK_URL and WEBHOOK_SECRET)
