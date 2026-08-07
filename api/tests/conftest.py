import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock

import pytest

# api.config reads DATABASE_URL at import time.
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

TEST_BOT_TOKEN = "123456:TEST-BOT-TOKEN"
TEST_SECRET = "0" * 64
OWNER_ID = 424242
STRANGER_ID = 999999


@pytest.fixture
def configured_auth(monkeypatch):
    """Populate the Telegram credentials the deployed API doesn't have yet."""
    from api import config
    monkeypatch.setattr(config, "BOT_TOKEN", TEST_BOT_TOKEN)
    monkeypatch.setattr(config, "OWNER_USER_ID", OWNER_ID)
    monkeypatch.setattr(config, "SESSION_SECRET", TEST_SECRET)
    return config


@pytest.fixture
def unconfigured_auth(monkeypatch):
    """The state the API is actually deployed in right now."""
    from api import config
    monkeypatch.setattr(config, "BOT_TOKEN", "")
    monkeypatch.setattr(config, "OWNER_USER_ID", None)
    monkeypatch.setattr(config, "SESSION_SECRET", "")
    return config


@pytest.fixture
def fake_db(monkeypatch):
    """Replace every api.db coroutine with an AsyncMock."""
    from api import db

    for name in (
        "list_plants", "get_plant", "create_plant", "update_plant", "plant_exists",
        "list_sensor_types", "latest_readings", "sensor_exists",
        "reading_history", "reading_aggregated",
        "get_watering_config", "put_watering_config", "patch_watering_config",
        "has_active_watering_command", "try_create_pending_watering_command",
        "get_command", "get_alert_configs", "upsert_alert_config",
        "get_calibration", "put_calibration", "list_alerts", "ack_alert",
        "healthy", "init_pool", "close_pool",
    ):
        monkeypatch.setattr(db, name, AsyncMock())

    db.healthy.return_value = True
    db.plant_exists.return_value = True
    db.sensor_exists.return_value = True
    db.try_create_pending_watering_command.return_value = True
    return db


@pytest.fixture
def client(fake_db, configured_auth):
    """TestClient with a mocked DB and working auth.

    Not used as a context manager on purpose: that would run the app's lifespan,
    which opens a real asyncpg pool.
    """
    from fastapi.testclient import TestClient

    from api.main import app
    return TestClient(app)


@pytest.fixture
def token(configured_auth):
    from api.auth import create_session_token
    return create_session_token(OWNER_ID, TEST_SECRET)


@pytest.fixture
def authed(client, token):
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def make_init_data(bot_token: str = TEST_BOT_TOKEN, user_id: int = OWNER_ID,
                   auth_date: int | None = None, extra: dict | None = None,
                   tamper: bool = False) -> str:
    """Build a correctly-signed initData string, the way Telegram would."""
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAF_test",
        "user": json.dumps({"id": user_id, "first_name": "Test"},
                           separators=(",", ":")),
    }
    if extra:
        fields.update(extra)

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()

    if tamper:
        # Change a field after signing — the classic forgery attempt.
        fields["user"] = json.dumps({"id": STRANGER_ID, "first_name": "Mallory"},
                                    separators=(",", ":"))

    from urllib.parse import urlencode
    return urlencode({**fields, "hash": digest})
