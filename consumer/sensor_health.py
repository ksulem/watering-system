"""Detect sensors that are disconnected, shorted, or glitching.

This exists because the failure is silent and dangerous. A capacitive moisture
sensor that comes unplugged leaves its ADC input floating, so it reads ~0 —
which the calibration curve happily maps to **100% moisture**. The plant then
looks perfectly watered: no low-moisture alert fires, and auto-water never
triggers. The plant quietly dies while the dashboard says it's fine.

So an implausible raw value is never stored as a reading. It's preserved as a
dead letter (the raw fact isn't lost) and, if it persists, raised as a
`sensor_error` alert and pushed to Telegram.

Persistence matters: a single stray zero is usually an SPI bus glitch, not an
unplugged sensor. Alerting on one would cry wolf. Alerting only after several
consecutive bad reads distinguishes "blip" from "someone pulled the sensor out".
"""
import logging

from consumer import db, metrics
from consumer.notify import notify_telegram

logger = logging.getLogger("consumer.sensor_health")

# A disconnected input reads ~0-5; a shorted one pegs at the top of the range.
# Real readings on this hardware have spanned roughly 300-820, so this band is
# deliberately generous — it catches broken wiring, not unusual soil.
PLAUSIBLE_RAW = {
    "moisture": (50, 1015),
    "water_level": (5, 1020),
    "light": (0, 1023),      # an LDR legitimately reads 0 in full darkness
}
DEFAULT_PLAUSIBLE = (5, 1020)

# ~3 consecutive bad reads ≈ 3.5 minutes at the current 70s cadence.
CONSECUTIVE_BEFORE_ALERT = 3

# A sensor left unplugged would otherwise alert every reading, forever.
ALERT_COOLDOWN_MIN = 120

# sensor_id -> consecutive implausible reads. In memory on purpose: a restart
# re-arms the counter, and a genuinely broken sensor re-trips within minutes.
_consecutive: dict[int, int] = {}
# Sensors currently considered faulty, so recovery can be announced once.
_faulty: set[int] = set()


def is_plausible(raw_adc: int, sensor_type: str) -> bool:
    low, high = PLAUSIBLE_RAW.get(sensor_type, DEFAULT_PLAUSIBLE)
    return low <= raw_adc <= high


def _label(sensor) -> str:
    return sensor["label"] or f"sensor {sensor['id']}"


async def record_fault(sensor, raw_adc: int, sensor_type: str) -> None:
    """Note an implausible reading; alert once it looks like a real fault."""
    sensor_id = sensor["id"]
    count = _consecutive.get(sensor_id, 0) + 1
    _consecutive[sensor_id] = count
    metrics.sensor_implausible_total.labels(address=sensor["address"]).inc()

    logger.warning("implausible raw_adc=%s from %s (%d in a row)",
                   raw_adc, _label(sensor), count)

    if count < CONSECUTIVE_BEFORE_ALERT or sensor_id in _faulty:
        return
    if await db.recent_alert_exists(sensor_id, "sensor_error",
                                    minutes=ALERT_COOLDOWN_MIN):
        _faulty.add(sensor_id)
        return

    _faulty.add(sensor_id)
    await db.create_alert(sensor_id, "sensor_error", float(raw_adc),
                          sensor["plant_id"])
    metrics.alerts_sent_total.labels(alert_type="sensor_error").inc()
    await notify_telegram(
        f"🔌 <b>{_label(sensor)}</b> looks disconnected.\n\n"
        f"The last {count} readings came back as {raw_adc} — outside anything a "
        f"working sensor produces. Check the wire is seated in the ADC and in "
        f"the soil.\n\n"
        f"Readings from it are being discarded until it recovers, so nothing "
        f"records a false 100% moisture.")


async def record_ok(sensor) -> None:
    """Clear the fault counter; announce recovery if it had been alerting."""
    sensor_id = sensor["id"]
    _consecutive.pop(sensor_id, None)
    if sensor_id in _faulty:
        _faulty.discard(sensor_id)
        logger.info("%s is reading normally again", _label(sensor))
        await notify_telegram(f"✅ <b>{_label(sensor)}</b> is reading normally again.")
