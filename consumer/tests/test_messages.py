from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from consumer.messages import ReadingMessage, StatusMessage, WateringEventMessage
from consumer.tests.conftest import event_payload, reading_payload, status_payload


def test_adc_reading_parses_with_null_value():
    msg = ReadingMessage.model_validate(reading_payload())
    assert msg.value is None
    assert msg.raw_adc == 612
    assert isinstance(msg.reading_id, UUID)
    assert msg.recorded_at == datetime(2026, 8, 7, 10, 23, tzinfo=UTC)


def test_digital_reading_parses_with_null_raw_adc():
    msg = ReadingMessage.model_validate(
        reading_payload(address="gpio:4:temp", value=22.5, raw_adc=None,
                        sensor_type="temperature"))
    assert msg.value == 22.5
    assert msg.raw_adc is None


@pytest.mark.parametrize("bad", [
    {"address": ""},
    {"address": "x" * 51},
    {"raw_adc": -1},
    {"reading_id": "not-a-uuid"},
    {"sensor_type": ""},
    {"recorded_at": "whenever"},
])
def test_malformed_readings_are_rejected(bad):
    with pytest.raises(ValidationError):
        ReadingMessage.model_validate(reading_payload(**bad))


def test_missing_field_is_rejected():
    payload = reading_payload()
    del payload["value"]
    with pytest.raises(ValidationError):
        ReadingMessage.model_validate(payload)


@pytest.mark.parametrize("event", [
    "watering_started", "watering_completed", "watering_failed", "command_expired",
])
def test_all_pi_event_types_are_accepted(event):
    # watering_started in particular: it was absent from an earlier draft of the
    # model, which would have dead-lettered every one of them and left
    # watering_events.started_at permanently NULL.
    msg = WateringEventMessage.model_validate(event_payload(event=event))
    assert msg.event == event


def test_unknown_event_type_is_rejected():
    with pytest.raises(ValidationError):
        WateringEventMessage.model_validate(event_payload(event="relay_exploded"))


@pytest.mark.parametrize("channel", [0, 9, -1])
def test_relay_channel_is_bounded(channel):
    with pytest.raises(ValidationError):
        WateringEventMessage.model_validate(event_payload(relay_channel=channel))


def test_failed_event_carries_error_and_no_duration():
    msg = WateringEventMessage.model_validate(
        event_payload(event="watering_failed", error="relay_not_responding",
                      duration_sec=None, triggered_by=None))
    assert msg.error == "relay_not_responding"
    assert msg.duration_sec is None


def test_last_will_status_has_no_timestamp():
    # The broker fires the pre-baked will at an unknown later moment, so it
    # deliberately omits recorded_at.
    msg = StatusMessage.model_validate(
        {"schema_version": 2, "device_id": status_payload()["device_id"],
         "status": "offline"})
    assert msg.recorded_at is None
    assert msg.firmware_version is None


def test_online_status_carries_device_metadata():
    msg = StatusMessage.model_validate(status_payload(
        firmware_version="1.2.0", boot_at="2026-08-07T08:59:00Z",
        capabilities={"tank_sensor": False, "dht22": False}))
    assert msg.firmware_version == "1.2.0"
    assert msg.capabilities == {"tank_sensor": False, "dht22": False}
    assert msg.boot_at == datetime(2026, 8, 7, 8, 59, tzinfo=UTC)


def test_unknown_status_value_is_rejected():
    with pytest.raises(ValidationError):
        StatusMessage.model_validate(status_payload(status="probably_fine"))
