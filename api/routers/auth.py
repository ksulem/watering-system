"""The only endpoint that accepts raw Telegram initData."""
import logging

from fastapi import APIRouter, Header, HTTPException, status

from api import config
from api.auth import SESSION_TTL_SECONDS, create_session_token, verify_telegram_init_data
from api.schemas import SessionOut

logger = logging.getLogger("api.auth")

router = APIRouter(tags=["auth"])


@router.post("/auth/session", response_model=SessionOut)
async def create_session(
    x_telegram_init_data: str | None = Header(default=None),
):
    if not config.auth_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Telegram auth is not configured: set BOT_TOKEN, OWNER_USER_ID "
            "and SESSION_SECRET.",
        )
    if not x_telegram_init_data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Missing X-Telegram-Init-Data header.")

    if not verify_telegram_init_data(x_telegram_init_data, config.BOT_TOKEN,
                                     config.OWNER_USER_ID):
        # One message for every failure mode — bad HMAC, stale auth_date, wrong
        # user. Telling a caller which one it was just helps them iterate.
        logger.warning("rejected initData exchange")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "initData failed validation.")

    token = create_session_token(config.OWNER_USER_ID, config.SESSION_SECRET)
    return SessionOut(token=token, expires_in=SESSION_TTL_SECONDS)
