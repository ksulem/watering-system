"""process_reading must never turn a disconnected sensor into a 100% reading."""
from unittest.mock import AsyncMock

import pytest

from consumer import main, sensor_health
from consumer.messages import ReadingMessage
from consumer.tests.conftest import reading_payload, sensor_row


@pytest.fixture(autouse=True)
def clean_state():
    sensor_health._consecutive.clear()
    sensor_health._faulty.clear()
    yield
    sensor_health._consecutive.clear()
    sensor_health._faulty.clear()


@pytest.fixture
def downstream(monkeypatch):
    thresholds, auto_water = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, "check_thresholds", thresholds)
    monkeypatch.setattr(main, "maybe_auto_water", auto_water)
    return thresholds, auto_water


def msg(**overrides) -> ReadingMessage:
    return ReadingMessage.model_validate(reading_payload(**overrides))


async def test_disconnected_sensor_is_not_stored_as_full_moisture(fake_db, downstream, sent):
    # The exact bug: raw 0 calibrates to 100%, so the plant reads as watered,
    # no low-moisture alert fires, and auto-water never runs.
    fake_db.get_sensor_by_device_address.return_value = sensor_row()
    fake_db.get_sensor_calibration.return_value = None

    await main.process_reading(msg(raw_adc=0))

    fake_db.insert_reading.assert_not_awaited()
    fake_db.mark_event_processed.assert_not_awaited()
    assert fake_db.insert_dead_letter.await_args.args[3] == "implausible_reading"


async def test_the_raw_value_is_preserved_for_diagnosis(fake_db, downstream, sent):
    fake_db.get_sensor_by_device_address.return_value = sensor_row()

    await main.process_reading(msg(raw_adc=3))

    payload = fake_db.insert_dead_letter.await_args.args[1]
    assert '"raw_adc":3' in payload.replace(" ", "")


async def test_implausible_readings_skip_alerts_and_auto_water(fake_db, downstream, sent):
    fake_db.get_sensor_by_device_address.return_value = sensor_row()
    thresholds, auto_water = downstream

    await main.process_reading(msg(raw_adc=0))

    thresholds.assert_not_awaited()
    auto_water.assert_not_awaited()


async def test_normal_readings_are_unaffected(fake_db, downstream, sent):
    fake_db.get_sensor_by_device_address.return_value = sensor_row()
    fake_db.get_sensor_calibration.return_value = None

    await main.process_reading(msg(raw_adc=678))

    fake_db.insert_reading.assert_awaited_once()
    fake_db.insert_dead_letter.assert_not_awaited()


async def test_digital_sensors_bypass_the_raw_check(fake_db, downstream, sent):
    # DHT22 sends a computed value and raw_adc=null; there's no raw to judge.
    fake_db.get_sensor_by_device_address.return_value = sensor_row()

    await main.process_reading(
        msg(address="gpio:4:temp", value=0.0, raw_adc=None, sensor_type="temperature"))

    fake_db.insert_reading.assert_awaited_once()
    assert fake_db.insert_reading.await_args.args[1] == 0.0


async def test_a_sustained_disconnect_notifies_telegram(fake_db, downstream, sent):
    fake_db.get_sensor_by_device_address.return_value = sensor_row()

    for _ in range(3):
        await main.process_reading(msg(raw_adc=0))

    assert len(sent) == 1
    assert "disconnected" in sent[0]
