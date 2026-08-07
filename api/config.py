"""Runtime configuration, read once at import.

Telegram credentials are optional so the API can be deployed before the bot
exists (Phase 6). When they're absent the auth endpoints fail closed with 503
rather than silently letting anyone in — see `auth.py`.
"""
import os

DATABASE_URL = os.environ["DATABASE_URL"]

# Telegram bot token, used only as the HMAC key for initData validation.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# Numeric Telegram user id of the single owner. This system has exactly one
# user; there is no user table and no registration flow.
_owner = os.environ.get("OWNER_USER_ID", "").strip()
OWNER_USER_ID: int | None = int(_owner) if _owner.isdigit() else None

# Signing key for session tokens. Deliberately separate from BOT_TOKEN so that
# rotating the bot (or leaking it to BotFather logs) doesn't hand out sessions.
# Generate with: openssl rand -hex 32
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")

# Comma-separated allowed origins. The plan assumed a DOMAIN env var, but no
# domain exists yet — the broker still uses an IP-SAN certificate. Empty means
# no cross-origin request is allowed, which is correct until the Mini App is
# actually served from somewhere.
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]

# Caps on history queries. sensor_readings grows at ~8600 rows/day; an unbounded
# range query would happily try to serialize a year of it into one JSON response.
MAX_HISTORY_ROWS = 5000
DEFAULT_HISTORY_ROWS = 500


def auth_configured() -> bool:
    return bool(BOT_TOKEN and OWNER_USER_ID is not None and SESSION_SECRET)


def sessions_configured() -> bool:
    """Session validation needs the signing key and the owner id, but not the
    bot token — the bot token is only used to mint a session in the first place."""
    return bool(SESSION_SECRET and OWNER_USER_ID is not None)
