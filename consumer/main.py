"""Consumer service — MQTT to TimescaleDB.

Subscribes to readings, events and status with a persistent session, validates
every payload, and fans each message out to a task so a slow DB write never
stalls the message loop.
"""
import asyncio
import json
import logging
import os
import random
import signal
from datetime import UTC, datetime

from aiomqtt import Client as MQTTClient
from aiomqtt import MqttError
from pydantic import ValidationError

from consumer import cleanup, db, event_handler, metrics, notify
from consumer.alerts import check_thresholds
from consumer.auto_water import maybe_auto_water
from consumer.calibration import calibrated_value
from consumer.health import start_health_server
from consumer.messages import (
    SUPPORTED_SCHEMA_VERSIONS,
    ReadingMessage,
    StatusMessage,
    WateringEventMessage,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("consumer")

DRAIN_TIMEOUT = 10       # seconds to let in-flight tasks commit on shutdown
MAX_INFLIGHT_DB = 50     # concurrent tasks allowed to hold a pool connection

shutdown = asyncio.Event()
inflight: set[asyncio.Task] = set()
_db_semaphore = asyncio.Semaphore(MAX_INFLIGHT_DB)

MODEL_BY_SUFFIX = {
    "readings": ReadingMessage,
    "events": WateringEventMessage,
    "status": StatusMessage,
}


def _spawn(coro) -> asyncio.Task:
    """Run a handler concurrently, keeping a reference and logging failures.

    asyncio drops both: an unreferenced task can be garbage-collected mid-flight,
    and an unretrieved exception vanishes silently.
    """
    task = asyncio.create_task(_with_semaphore(coro))
    inflight.add(task)
    task.add_done_callback(inflight.discard)
    task.add_done_callback(
        lambda t: logger.error("handler task failed", exc_info=t.exception())
        if not t.cancelled() and t.exception() else None
    )
    return task


async def _with_semaphore(coro) -> None:
    """Bound simultaneous pool checkouts regardless of message burst rate.

    Never contended at steady state (~6 readings/min); it exists for the
    reconnect case where the broker replays a large queued backlog at once.
    """
    async with _db_semaphore:
        await coro


# --- Handlers ----------------------------------------------------------------

async def process_reading(msg: ReadingMessage) -> None:
    topic = f"plant-monitor/{msg.device_id}/readings"

    # Resolve the sensor FIRST. An unregistered sensor is dead-lettered but
    # deliberately NOT marked processed: leaving the idempotency key unclaimed
    # means that once the sensors row is seeded, a later redelivery inserts
    # normally. The dead letter preserves the payload for manual backfill.
    sensor = await db.get_sensor_by_device_address(
        msg.device_id, msg.address, msg.sensor_type)
    if sensor is None:
        metrics.mqtt_unknown_sensor_total.labels(device_id=str(msg.device_id)).inc()
        await db.insert_dead_letter(
            topic, msg.model_dump_json(),
            f"no sensors row for address={msg.address} sensor_type={msg.sensor_type}",
            "unknown_sensor")
        return

    # Calibration runs BEFORE the transaction on purpose. If it fails we
    # dead-letter, and doing that inside the transaction would commit the
    # idempotency mark alongside it — permanently blocking every redelivery of a
    # reading that a calibration row would have fixed.
    if msg.value is None and msg.raw_adc is not None:
        cal = await db.get_sensor_calibration(sensor["id"])
        value = calibrated_value(msg.raw_adc, msg.sensor_type, cal)
    else:
        value = msg.value

    if value is None:
        await db.insert_dead_letter(
            topic, msg.model_dump_json(),
            f"no value and no usable calibration for sensor_type={msg.sensor_type}",
            "uncalibratable")
        return

    # Idempotency mark and insert must commit together: a crash between them
    # would lose the reading forever, since the mark blocks all redeliveries.
    async with db.transaction() as conn:
        if not await db.mark_event_processed(
                msg.reading_id, event_type="sensor_reading", conn=conn):
            return  # duplicate delivery
        await db.insert_reading(sensor["id"], value, msg.raw_adc,
                                msg.recorded_at, conn=conn)

    # Outside the transaction: a slow Telegram call must not hold a write lock.
    await check_thresholds(sensor, msg.sensor_type, value)
    if sensor["plant_id"] is not None:
        await maybe_auto_water(sensor["plant_id"], msg.sensor_type, value)


async def handle_status(msg: StatusMessage) -> None:
    # The last-will offline payload carries no timestamp — the broker fires it
    # whenever it notices the keepalive lapsed, so now() is the honest value.
    last_seen = msg.recorded_at or datetime.now(UTC)
    known = await db.update_device_status(
        msg.device_id, msg.status, last_seen_at=last_seen,
        firmware_version=msg.firmware_version, boot_at=msg.boot_at,
        capabilities=msg.capabilities)
    if not known:
        # Not dead-lettered: status is a retained topic, so this would re-fire on
        # every reconnect and flood the table. Seed the devices row instead.
        logger.warning("status from unregistered device_id=%s — seed a devices row",
                       msg.device_id)


# --- Dispatch ----------------------------------------------------------------

async def dispatch(topic: str, raw) -> None:
    suffix = topic.rsplit("/", 1)[-1]
    model = MODEL_BY_SUFFIX.get(suffix)
    if model is None:
        return  # unreachable given our subscriptions and the broker ACL

    metrics.mqtt_messages_total.labels(topic_suffix=suffix).inc()

    if isinstance(raw, bytes | bytearray):
        # Decode explicitly: json.loads(bytes) raises UnicodeDecodeError, not
        # JSONDecodeError, on invalid UTF-8 — decoding first keeps one error path.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            await db.insert_dead_letter(
                topic, raw.decode("utf-8", errors="replace"), str(e), "json_decode")
            return
    else:
        text = str(raw)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        await db.insert_dead_letter(topic, text, str(e), "json_decode")
        return
    if not isinstance(payload, dict):
        await db.insert_dead_letter(topic, text, "payload is not a JSON object",
                                    "schema_validation")
        return

    if payload.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        await db.insert_dead_letter(
            topic, text, f"schema_version={payload.get('schema_version')}",
            "unsupported_version")
        return

    try:
        msg = model.model_validate(payload)
    except ValidationError as e:
        await db.insert_dead_letter(topic, text, e.json(), "schema_validation")
        return

    # A device may only publish under its own namespace. The ACL enforces this
    # too, but a mismatch here means either a misconfigured device or a payload
    # crafted to write readings against someone else's sensors.
    if str(msg.device_id) != topic.split("/")[1]:
        await db.insert_dead_letter(topic, text, "topic/payload device_id mismatch",
                                    "schema_validation")
        return

    if suffix == "readings":
        _spawn(process_reading(msg))
    elif suffix == "events":
        _spawn(event_handler.process(msg))
    elif suffix == "status":
        _spawn(handle_status(msg))


