from datetime import UTC, datetime, timedelta
from uuid import UUID

from consumer.auto_water import maybe_auto_water
from consumer.tests.conftest import DEVICE_ID


def config(mode="auto", auto_threshold=30.0, duration_sec=10, relay_channel=2,
           min_interval_min=240):
    return {"plant_id": 3, "device_id": UUID(DEVICE_ID), "mode": mode,
            "auto_threshold": auto_threshold, "duration_sec": duration_sec,
            "relay_channel": relay_channel, "min_interval_min": min_interval_min}


async def test_non_moisture_readings_never_water(fake_db):
    await maybe_auto_water(3, "temperature", 1.0)

    fake_db.get_watering_config.assert_not_awaited()


async def test_plant_without_watering_config_is_skipped(fake_db):
    # No config row means no relay is assigned to this plant yet — the state
    # every plant is in until the relay hardware is wired.
    fake_db.get_watering_config.return_value = None

    await maybe_auto_water(3, "moisture", 5.0)

    fake_db.try_create_pending_watering_command.assert_not_awaited()


async def test_manual_mode_never_waters(fake_db):
    fake_db.get_watering_config.return_value = config(mode="manual")

    await maybe_auto_water(3, "moisture", 5.0)

    fake_db.try_create_pending_watering_command.assert_not_awaited()


async def test_moist_enough_soil_is_left_alone(fake_db):
    fake_db.get_watering_config.return_value = config(auto_threshold=30.0)

    await maybe_auto_water(3, "moisture", 30.0)   # at the threshold, not below

    fake_db.try_create_pending_watering_command.assert_not_awaited()


async def test_command_already_in_flight_blocks_a_second(fake_db):
    fake_db.get_watering_config.return_value = config()
    fake_db.has_active_watering_command.return_value = True

    await maybe_auto_water(3, "moisture", 12.0)

    fake_db.try_create_pending_watering_command.assert_not_awaited()


async def test_recent_watering_blocks_a_repeat(fake_db):
    fake_db.get_watering_config.return_value = config(min_interval_min=240)
    fake_db.watered_within.return_value = True

    await maybe_auto_water(3, "moisture", 12.0)

    assert fake_db.watered_within.await_args.kwargs["minutes"] == 240
    fake_db.try_create_pending_watering_command.assert_not_awaited()


async def test_dry_soil_queues_a_watering_command(fake_db):
    fake_db.get_watering_config.return_value = config(
        duration_sec=15, relay_channel=2)

    before = datetime.now(UTC)
    await maybe_auto_water(3, "moisture", 12.0)

    kwargs = fake_db.try_create_pending_watering_command.await_args.kwargs
    assert kwargs["plant_id"] == 3
    assert kwargs["device_id"] == UUID(DEVICE_ID)
    assert kwargs["triggered_by"] == "auto"
    assert kwargs["duration_sec"] == 15
    assert kwargs["relay_channel"] == 2
    assert isinstance(kwargs["command_id"], UUID)
    # 5-minute TTL: the Pi refuses anything older, so a command queued during a
    # long outage doesn't water hours after the soil reading that prompted it.
    assert before + timedelta(minutes=4) < kwargs["expires_at"] <= \
        datetime.now(UTC) + timedelta(minutes=5)


async def test_losing_the_concurrency_race_is_not_an_error(fake_db):
    # Two tasks acting on the same dry reading serialize on SELECT FOR UPDATE;
    # the loser just gets False back.
    fake_db.get_watering_config.return_value = config()
    fake_db.try_create_pending_watering_command.return_value = False

    await maybe_auto_water(3, "moisture", 12.0)   # must not raise
