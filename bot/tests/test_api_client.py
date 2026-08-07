"""The bot signs its own session tokens instead of calling /auth/session.

That endpoint validates Telegram initData, which only a Mini App can produce.
Both processes are server-side and share SESSION_SECRET, so the bot signs
directly — but only if what it produces is exactly what the API accepts.
"""
import time

import jwt

from api.auth import verify_session_token
from bot import api_client, config
from bot.tests.conftest import OWNER_ID


def _fresh_token() -> str:
    # _bearer caches; clear it so each test mints anew.
    api_client._token = ""
    api_client._token_expires_at = 0.0
    return api_client._bearer()


def test_bot_minted_token_is_accepted_by_the_apis_verifier():
    # The contract that makes this design work. If either side changes its
    # claims or algorithm, this fails rather than the bot silently 401ing.
    token = _fresh_token()
    assert verify_session_token(token, config.SESSION_SECRET, OWNER_ID)


def test_token_identifies_the_owner():
    claims = jwt.decode(_fresh_token(), config.SESSION_SECRET, algorithms=["HS256"])
    assert claims["sub"] == str(OWNER_ID)


def test_token_is_not_immortal():
    claims = jwt.decode(_fresh_token(), config.SESSION_SECRET, algorithms=["HS256"])
    lifetime = claims["exp"] - claims["iat"]
    # Under the API's 24h ceiling, so a cached token can't outlive what the
    # API would accept.
    assert 0 < lifetime <= 24 * 3600


def test_token_is_reused_while_valid():
    first = _fresh_token()
    assert api_client._bearer() == first


def test_token_is_reminted_once_it_nears_expiry():
    _fresh_token()
    api_client._token_expires_at = time.time() - 1
    # Different iat -> different token; proves the refresh path runs at all.
    assert api_client._bearer() is not None
    assert api_client._token_expires_at > time.time()


def test_token_signed_with_the_wrong_secret_is_rejected():
    # Sanity check that the acceptance above isn't vacuous.
    assert not verify_session_token(_fresh_token(), "a" * 64, OWNER_ID)