# --- MQTT session ------------------------------------------------------------

async def session_main() -> None:
    """One MQTT session: connect, subscribe, consume until disconnect."""
    async with MQTTClient(
        os.environ["MQTT_HOST"],
        port=int(os.environ.get("MQTT_PORT", "1883")),
        username=os.environ["MQTT_CONSUMER_USERNAME"],
        password=os.environ["MQTT_CONSUMER_PASSWORD"],
        # Fixed identifier + clean_session=False give a persistent session: the
        # broker queues QoS-1 messages published while the Consumer is down.
        # Note aiomqtt 2.x renamed asyncio-mqtt's `client_id` to `identifier`.
        identifier="consumer_service",
        clean_session=False,
    ) as client:
        for suffix in MODEL_BY_SUFFIX:
            await client.subscribe(f"plant-monitor/+/{suffix}", qos=1)
        logger.info("connected to %s, subscribed to readings/events/status",
                    os.environ["MQTT_HOST"])
        async for message in client.messages:
            if shutdown.is_set():
                break
            await dispatch(str(message.topic), message.payload)


async def run_with_reconnect(coro_factory, *, base: float = 1.0, cap: float = 60.0) -> None:
    """Reconnect with exponential backoff + jitter.

    Without this the process would exit on every broker blip and rely on
    Docker's fixed restart delay — which reconnects all services in lockstep
    after a broker outage.
    """
    delay = base
    while not shutdown.is_set():
        try:
            await coro_factory()
            delay = base   # clean exit (shutdown) — don't pre-load a backoff
        except asyncio.CancelledError:
            # Shutdown path. Explicit so a future `except Exception` can't
            # swallow cancellation and stall the drain.
            raise
        except (MqttError, OSError) as e:
            logger.warning("mqtt disconnect: %s; reconnecting in %.1fs", e, delay)
            await asyncio.sleep(delay + random.uniform(0, delay))
            delay = min(delay * 2, cap)


# --- Entrypoint --------------------------------------------------------------

def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            # Windows dev boxes — the container runs on Linux.
            signal.signal(sig, lambda *_: shutdown.set())


async def main() -> None:
    await db.init_pool(os.environ["DATABASE_URL"])
    await notify.init_session()
    _install_signal_handlers(asyncio.get_running_loop())

    runner = await start_health_server()
    cleanup_task = asyncio.create_task(cleanup.run())
    mqtt_task = asyncio.create_task(run_with_reconnect(session_main))
    logger.info("consumer started")

    try:
        await shutdown.wait()
    finally:
        logger.info("shutting down: draining %d in-flight task(s)", len(inflight))
        mqtt_task.cancel()
        cleanup_task.cancel()
        await asyncio.gather(mqtt_task, cleanup_task, return_exceptions=True)
        # Let started handlers finish committing. Without this, SIGTERM between
        # a reading's idempotency mark and its insert would lose the reading.
        if inflight:
            await asyncio.wait(inflight, timeout=DRAIN_TIMEOUT)
        await runner.cleanup()
        await notify.close_session()
        await db.close_pool()
        logger.info("consumer stopped")


if __name__ == "__main__":
    asyncio.run(main())
