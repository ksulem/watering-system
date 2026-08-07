"""Telegram WebApp initData validation and session tokens.

initData is verified ONCE, at POST /auth/session, and exchanged for a signed
24-hour bearer token. Validating initData per-request would 401 any dashboard
left open longer than five minutes: Telegram regenerates it only when the Mini
App is (re)opened, not while it stays open.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

import jwt

# Short enough to make a captured initData string useless to an attacker who
# grabs it out of a log or a screenshot.
MAX_AUTH_AGE_SECONDS = 300
SESSION_TTL_SECONDS = 24 * 3600


def verify_telegram_init_data(init_data: str, bot_token: str, allowed_user_id: int) -> bool:
    """Validate Telegram's HMAC over initData, its freshness, and the user id."""
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return False

    # Per Telegram's spec the check string covers every received field except
    # `hash`, sorted by key. `signature` (used for the separate Ed25519
    # third-party validation flow) stays in — removing it breaks the HMAC.
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    # compare_digest, not ==, so a wrong hash can't be recovered byte-by-byte
    # from response timing.
    if not hmac.compare_digest(expected_hash, received_hash):
        return False

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        return False
    if time.time() - auth_date > MAX_AUTH_AGE_SECONDS:
        return False

    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        return False
    if not isinstance(user, dict):
        return False

    return user.get("id") == allowed_user_id


def create_session_token(user_id: int, secret: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + SESSION_TTL_SECONDS},
        secret, algorithm="HS256")


def verify_session_token(token: str, secret: str, allowed_user_id: int) -> bool:
    try:
        # algorithms is pinned: without it PyJWT would accept whatever the
        # token's own header claims, which is the classic alg-confusion bug.
        # decode() enforces exp itself.
        claims = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return False
    return claims.get("sub") == str(allowed_user_id)
