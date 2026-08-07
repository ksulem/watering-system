"""Watering acknowledgement events published by the Pi.

The Pi is the only authority on whether a relay actually fired; these events are
how `watering_events` learns the real outcome of a command it dispatched.
"""
import logging
from datetime import UTC, datetime

from consumer import db, metrics
from consumer.messages import WateringEventMessage
from consumer.notify import notify_telegram

logger = logging.getLogger("consumer.event_handler")


async def process(event: WateringEventMessage) -> None:
    notify_text = None

    # Idempotency mark and the lifecycle update must commit atomically. A crash
    # between them would leave the event marked processed with the command still
    # 'dispatched' — and since the Pi only republishes a terminal event when it
    # receives the command again, that acknowledgement would be lost for good.
    async with db.transaction() as conn:
        if not await db.mark_event_processed(
                event.event_id, event_type=event.event,
                command_id=event.command_id, conn=conn):
            return  # duplicate delivery

        match event.event:
            case "watering_started":
                # Published immediately before the relay is energised, so
                # recorded_at is real actuation time, not broker-delivery time.
                await db.update_watering_event(
                    event.command_id, "dispatched",
                    started_at=event.recorded_at, conn=conn)

            case "watering_completed":
                changed = await db.update_watering_event(
                    event.command_id, "completed",
                    completed_at=datetime.now(UTC), conn=conn)
                if changed:
                    metrics.watering_commands_total.labels(status="completed").inc()
                    notify_text = (f"✅ Plant {event.plant_id} watered for "
                                   f"{event.duration_sec}s")

            case "watering_failed":
                changed = await db.update_watering_event(
                    event.command_id, "failed",
                    completed_at=datetime.now(UTC), conn=conn)
                if changed:
                    metrics.watering_commands_total.labels(status="failed").inc()
                    notify_text = f"❌ Watering failed — {event.error}"

            case "command_expired":
                changed = await db.update_watering_event(
                    event.command_id, "expired",
                    completed_at=datetime.now(UTC), conn=conn)
                if changed:
                    metrics.watering_commands_total.labels(status="expired").inc()

    # Side effects only after the transaction commits — a Telegram timeout must
    # not roll back or repeat a lifecycle transition.
    if notify_text:
        await notify_telegram(notify_text)
