from datetime import UTC, datetime

import pytest

from bot.formatting import (
    format_reading,
    format_status,
    format_water_targets,
    plant_title,
    time_ago,
)
from bot.tests.conftest import plant, sensor

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("stamp,expected", [
    ("2026-08-07T11:59:30+00:00", "just now"),
    ("2026-08-07T11:30:00+00:00", "30 min ago"),
    ("2026-08-07T11:00:00+00:00", "1 hour ago"),
    ("2026-08-07T09:00:00+00:00", "3 hours ago"),
    ("2026-08-05T12:00:00+00:00", "2 days ago"),
    ("2026-08-06T12:00:00+00:00", "1 day ago"),
])
def test_time_ago(stamp, expected):
    assert time_ago(stamp, now=NOW) == expected


def test_time_ago_handles_never_watered():
    # last_watered_at is NULL for every plant until one is actually watered.
    assert time_ago(None) == "never"


def test_time_ago_handles_garbage():
    assert time_ago("not-a-date") == "never"


def test_time_ago_treats_naive_timestamps_as_utc():
    # Guards against a naive/aware comparison raising TypeError mid-handler.
    assert time_ago("2026-08-07T11:00:00", now=NOW) == "1 hour ago"


def test_plant_title_prefers_the_owners_language():
    # The Russian name is what the owner calls the plant; English is a gloss.
    assert plant_title(plant()) == "Монстера (Monstera) — Kitchen"


def test_plant_title_without_english_or_location():
    assert plant_title(plant(name_en=None, location=None)) == "Монстера"


def test_dry_moisture_is_flagged_against_the_plants_own_threshold():
    text = format_reading(sensor(value=18.0), low_threshold=25.0)
    assert "⚠️ Low" in text


def test_moisture_above_threshold_is_not_flagged():
    text = format_reading(sensor(value=40.0), low_threshold=25.0)
    assert "⚠️" not in text and "✅" in text


def test_moisture_without_a_configured_threshold_uses_the_default():
    # A plant with no watering_config still deserves a warning when it's dry.
    assert "⚠️ Low" in format_reading(sensor(value=10.0), low_threshold=None)
    assert "⚠️" not in format_reading(sensor(value=80.0), low_threshold=None)


def test_sensor_that_never_reported():
    assert "no reading yet" in format_reading(sensor(value=None), None)


def test_non_moisture_sensors_are_never_flagged():
    text = format_reading(sensor(sensor_type="temperature", unit="°C", value=1.0), None)
    assert "⚠️" not in text and "✅" not in text


def test_status_groups_sensors_under_their_plant():
    text = format_status(
        [plant(1), plant(2, name="Алоказия", name_en="Alocasia", location=None)],
        [sensor(1, plant_id=1, value=40.0), sensor(2, plant_id=2, value=12.0)],
        now=NOW)

    assert "Монстера (Monstera)" in text
    assert "Алоказия (Alocasia)" in text
    assert text.index("Монстера") < text.index("Алоказия")


def test_status_reports_plants_with_no_relay():
    # Every plant is in this state until the relay hardware is wired.
    text = format_status([plant(mode=None, auto_threshold=None)], [sensor()], now=NOW)
    assert "No relay assigned" in text
    assert "Mode:" not in text


def test_status_shows_mode_and_last_watered_when_configured():
    text = format_status(
        [plant(mode="auto", last_watered_at="2026-08-07T09:00:00+00:00")],
        [sensor()], now=NOW)
    assert "Mode: AUTO" in text
    assert "Last watered: 3 hours ago" in text


def test_status_separates_device_level_sensors():
    # Tank level and ambient light have plant_id NULL — they belong to no plant.
    text = format_status(
        [plant()],
        [sensor(1, plant_id=1),
         sensor(7, plant_id=None, sensor_type="water_level", value=72.0)],
        now=NOW)
    assert "Device sensors" in text


def test_status_with_no_plants():
    assert "No plants registered" in format_status([], [], now=NOW)


def test_status_notes_a_plant_with_no_sensors():
    assert "no sensors attached" in format_status([plant()], [], now=NOW)


def test_water_menu_lists_only_manual_plants():
    text = format_water_targets([
        plant(1, mode="manual"),
        plant(2, name="Алоказия", mode="auto"),
    ])
    assert "/water_1" in text
    assert "/water_2" not in text


def test_water_menu_explains_when_everything_is_auto():
    text = format_water_targets([plant(1, mode="auto")])
    assert "AUTO mode" in text
    assert "/water_" not in text


def test_water_menu_explains_when_no_relay_exists():
    # The state the system is actually in today.
    text = format_water_targets([plant(1, mode=None), plant(2, mode=None)])
    assert "No plant has a relay assigned" in text
