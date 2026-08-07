import pytest

from consumer.alerts import check_thresholds, format_alert
from consumer.tests.conftest import sensor_row


def config(alert_type="too_low", threshold=25.0, cooldown_min=360):
    return {"alert_type": alert_type, "threshold": threshold,
            "cooldown_min": cooldown_min}


async def test_no_configured_thresholds_means_no_alerts(fake_db, sent):
    fake_db.get_alert_configs.return_value = []

    await check_thresholds(sensor_row(), "moisture", 5.0)

    fake_db.create_alert.assert_not_awaited()
    assert sent == []


async def test_value_below_threshold_raises_an_alert(fake_db, sent):
    fake_db.get_alert_configs.return_value = [config(threshold=25.0)]

    await check_thresholds(sensor_row(plant_id=4), "moisture", 18.3)

    fake_db.create_alert.assert_awaited_once()
    args = fake_db.create_alert.await_args.args
    assert args[:3] == (1, "too_low", 18.3)
    assert args[3] == 4          # plant_id carried through for the API
    assert len(sent) == 1


async def test_value_above_threshold_is_quiet(fake_db, sent):
    fake_db.get_alert_configs.return_value = [config(threshold=25.0)]

    await check_thresholds(sensor_row(), "moisture", 40.0)

    fake_db.create_alert.assert_not_awaited()
    assert sent == []


async def test_cooldown_suppresses_a_repeat_alert(fake_db, sent):
    fake_db.get_alert_configs.return_value = [config(threshold=25.0, cooldown_min=360)]
    fake_db.recent_alert_exists.return_value = True

    await check_thresholds(sensor_row(), "moisture", 18.3)

    fake_db.create_alert.assert_not_awaited()
    assert sent == []


async def test_cooldown_is_checked_in_minutes(fake_db, sent):
    # A sub-hour cooldown converted to hours would floor to 0 and re-alert on
    # every reading — 6 pushes a minute.
    fake_db.get_alert_configs.return_value = [config(cooldown_min=45)]

    await check_thresholds(sensor_row(), "moisture", 10.0)

    assert fake_db.recent_alert_exists.await_args.kwargs["minutes"] == 45


async def test_too_high_triggers_on_the_other_side(fake_db, sent):
    fake_db.get_alert_configs.return_value = [config(alert_type="too_high",
                                                     threshold=30.0)]

    await check_thresholds(sensor_row(), "temperature", 34.0)

    assert fake_db.create_alert.await_args.args[1] == "too_high"


async def test_both_directions_can_be_configured_on_one_sensor(fake_db, sent):
    fake_db.get_alert_configs.return_value = [
        config(alert_type="too_low", threshold=20.0),
        config(alert_type="too_high", threshold=30.0),
    ]

    await check_thresholds(sensor_row(), "temperature", 25.0)

    fake_db.create_alert.assert_not_awaited()


@pytest.mark.parametrize("sensor_type,unit", [
    ("moisture", "%"), ("temperature", "°C"), ("light", " lux"),
])
def test_alert_text_names_the_sensor_and_its_unit(sensor_type, unit):
    text = format_alert("Монстера moisture", sensor_type, "too_low", 18.34, 25.0)
    assert "Монстера moisture" in text
    assert f"18.3{unit}" in text
    assert f"25.0{unit}" in text
    assert "below" in text
