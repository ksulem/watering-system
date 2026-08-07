"""Threshold alerting.

Thresholds are entirely DB-driven (`sensor_alert_config`) — nothing hardcoded,
so the API can retune them without a redeploy. This is separate from
`watering_config.auto_threshold`: that fires a pump, this sends a message.
"""
import logging

from consumer import db, metrics
from consumer.notify import notify_telegram

logger = logging.getLogger("consumer.alerts")

# Display units only. sensor_types.unit is the source of truth, but joining it
# on every reading to render a string isn't worth the query.
UNITS = {
    "moisture": "%",
    "temperature": "°C",
    "humidity": "%RH",
    "water_level": "%",
    "light": " lux",
}

ARROW = {"too_low": "🔻", "too_high": "🔺"}


def format_alert(label: str, sensor_type: str, alert_type: str,
                 value: float, threshold: float) -> str:
    unit = UNITS.get(sensor_type, "")
    direction = "below" if alert_type == "too_low" else "above"
    return (f"{ARROW.get(alert_type, '⚠️')} {label}: {value:.1f}{unit} "
            f"({direction} threshold {threshold:.1f}{unit})")


async def check_thresholds(sensor, sensor_type: str, value: float) -> None:
    configs = await db.get_alert_configs(sensor["id"])
    for cfg in configs:
        alert_type = cfg["alert_type"]
        triggered = (
            (alert_type == "too_low" and value < cfg["threshold"]) or
            (alert_type == "too_high" and value > cfg["threshold"])
        )
        if not triggered:
            continue
        # cooldown_min is minutes. Converting it (a `// 60` appeared in an early
        # draft) would floor every sub-hour cooldown to zero and re-alert on
        # every single reading.
        if await db.recent_alert_exists(sensor["id"], alert_type,
                                        minutes=cfg["cooldown_min"]):
            continue

        await db.create_alert(sensor["id"], alert_type, value, sensor["plant_id"])
        metrics.alerts_sent_total.labels(alert_type=alert_type).inc()
        label = sensor["label"] or f"sensor {sensor['id']}"
        await notify_telegram(
            format_alert(label, sensor_type, alert_type, value, cfg["threshold"]))
