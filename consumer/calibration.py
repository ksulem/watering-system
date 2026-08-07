"""Server-side calibration of raw ADC counts to a 0-100% value.

Keeping this on the backend means recalibrating a drifting capacitive sensor is
a DB update, not a Pi redeploy — and the Pi stays a pure transducer with no DB
or API dependency.
"""

# Fallback used when a sensor has no `sensor_calibration` row.
SENSOR_TYPE_DEFAULTS = {
    "moisture": {"raw_dry": 720, "raw_wet": 300},    # capacitive in air vs. water
    "water_level": {"raw_dry": 1023, "raw_wet": 0},  # tank empty vs. full
    "light": {"raw_dry": 0, "raw_wet": 1023},        # linear LDR mapping
}


def calibrated_value(raw_adc: int, sensor_type: str, cal_row) -> float | None:
    """Linear-map raw_adc onto 0-100%, clamped.

    Returns None when the reading cannot be calibrated at all — an unknown
    sensor_type with no calibration row. `sensor_readings.value` is NOT NULL, so
    the caller dead-letters instead of inserting; a None here means a sensor
    type was added on the Pi without either a DB calibration row or an entry in
    SENSOR_TYPE_DEFAULTS.
    """
    if cal_row is not None:
        raw_dry, raw_wet = cal_row["raw_dry"], cal_row["raw_wet"]
    else:
        defaults = SENSOR_TYPE_DEFAULTS.get(sensor_type)
        if defaults is None:
            return None
        raw_dry, raw_wet = defaults["raw_dry"], defaults["raw_wet"]

    # sensor_calibration_distinct CHECK makes this impossible for DB rows, but a
    # zero span would be a ZeroDivisionError inside a task — cheap to rule out.
    if raw_dry == raw_wet:
        return None

    pct = 100.0 * (raw_dry - raw_adc) / (raw_dry - raw_wet)
    return max(0.0, min(100.0, pct))
