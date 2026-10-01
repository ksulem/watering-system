import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import watering_controller as wc
from relay import DE_ENERGISED, ENERGISED, RELAY_PINS

from consumer.messages import WateringEventMessage

WIRED_CHANNEL = 1          # IN1 / GPIO5 — Кофейное дерево / Coffee tree
WIRED_PIN = RELAY_PINS[WIRED_CHANNEL]
UNWIRED_CHANNEL = 4        # in the schema's 1-8 range, but nothing attached


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("""
        CREATE TABLE executed_commands (
            command_id TEXT PRIMARY KEY, event TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))
    """)
    yield c
    c.close()


@pytest.fixture
def client():
    c = MagicMock()
    c.publish = AsyncMock()
    return c


@pytest.fixture(autouse=True)
def gpio():
    """Fresh GPIO mock per test, and a real semaphore bound to this test's loop."""
    wc.GPIO.reset_mock()
    wc.active_relays.clear()
    wc.pump_slots = asyncio.Semaphore(1)
    return wc.GPIO


def command(**overrides) -> dict:
    base = {
        "schema_version": 2,
        "command_id": str(uuid4()),
        "command": "water_now",
        "device_id": wc.DEVICE_ID,
        "plant_id": 1,
        "relay_channel": WIRED_CHANNEL,
        "duration_sec": 2,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    return {**base, **overrides}


def published_events(client) -> list[dict]:
    return [json.loads(call.args[1]) for call in client.publish.call_args_list
            if call.args[0] == wc.EVENTS_TOPIC]


# --- parse_command -----------------------------------------------------------

def test_parse_command_accepts_the_real_api_payload():
    parsed = wc.parse_command(json.dumps(command()))
    assert parsed is not None
    assert parsed["relay_channel"] == WIRED_CHANNEL


@pytest.mark.parametrize("payload", [
    b"not json",
    b'"a bare string"',
    b'{"command_id": "not-a-uuid", "plant_id": 1, "relay_channel": 1, '
    b'"duration_sec": 5, "expires_at": "2030-01-01T00:00:00+00:00"}',
])
def test_parse_command_drops_unusable_payloads(payload):
    assert wc.parse_command(payload) is None


def test_parse_command_drops_missing_fields():
    incomplete = command()
    del incomplete["relay_channel"]
    assert wc.parse_command(json.dumps(incomplete)) is None


def test_parse_command_drops_channel_outside_the_schema_range():
    # An event echoing channel 99 would fail the Consumer's ge=1/le=8 validation
    # and be dead-lettered, losing the only trace that a bad command arrived.
    assert wc.parse_command(json.dumps(command(relay_channel=99))) is None


# --- event contract ----------------------------------------------------------

def test_every_event_validates_against_the_consumer_model():
    # The Pi and the Consumer are separate deployables with no shared schema
    # module. If this drifts, events are dead-lettered and commands hang until
    # stale-expiry — with no error anywhere near the Pi.
    for event in ("watering_started", "watering_completed", "watering_failed",
                  "command_expired"):
        payload = wc.build_event(event, command(), error="x", duration_sec=5)
        WateringEventMessage.model_validate(payload)


# --- handle_command ----------------------------------------------------------

async def test_happy_path_energises_then_de_energises(conn, client, gpio, monkeypatch):
    monkeypatch.setattr(wc.asyncio, "sleep", AsyncMock())
    await wc.handle_command(client, conn, command())

    levels = [c.args for c in gpio.output.call_args_list if c.args[0] == WIRED_PIN]
    assert levels[0] == (WIRED_PIN, ENERGISED)
    assert levels[-1] == (WIRED_PIN, DE_ENERGISED)

    assert [e["event"] for e in published_events(client)] == [
        "watering_started", "watering_completed"]


async def test_unwired_channel_fails_without_touching_gpio(conn, client, gpio):
    await wc.handle_command(client, conn, command(relay_channel=UNWIRED_CHANNEL))

    events = published_events(client)
    assert [e["event"] for e in events] == ["watering_failed"]
    assert events[0]["error"] == "invalid_relay_channel"
    assert gpio.output.call_args_list == []


async def test_expired_command_never_fires(conn, client, gpio):
    stale = command(expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    await wc.handle_command(client, conn, stale)

    assert [e["event"] for e in published_events(client)] == ["command_expired"]
    assert gpio.output.call_args_list == []


async def test_unparseable_expiry_is_treated_as_expired(conn, client, gpio):
    await wc.handle_command(client, conn, command(expires_at="whenever"))

    assert [e["event"] for e in published_events(client)] == ["command_expired"]
    assert gpio.output.call_args_list == []


async def test_duration_is_capped(conn, client, monkeypatch):
    slept = []
    monkeypatch.setattr(wc.asyncio, "sleep", AsyncMock(side_effect=slept.append))
    monkeypatch.setattr(wc, "MAX_WATERING_DURATION_SEC", 30)

    await wc.handle_command(client, conn, command(duration_sec=9999))

    assert slept == [30]
    assert published_events(client)[-1]["duration_sec"] == 30


async def test_redelivery_replays_the_stored_outcome(conn, client, monkeypatch):
    monkeypatch.setattr(wc.asyncio, "sleep", AsyncMock())
    cmd = command()
    await wc.handle_command(client, conn, cmd)
    first = published_events(client)
    client.publish.reset_mock()

    await wc.handle_command(client, conn, cmd)

    replay = published_events(client)
    assert len(replay) == 1
    assert replay[0]["event"] == "watering_completed"
    # Same outcome, new event_id — the Consumer dedups on event_id, so reusing
    # it would make the replay invisible and leave the command stuck.
    assert replay[0]["event_id"] != first[-1]["event_id"]
    WateringEventMessage.model_validate(replay[0])


async def test_redelivery_does_not_run_the_pump_again(conn, client, gpio, monkeypatch):
    monkeypatch.setattr(wc.asyncio, "sleep", AsyncMock())
    cmd = command()
    await wc.handle_command(client, conn, cmd)
    gpio.reset_mock()

    await wc.handle_command(client, conn, cmd)

    assert gpio.output.call_args_list == []


async def test_busy_relay_is_rejected_not_queued(conn, client, gpio):
    wc.active_relays.add(WIRED_CHANNEL)
    await wc.handle_command(client, conn, command())

    events = published_events(client)
    assert events[0]["error"] == "relay_busy"
    assert gpio.output.call_args_list == []
    # The rejection must not clear the in-flight command's claim on the relay.
    assert WIRED_CHANNEL in wc.active_relays


async def test_relay_is_de_energised_when_execution_raises(conn, client, gpio, monkeypatch):
    monkeypatch.setattr(wc, "execute_relay", AsyncMock(side_effect=RuntimeError("boom")))
    await wc.handle_command(client, conn, command())

    assert (WIRED_PIN, DE_ENERGISED) in [c.args for c in gpio.output.call_args_list]
    assert published_events(client)[-1]["error"] == "relay_error"
    assert WIRED_CHANNEL not in wc.active_relays


async def test_timeout_is_reported_and_channel_released(conn, client, monkeypatch):
    monkeypatch.setattr(wc, "execute_relay",
                        AsyncMock(side_effect=asyncio.TimeoutError))
    await wc.handle_command(client, conn, command())

    assert published_events(client)[-1]["error"] == "relay_timeout"
    assert WIRED_CHANNEL not in wc.active_relays


async def test_outcome_is_persisted_even_when_the_broker_is_gone(conn, client, monkeypatch):
    # If the broker drops mid-command, the outcome must still be recorded: it is
    # what answers the redelivery that arrives after reconnect. A dead broker
    # must not take down the handler either — the session loop owns reconnects.
    monkeypatch.setattr(wc.asyncio, "sleep", AsyncMock())
    client.publish = AsyncMock(side_effect=wc.MqttError("broker gone"))
    cmd = command()

    await wc.handle_command(client, conn, cmd)

    stored = wc.recall(conn, cmd["command_id"])
    assert stored is not None
    assert stored["event"] == "watering_failed"
    assert WIRED_CHANNEL not in wc.active_relays


async def test_a_failed_publish_cannot_overwrite_a_real_completion(conn, client, monkeypatch):
    # The pump ran and completed. Publishing that outcome then fails, so the
    # handler publishes watering_failed. The stored outcome must stay
    # 'watering_completed' — otherwise the redelivery reports a failure for a
    # watering that genuinely happened, and the backend may water again.
    monkeypatch.setattr(wc.asyncio, "sleep", AsyncMock())
    cmd = command()

    async def publish(topic, payload, **kw):
        if json.loads(payload).get("event") == "watering_completed":
            raise wc.MqttError("broker gone")

    client.publish = AsyncMock(side_effect=publish)
    await wc.handle_command(client, conn, cmd)

    assert wc.recall(conn, cmd["command_id"])["event"] == "watering_completed"


# --- tank lockout ------------------------------------------------------------

async def test_tank_check_is_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(wc, "TANK_MIN_PCT", 0.0)
    assert await wc.tank_ok() == (True, None)


async def test_tank_fails_closed_when_unreadable(monkeypatch):
    # No tank sensor is wired, so TANK_MIN_PCT defaults to 0. If someone enables
    # the lockout without wiring the channel, refusing is the safe answer — a
    # pump run dry destroys itself.
    monkeypatch.setattr(wc, "TANK_MIN_PCT", 5.0)
    monkeypatch.setattr(wc, "TANK_ADC_CHANNEL", None)
    assert await wc.tank_ok() == (False, "tank_unreadable")


# --- startup -----------------------------------------------------------------

def test_init_gpio_sets_every_pin_de_energised(gpio):
    wc.init_gpio()

    configured = {c.args[0]: c.kwargs.get("initial") for c in gpio.setup.call_args_list}
    assert configured == {pin: DE_ENERGISED for pin in RELAY_PINS.values()}


def test_de_energised_is_high_on_this_active_low_board():
    # The single fact the whole file depends on. If the board is ever swapped,
    # this test is the one that should fail first and loudest.
    assert (ENERGISED, DE_ENERGISED) == (0, 1)
