"""Auto-watering decision, evaluated after every moisture reading.

The Pi holds no thresholds or schedules — it only executes commands. All the
"should we water?" logic lives here, which is why changing a threshold is an
API call rather than a firmware push.
"""
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from consumer import db

logger = logging.getLogger("consumer.auto_water")

COMMAND_TTL = timedelta(minutes=5)


async def maybe_auto_water(plant_id: int, sensor_type: str, value: float) -> None:
    if sensor_type != "moisture":
        return

    config = await db.get_watering_config(plant_id)
    # No config row means this plant has no relay assigned yet — nothing to do.
    if config is None:
        return
    if config["mode"] != "auto" or value >= config["auto_threshold"]:
        return
    if await db.has_active_watering_command(plant_id):
        return
    if await db.watered_within(plant_id, minutes=config["min_interval_min"]):
        return

    command_id = uuid4()
    expires_at = datetime.now(UTC) + COMMAND_TTL

    created = await db.try_create_pending_watering_command(
        plant_id=plant_id,
        device_id=config["device_id"],
        command_id=command_id,
        triggered_by="auto",
        duration_sec=config["duration_sec"],
        relay_channel=config["relay_channel"],
        expires_at=expires_at,
    )
    if not created:
        # The FOR UPDATE guard fired — another task created a command for this
        # plant while we were deciding. Correct outcome, nothing to report.
        return

    logger.info("auto-water queued plant_id=%s command_id=%s moisture=%.1f%% threshold=%.1f%%",
                plant_id, command_id, value, config["auto_threshold"])
