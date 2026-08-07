import json

import pytest

from consumer import main
from consumer.tests.conftest import (
    DEVICE_ID,
    OTHER_DEVICE_ID,
    event_payload,
    reading_payload,
    status_payload,
)


@pytest.fixture
def spawned(monkeypatch):
    """Capture handler coroutines instead of running them as tasks."""
    captured = []

    def fake_spawn(coro):
        captured.append(coro.__qualname__)
        coro.close()   # never awaited — close it so asyncio doesn't warn
        return None

    monkeypatch.setattr(main, "_spawn", fake_spawn)
    return captured


def topic_for(suffix: str, device_id: str = DEVICE_ID) -> str:
    return f"plant-monitor/{device_id}/{suffix}"


def encode(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def dead_letter_kind(fake_db):
    fake_db.insert_dead_letter.assert_awaited_once()
    return fake_db.insert_dead_letter.await_args.args[3]


@pytest.mark.parametrize("suffix,payload,handler", [
    ("readings", reading_payload(), "process_reading"),
    ("events", event_payload(), "process"),
    ("status", status_payload(), "handle_status"),
])
async def test_valid_message_routes_to_its_handler(fake_db, spawned, suffix, payload, handler):
    await main.dispatch(topic_for(suffix), encode(payload))
    assert spawned == [handler]
    fake_db.insert_dead_letter.assert_not_awaited()


async def test_invalid_utf8_is_dead_lettered(fake_db, spawned):
    await main.dispatch(topic_for("readings"), b"\xff\xfe not utf-8")
    assert dead_letter_kind(fake_db) == "json_decode"
    assert spawned == []


async def test_malformed_json_is_dead_lettered(fake_db, spawned):
    await main.dispatch(topic_for("readings"), b'{"schema_version": 2,')
    assert dead_letter_kind(fake_db) == "json_decode"
    assert spawned == []


async def test_json_array_is_not_treated_as_a_message(fake_db, spawned):
    # json.loads succeeds but .get() would blow up on a list.
    await main.dispatch(topic_for("readings"), b"[1, 2, 3]")
    assert dead_letter_kind(fake_db) == "schema_validation"
    assert spawned == []


@pytest.mark.parametrize("version", [1, 3, None, "2"])
async def test_unsupported_schema_version_is_dead_lettered(fake_db, spawned, version):
    payload = reading_payload()
    payload["schema_version"] = version
    await main.dispatch(topic_for("readings"), encode(payload))
    assert dead_letter_kind(fake_db) == "unsupported_version"
    assert spawned == []


async def test_schema_violation_is_dead_lettered(fake_db, spawned):
    await main.dispatch(topic_for("readings"), encode(reading_payload(raw_adc=-5)))
    assert dead_letter_kind(fake_db) == "schema_validation"
    assert spawned == []


async def test_device_id_spoofing_is_dead_lettered(fake_db, spawned):
    # A device publishing a payload claiming to be another device — the broker
    # ACL already blocks this, but a compromised credential must not be able to
    # write readings against someone else's sensors.
    await main.dispatch(topic_for("readings", OTHER_DEVICE_ID),
                        encode(reading_payload(device_id=DEVICE_ID)))
    assert dead_letter_kind(fake_db) == "schema_validation"
    assert spawned == []


async def test_unknown_topic_suffix_is_ignored(fake_db, spawned):
    await main.dispatch(topic_for("commands"), encode(reading_payload()))
    assert spawned == []
    fake_db.insert_dead_letter.assert_not_awaited()


async def test_str_payload_is_accepted(fake_db, spawned):
    # aiomqtt types payload as bytes|str|int|float|None; paho gives bytes, but
    # the str branch must not crash if that ever changes.
    await main.dispatch(topic_for("readings"), json.dumps(reading_payload()))
    assert spawned == ["process_reading"]
