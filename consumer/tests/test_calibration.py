import pytest

from consumer.calibration import calibrated_value


@pytest.mark.parametrize("raw,expected", [
    (720, 0.0),      # dry-air reference
    (300, 100.0),    # submerged reference
    (510, 50.0),     # midpoint
])
def test_moisture_defaults_map_linearly(raw, expected):
    assert calibrated_value(raw, "moisture", None) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [800, 1023])
def test_drier_than_reference_clamps_to_zero(raw):
    assert calibrated_value(raw, "moisture", None) == 0.0


@pytest.mark.parametrize("raw", [200, 0])
def test_wetter_than_reference_clamps_to_hundred(raw):
    assert calibrated_value(raw, "moisture", None) == 100.0


def test_calibration_row_overrides_type_defaults():
    cal = {"raw_dry": 800, "raw_wet": 400}
    assert calibrated_value(600, "moisture", cal) == pytest.approx(50.0)
    # The same raw value against the defaults would read differently — proving
    # the row, not the fallback, is in play.
    assert calibrated_value(600, "moisture", None) != pytest.approx(50.0)


def test_water_level_defaults_are_inverted():
    assert calibrated_value(1023, "water_level", None) == 0.0     # empty tank
    assert calibrated_value(0, "water_level", None) == 100.0      # full tank


def test_unknown_sensor_type_without_calibration_is_uncalibratable():
    # Signals the caller to dead-letter rather than insert a bogus value into a
    # NOT NULL column.
    assert calibrated_value(500, "conductivity", None) is None


def test_zero_span_calibration_is_uncalibratable():
    # The sensor_calibration_distinct CHECK makes this unreachable from the DB,
    # but it must not become a ZeroDivisionError inside a handler task.
    assert calibrated_value(500, "moisture", {"raw_dry": 500, "raw_wet": 500}) is None
