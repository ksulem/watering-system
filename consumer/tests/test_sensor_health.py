"""Disconnected-sensor detection.

The failure this guards against is silent: a floating ADC input reads ~0, which
calibrates to 100% moisture, so the plant looks watered and neither the alert
nor auto-water ever fires.
"""
import pytest

from consumer import sensor_health
from consumer.tests.conftest import sensor_row


@pytest.fixture(autouse=True)
def clean_state():
    sensor_health._consecutive.clear()
    sensor_health._faulty.clear()
    yield
    sensor_health._consecutive.clear()
    sensor_health._faulty.clear()


@pytest.mark.parametrize("raw", [0, 5, 49, 1016, 1023])
def test_disconnected_or_shorted_values_are_implausible(raw):
    assert not sensor_health.is_plausible(raw, "moisture")


@pytest.mark.parametrize("raw", [300, 470, 678, 814, 1015])
def test_real_readings_stay_plausible(raw):
    # Every value this hardware has actually produced must pass — the band
    # catches broken wiring, not unusual soil.
    assert sensor_health.is_plausible(raw, "moisture")


def test_light_sensor_may_legitimately_read_zero():
    # An LDR in full darkness is not a fault.
    assert sensor_health.is_plausible(0, "light")
    assert not sensor_health.is_plausible(0, "moisture")


def test_unknown_sensor_type_uses_the_default_band():
    assert sensor_health.is_plausible(500, "conductivity")
    assert not sensor_health.is_plausible(0, "conductivity")


async def test_a_single_glitch_does_not_alert(fake_db, sent):
    # One stray zero is an SPI bus blip, not an unplugged sensor. Alerting here
    # would cry wolf several times a day.
    await sensor_health.record_fault(sensor_row(), 0, "moisture")

    fake_db.create_alert.assert_not_awaited()
    assert sent == []


async def test_sustained_faults_alert_once(fake_db, sent):
    for _ in range(5):
        await sensor_health.record_fault(sensor_row(), 0, "moisture")

    # Threshold is 3 consecutive; the 4th and 5th must not re-notify.
    fake_db.create_alert.assert_awaited_once()
    assert fake_db.create_alert.await_args.args[1] == "sensor_error"
    assert len(sent) == 1
    assert "disconnected" in sent[0]


async def test_a_good_reading_resets_the_counter(fake_db, sent):
    s = sensor_row()
    await sensor_health.record_fault(s, 0, "moisture")
    await sensor_health.record_fault(s, 0, "moisture")
    await sensor_health.record_ok(s)
    await sensor_health.record_fault(s, 0, "moisture")

    # Two before the reset plus one after must not add up to an alert.
    fake_db.create_alert.assert_not_awaited()
    assert sent == []


async def test_recovery_is_announced(fake_db, sent):
    s = sensor_row()
    for _ in range(3):
        await sensor_health.record_fault(s, 0, "moisture")
    sent.clear()

    await sensor_health.record_ok(s)

    assert len(sent) == 1
    assert "reading normally again" in sent[0]


async def test_recovery_is_silent_if_it_never_alerted(fake_db, sent):
    await sensor_health.record_fault(sensor_row(), 0, "moisture")
    await sensor_health.record_ok(sensor_row())
    assert sent == []


async def test_cooldown_suppresses_a_repeat_alert(fake_db, sent):
    # A sensor left unplugged would otherwise alert forever.
    fake_db.recent_alert_exists.return_value = True

    for _ in range(4):
        await sensor_health.record_fault(sensor_row(), 0, "moisture")

    fake_db.create_alert.assert_not_awaited()
    assert sent == []
