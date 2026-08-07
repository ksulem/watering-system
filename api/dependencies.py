"""Shared FastAPI dependencies."""
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api import config
from api.auth import verify_session_token

# auto_error=False so a missing header produces our own 401 with a useful
# message rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)


async def require_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> int:
    """Authorize the request and return the owner's Telegram user id.

    Fails closed when auth isn't configured: with no SESSION_SECRET there is no
    key to verify signatures against, and with no OWNER_USER_ID there is nobody
    to authorize. Returning 503 rather than 401 says "this server can't answer
    that yet", which is the honest distinction while the bot doesn't exist.
    """
    if not config.sessions_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Auth is not configured: set SESSION_SECRET and OWNER_USER_ID.",
        )
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not verify_session_token(credentials.credentials, config.SESSION_SECRET,
                                config.OWNER_USER_ID):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return config.OWNER_USER_ID
