import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

# asyncpg and aiohttp are stubbed in the repo-root conftest.py — see the note
# there for why.

# main.py reads these at import time via os.environ in session_main(); only
# DATABASE_URL/MQTT_* are touched at call time, but keep the environment
# realistic so an accidental import-time read doesn't KeyError.
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")
os.environ.setdefault("MQTT_HOST", "test-broker")
os.environ.setdefault("MQTT_CONSUMER_USERNAME", "backend_consumer")
os.environ.setdefault("MQTT_CONSUMER_PASSWORD", "test-pass")

DEVICE_ID = "260922ba-03f3-4c20-a7e4-1d7dbf8d9f42"
OTHER_DEVICE_ID = "11111111-2222-3333-4444-555555555555"


class FakeConn:
    """Stand-in for the connection yielded by db.transaction()."""


@pytest.fixture
def fake_db(monkeypatch):
    """Replace every consumer.db coroutine with an AsyncMock.

    Returns the patched module so tests can set return values and assert calls.
    Sensible defaults: nothing is a duplicate, nothing already exists.
    """
    from consumer import db

    for name in (
        "get_sensor_by_device_address", "get_sensor_calibration", "insert_reading",
        "mark_event_processed", "get_alert_configs", "recent_alert_exists",
        "create_alert", "get_watering_config", "has_active_watering_command",
        "watered_within", "try_create_pending_watering_command",
        "update_watering_event", "count_pending_outbox", "update_device_status",
        "insert_dead_letter", "execute", "healthy",
    ):
        monkeypatch.setattr(db, name, AsyncMock())

    db.mark_event_processed.return_value = True
    db.update_watering_event.return_value = True
    db.get_alert_configs.return_value = []
    db.recent_alert_exists.return_value = False
    db.has_active_watering_command.return_value = False
    db.watered_within.return_value = False
    db.try_create_pending_watering_command.return_value = True
    db.update_device_status.return_value = True

    @asynccontextmanager
    async def fake_transaction():
        yield FakeConn()

    monkeypatch.setattr(db, "transaction", fake_transaction)
    return db


@pytest.fixture
def sent(monkeypatch):
    """Collect Telegram notifications instead of sending them."""
    messages = []

    async def fake_notify(text: str) -> None:
        messages.append(text)

    from consumer import alerts, event_handler
    monkeypatch.setattr(alerts, "notify_telegram", fake_notify)
    monkeypatch.setattr(event_handler, "notify_telegram", fake_notify)
    return messages


def reading_payload(**overrides) -> dict:
    payload = {
        "schema_version": 2,
        "reading_id": "aaaaaaaa-0000-4000-8000-000000000001",
        "device_id": DEVICE_ID,
        "address": "adc:0",
        "value": None,
        "raw_adc": 612,
        "sensor_type": "moisture",
        "recorded_at": "2026-08-07T10:23:00Z",
    }
    payload.update(overrides)
    return payload


def event_payload(**overrides) -> dict:
    payload = {
        "schema_version": 2,
        "event_id": "bbbbbbbb-0000-4000-8000-000000000001",
        "event": "watering_completed",
        "command_id": "cccccccc-0000-4000-8000-000000000001",
        "device_id": DEVICE_ID,
        "plant_id": 2,
        "relay_channel": 2,
        "duration_sec": 10,
        "triggered_by": "auto",
        "recorded_at": "2026-08-07T10:55:00Z",
    }
    payload.update(overrides)
    return payload


def status_payload(**overrides) -> dict:
    payload = {
        "schema_version": 2,
        "device_id": DEVICE_ID,
        "status": "online",
        "recorded_at": "2026-08-07T09:00:00Z",
    }
    payload.update(overrides)
    return payload


def sensor_row(sensor_id: int = 1, plant_id: int | None = 1, label: str = "Монстера moisture"):
    """asyncpg Records behave like read-only mappings; a dict is close enough."""
    return {"id": sensor_id, "plant_id": plant_id, "label": label}
