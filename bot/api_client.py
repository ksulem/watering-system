"""Thin client for the Plant Monitor API.

The bot goes through HTTP rather than touching TimescaleDB directly. That keeps
one implementation of the watering rules — mode checks, the in-flight-command
guard, the plant-row lock — instead of a third copy that could drift from the
API's and the Consumer's.
"""
import logging
import time

import aiohttp
import jwt

from bot import config

logger = logging.getLogger("bot.api")

# Refresh a little before the API's 24h expiry so a long-lived bot never
# presents a token that expires mid-request.
TOKEN_TTL = 23 * 3600
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class ApiError(Exception):
    """Non-2xx from the API. `detail` is safe to show the user."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


_session: aiohttp.ClientSession | None = None
_token: str = ""
_token_expires_at: float = 0.0


async def init_session() -> None:
    global _session
    _session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)


async def close_session() -> None:
    global _session
    if _session is not None:
        await _session.close()
        _session = None


def _bearer() -> str:
    """Mint (and cache) a session token identical to what /auth/session issues."""
    global _token, _token_expires_at
    now = time.time()
    if _token and now < _token_expires_at:
        return _token
    _token = jwt.encode(
        {"sub": str(config.OWNER_USER_ID), "iat": int(now),
         "exp": int(now) + TOKEN_TTL},
        config.SESSION_SECRET, algorithm="HS256")
    _token_expires_at = now + TOKEN_TTL - 300
    return _token


async def _call(method: str, path: str, **kwargs):
    if _session is None:
        raise RuntimeError("api_client.init_session() has not been called")
    url = f"{config.API_BASE}{path}"
    async with _session.request(
            method, url, headers={"Authorization": f"Bearer {_bearer()}"},
            **kwargs) as resp:
        if resp.status == 204:
            return None
        try:
            body = await resp.json()
        except (aiohttp.ContentTypeError, ValueError):
            body = {"detail": (await resp.text())[:200]}
        if resp.status >= 400:
            detail = body.get("detail") if isinstance(body, dict) else str(body)
            raise ApiError(resp.status, str(detail))
        return body


async def list_plants():
    return await _call("GET", "/plants")


async def list_sensors():
    return await _call("GET", "/sensors")


async def water_plant(plant_id: int):
    # triggered_by=bot so watering_events records where the command came from —
    # the schema distinguishes auto / manual / bot.
    return await _call("POST", f"/plants/{plant_id}/water",
                       params={"triggered_by": "bot"})


async def command_status(command_id: str):
    return await _call("GET", f"/commands/{command_id}/status")


async def healthy() -> bool:
    try:
        if _session is None:
            return False
        async with _session.get(f"{config.API_BASE}/health") as resp:
            return resp.status == 200
    except aiohttp.ClientError as e:
        logger.warning("api health check failed: %s", e)
        return False
