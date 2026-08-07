import time

import jwt
import pytest

from api.auth import (
    SESSION_TTL_SECONDS,
    create_session_token,
    verify_session_token,
    verify_telegram_init_data,
)
from api.tests.conftest import OWNER_ID, STRANGER_ID, TEST_BOT_TOKEN, TEST_SECRET, make_init_data

# --- initData ----------------------------------------------------------------

def test_genuine_init_data_is_accepted():
    assert verify_telegram_init_data(make_init_data(), TEST_BOT_TOKEN, OWNER_ID)


def test_signature_from_a_different_bot_is_rejected():
    forged = make_init_data(bot_token="999:SOMEONE-ELSES-BOT")
    assert not verify_telegram_init_data(forged, TEST_BOT_TOKEN, OWNER_ID)


def test_fields_edited_after_signing_are_rejected():
    # Swapping the user payload for another id without being able to re-sign is
    # the whole attack this HMAC exists to stop.
    assert not verify_telegram_init_data(make_init_data(tamper=True),
                                         TEST_BOT_TOKEN, OWNER_ID)


def test_missing_hash_is_rejected():
    assert not verify_telegram_init_data("auth_date=1&user=%7B%7D",
                                         TEST_BOT_TOKEN, OWNER_ID)


def test_stale_init_data_is_rejected():
    # Captured initData must not stay usable: 5 minutes is the whole window.
    stale = make_init_data(auth_date=int(time.time()) - 301)
    assert not verify_telegram_init_data(stale, TEST_BOT_TOKEN, OWNER_ID)


def test_init_data_just_inside_the_window_is_accepted():
    fresh = make_init_data(auth_date=int(time.time()) - 60)
    assert verify_telegram_init_data(fresh, TEST_BOT_TOKEN, OWNER_ID)


def test_another_telegram_user_is_rejected():
    # Correctly signed by the real bot, but not the owner — this is a
    # single-user system.
    other = make_init_data(user_id=STRANGER_ID)
    assert not verify_telegram_init_data(other, TEST_BOT_TOKEN, OWNER_ID)
    # Sanity check that the payload itself is well-formed, so the rejection
    # above is really about the user id and not a broken fixture.
    assert verify_telegram_init_data(other, TEST_BOT_TOKEN, STRANGER_ID)


def test_non_numeric_auth_date_is_rejected():
    bad = make_init_data(auth_date=0, extra=None).replace("auth_date=0", "auth_date=soon")
    assert not verify_telegram_init_data(bad, TEST_BOT_TOKEN, OWNER_ID)


def test_unparseable_user_field_is_rejected():
    assert not verify_telegram_init_data(
        make_init_data(extra={"user": "not-json"}), TEST_BOT_TOKEN, OWNER_ID)


def test_extra_fields_are_covered_by_the_signature():
    # Telegram adds fields over time (chat_instance, signature, ...). They must
    # take part in the check string, or the HMAC stops matching real payloads.
    signed = make_init_data(extra={"chat_instance": "-42", "signature": "abc"})
    assert verify_telegram_init_data(signed, TEST_BOT_TOKEN, OWNER_ID)


# --- Session tokens ----------------------------------------------------------

def test_session_token_round_trip():
    token = create_session_token(OWNER_ID, TEST_SECRET)
    assert verify_session_token(token, TEST_SECRET, OWNER_ID)


def test_token_signed_with_another_secret_is_rejected():
    token = create_session_token(OWNER_ID, "a" * 64)
    assert not verify_session_token(token, TEST_SECRET, OWNER_ID)


def test_token_for_another_user_is_rejected():
    token = create_session_token(STRANGER_ID, TEST_SECRET)
    assert not verify_session_token(token, TEST_SECRET, OWNER_ID)


def test_expired_token_is_rejected():
    now = int(time.time())
    token = jwt.encode({"sub": str(OWNER_ID), "iat": now - 100_000,
                        "exp": now - 10}, TEST_SECRET, algorithm="HS256")
    assert not verify_session_token(token, TEST_SECRET, OWNER_ID)


def test_alg_none_token_is_rejected():
    # Pinning algorithms=["HS256"] is what stops an attacker handing us an
    # unsigned token that decodes cleanly.
    token = jwt.encode({"sub": str(OWNER_ID), "exp": int(time.time()) + 600},
                       key="", algorithm="none")
    assert not verify_session_token(token, TEST_SECRET, OWNER_ID)


@pytest.mark.parametrize("garbage", ["", "not.a.token", "a.b.c"])
def test_malformed_tokens_are_rejected(garbage):
    assert not verify_session_token(garbage, TEST_SECRET, OWNER_ID)


def test_token_lifetime_is_24h():
    token = create_session_token(OWNER_ID, TEST_SECRET)
    claims = jwt.decode(token, TEST_SECRET, algorithms=["HS256"])
    assert claims["exp"] - claims["iat"] == SESSION_TTL_SECONDS == 86400
