from unittest.mock import AsyncMock

import pytest

from consumer import main
from consumer.messages import ReadingMessage
from consumer.tests.conftest import reading_payload, sensor_row


@pytest.fixture
def downstream(monkeypatch):
    """Stub the post-insert side effects so this file tests ingest only."""
    thresholds, auto_water = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, "check_thresholds", thresholds)
    monkeypatch.setattr(main, "maybe_auto_water", auto_water)
    return thresholds, auto_water


def msg(**overrides) -> ReadingMessage:
    return ReadingMessage.model_validate(reading_payload(**overrides))


async def test_unknown_sensor_is_dead_lettered_without_claiming_the_event_id(
        fake_db, downstream):
    fake_db.get_sensor_by_device_address.return_value = None

    await main.process_reading(msg())

    assert fake_db.insert_dead_letter.await_args.args[3] == "unknown_sensor"
    # Critical: the idempotency key stays unclaimed so that once the sensors row
    # is seeded, a redelivery of this same reading still inserts.
    fake_db.mark_event_processed.assert_not_awaited()
    fake_db.insert_reading.assert_not_awaited()


async def test_duplicate_delivery_inserts_nothing(fake_db, downstream):
    fake_db.get_sensor_by_device_address.return_value = sensor_row()
    fake_db.mark_event_processed.return_value = False

    await main.process_reading(msg())

    fake_db.insert_reading.assert_not_awaited()
    thresholds, auto_water = downstream
    thresholds.assert_not_awaited()
    auto_water.assert_not_awaited()


async def test_adc_reading_is_calibrated_before_insert(fake_db, downstream):
    fake_db.get_sensor_by_device_address.return_value = sensor_row()
    fake_db.get_sensor_calibration.return_value = None   # fall back to defaults

    await main.process_reading(msg(raw_adc=510))

    _, kwargs = fake_db.insert_reading.await_args
    args = fake_db.insert_reading.await_args.args
    assert args[0] == 1                      # sensor_id
    assert args[1] == pytest.approx(50.0)    # calibrated %
    assert args[2] == 510                    # raw_adc preserved
    assert kwargs["conn"] is not None        # written inside the transaction


async def test_precomputed_value_skips_calibration(fake_db, downstream):
    fake_db.get_sensor_by_device_address.return_value = sensor_row()

    await main.process_reading(
        msg(address="gpio:4:temp", value=22.5, raw_adc=None, sensor_type="temperature"))

    fake_db.get_sensor_calibration.assert_not_awaited()
    assert fake_db.insert_reading.await_args.args[1] == 22.5


async def test_uncalibratable_reading_is_dead_lettered_without_claiming_the_event_id(
        fake_db, downstream):
    fake_db.get_sensor_by_device_address.return_value = sensor_row()
    fake_db.get_sensor_calibration.return_value = None

    # Unknown sensor_type, raw counts only, no calibration row: nothing can turn
    # this into the NOT NULL value column.
    await main.process_reading(msg(sensor_type="conductivity", address="adc:6"))

    assert fake_db.insert_dead_letter.await_args.args[3] == "uncalibratable"
    fake_db.mark_event_processed.assert_not_awaited()
    fake_db.insert_reading.assert_not_awaited()


async def test_successful_reading_triggers_alerts_and_auto_water(fake_db, downstream):
    sensor = sensor_row(plant_id=3)
    fake_db.get_sensor_by_device_address.return_value = sensor
    fake_db.get_sensor_calibration.return_value = None
    thresholds, auto_water = downstream

    await main.process_reading(msg(raw_adc=700))

    thresholds.assert_awaited_once()
    assert thresholds.await_args.args[0] is sensor
    auto_water.assert_awaited_once()
    assert auto_water.await_args.args[0] == 3


async def test_sensor_without_a_plant_never_auto_waters(fake_db, downstream):
    # Tank-level and ambient-light sensors have plant_id NULL.
    fake_db.get_sensor_by_device_address.return_value = sensor_row(plant_id=None)
    fake_db.get_sensor_calibration.return_value = None
    thresholds, auto_water = downstream

    await main.process_reading(msg())

    thresholds.assert_awaited_once()
    auto_water.assert_not_awaited()


async def test_sensor_lookup_uses_the_full_address_tuple(fake_db, downstream):
    fake_db.get_sensor_by_device_address.return_value = sensor_row()
    fake_db.get_sensor_calibration.return_value = None
    m = msg()

    await main.process_reading(m)

    args = fake_db.get_sensor_by_device_address.await_args.args
    assert args == (m.device_id, "adc:0", "moisture")
