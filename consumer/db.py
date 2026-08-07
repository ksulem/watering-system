"""Database access for the Consumer.

Raw asyncpg rather than SQLAlchemy: the query set is small and hand-written,
and skipping the ORM saves ~40 MB of resident memory on a 1 GB VM that also
runs TimescaleDB, Mosquitto and (soon) the API and bot.

Every function acquires from the module-level pool, NOT a shared connection —
concurrent `_spawn`ed tasks on one connection would serialize and deadlock
under a broker replay burst. Functions that must participate in a caller's
transaction take an optional `conn`.
"""
import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

import asyncpg

from consumer import metrics

logger = logging.getLogger("consumer.db")

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 10,
                    attempts: int = 8, base: float = 1.0, cap: float = 30.0) -> None:
    """Create the pool with bounded exponential backoff.

    Even with compose's depends_on:service_healthy, TCP-readiness and
    auth-readiness can lag, and DNS blips inside the container can fail the very
    first connect. Bounded (unlike cmd_dispatcher's infinite retry) so a genuine
    misconfiguration surfaces as a crash-loop instead of a silent hang.
    """
    global _pool
    delay = base
    for n in range(1, attempts + 1):
        try:
            _pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
            return
        except (OSError, asyncpg.PostgresError) as e:
            if n == attempts:
                raise
            logger.warning("DB pool init failed (attempt %d/%d): %s; retrying in %.1fs",
                           n, attempts, e, delay)
            await asyncio.sleep(delay + random.uniform(0, delay))
            delay = min(delay * 2, cap)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db.init_pool() has not been called")
    return _pool


@asynccontextmanager
async def transaction():
    """Acquire a connection and open a transaction on it.

    Pass the yielded connection to the helpers below so their statements commit
    or roll back together.
    """
    async with pool().acquire() as conn, conn.transaction():
        yield conn


def _x(conn):
    """Return the executor to use — the caller's connection, or the pool.

    asyncpg.Pool proxies execute/fetch/fetchrow/fetchval by acquiring a
    connection internally, so both sides of this satisfy the same interface.
    """
    return conn if conn is not None else pool()


async def execute(sql: str, *args, conn=None) -> str:
    return await _x(conn).execute(sql, *args)


# --- Sensors -----------------------------------------------------------------

async def get_sensor_by_device_address(device_id: UUID, address: str, sensor_type: str,
                                       *, conn=None):
    """Resolve (device, address, sensor_type) -> sensors row, or None.

    sensor_type arrives as a code string ('moisture'); `sensors` stores a
    sensor_type_id FK, hence the join.
    """
    # address is selected even though the caller already knows it: sensor_health
    # labels its metrics with it, and asyncpg Records raise KeyError rather than
    # returning None for a column that wasn't selected.
    return await _x(conn).fetchrow("""
        SELECT s.id, s.plant_id, s.label, s.address
        FROM sensors s
        JOIN sensor_types st ON st.id = s.sensor_type_id
        WHERE s.device_id = $1 AND s.address = $2 AND st.code = $3
    """, device_id, address, sensor_type)


async def get_sensor_calibration(sensor_id: int, *, conn=None):
    return await _x(conn).fetchrow("""
        SELECT raw_dry, raw_wet FROM sensor_calibration WHERE sensor_id = $1
    """, sensor_id)


async def insert_reading(sensor_id: int, value: float, raw_adc: int | None,
                         recorded_at: datetime, *, conn=None) -> None:
    with metrics.db_insert_seconds.time():
        await _x(conn).execute("""
            INSERT INTO sensor_readings (recorded_at, sensor_id, value, raw_adc)
            VALUES ($1, $2, $3, $4)
        """, recorded_at, sensor_id, value, raw_adc)


# --- Idempotency -------------------------------------------------------------

async def mark_event_processed(event_id: UUID, event_type: str,
                               command_id: UUID | None = None, *, conn=None) -> bool:
    """Claim an event_id. True if this delivery is the first, False if duplicate.

    Used for both sensor `reading_id`s and watering `event_id`s — QoS 1 and the
    Pi's own outbox can both redeliver.
    """
    row = await _x(conn).fetchval("""
        INSERT INTO processed_sensor_events (event_id, command_id, event_type)
        VALUES ($1, $2, $3)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
    """, event_id, command_id, event_type)
    return row is not None


# --- Alerts ------------------------------------------------------------------

async def get_alert_configs(sensor_id: int, *, conn=None) -> list:
    return await _x(conn).fetch("""
        SELECT alert_type, threshold, cooldown_min
        FROM sensor_alert_config WHERE sensor_id = $1
    """, sensor_id)


async def recent_alert_exists(sensor_id: int, alert_type: str, minutes: int,
                              *, conn=None) -> bool:
    return await _x(conn).fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM alerts
            WHERE sensor_id = $1 AND alert_type = $2
              AND sent_at > now() - make_interval(mins => $3)
        )
    """, sensor_id, alert_type, minutes)


async def create_alert(sensor_id: int, alert_type: str, value: float,
                       plant_id: int | None = None, *, conn=None) -> None:
    await _x(conn).execute("""
        INSERT INTO alerts (plant_id, sensor_id, alert_type, value)
        VALUES ($1, $2, $3, $4)
    """, plant_id, sensor_id, alert_type, value)


# --- Watering ----------------------------------------------------------------

async def get_watering_config(plant_id: int, *, conn=None):
    return await _x(conn).fetchrow("""
        SELECT plant_id, device_id, mode, auto_threshold, duration_sec,
               relay_channel, min_interval_min
        FROM watering_config WHERE plant_id = $1
    """, plant_id)


async def has_active_watering_command(plant_id: int, *, conn=None) -> bool:
    return await _x(conn).fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM watering_events
            WHERE plant_id = $1 AND status IN ('pending', 'dispatched')
        )
    """, plant_id)


async def watered_within(plant_id: int, minutes: int, *, conn=None) -> bool:
    """True if this plant completed a watering inside the cooldown window."""
    return await _x(conn).fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM watering_events
            WHERE plant_id = $1 AND status = 'completed'
              AND completed_at > now() - make_interval(mins => $2)
        )
    """, plant_id, minutes)


async def try_create_pending_watering_command(*, plant_id: int, device_id: UUID,
                                              command_id: UUID, triggered_by: str,
                                              duration_sec: int, relay_channel: int,
                                              expires_at: datetime) -> bool:
    """Create the watering_events + command_outbox rows in one transaction.

    Either both rows exist or neither does — there is no separate enqueue step
    and no compensation path. `SELECT ... FOR UPDATE` on the plant row is the
    concurrency guard: two tasks acting on the same dry-soil reading serialize
    here, and the second sees the first's pending row and returns False.
    """
    topic = f"plant-monitor/{device_id}/commands"
    payload = {
        "schema_version": 2,
        "command_id": str(command_id),
        "command": "water_now",
        "device_id": str(device_id),
        "plant_id": plant_id,
        "relay_channel": relay_channel,
        "duration_sec": duration_sec,
        "expires_at": expires_at.isoformat(),
    }
    async with transaction() as conn:
        locked = await conn.fetchval(
            "SELECT id FROM plants WHERE id = $1 FOR UPDATE", plant_id)
        if locked is None:
            logger.warning("auto-water for unknown plant_id=%s", plant_id)
            return False
        if await has_active_watering_command(plant_id, conn=conn):
            return False

        await conn.execute("""
            INSERT INTO watering_events
                (plant_id, device_id, command_id, triggered_by, duration_sec,
                 status, expires_at)
            VALUES ($1, $2, $3, $4, $5, 'pending', $6)
        """, plant_id, device_id, command_id, triggered_by, duration_sec, expires_at)

        # ::jsonb cast because asyncpg has no implicit dict->jsonb codec; the
        # dispatcher reads this column back out as a JSON string and publishes
        # it verbatim.
        await conn.execute("""
            INSERT INTO command_outbox (command_id, device_id, topic, payload)
            VALUES ($1, $2, $3, $4::jsonb)
        """, command_id, device_id, topic, json.dumps(payload))
    return True


async def update_watering_event(command_id: UUID, status: str, *,
                                started_at: datetime | None = None,
                                completed_at: datetime | None = None,
                                conn=None) -> bool:
    """Guarded lifecycle update. True only when a row actually changed.

    The WHERE clause makes terminal states absorbing: a duplicate
    watering_completed, or a cleanup-task expiry racing a real completion,
    cannot overwrite the first writer's outcome.
    """
    row = await _x(conn).fetchval("""
        UPDATE watering_events
        SET status       = $2,
            started_at   = COALESCE($3, started_at),
            completed_at = COALESCE($4, completed_at)
        WHERE command_id = $1 AND status IN ('pending', 'dispatched')
        RETURNING id
    """, command_id, status, started_at, completed_at)
    return row is not None


async def count_pending_outbox(*, conn=None) -> int:
    return await _x(conn).fetchval(
        "SELECT count(*) FROM command_outbox WHERE status = 'pending'")


# --- Devices -----------------------------------------------------------------

async def update_device_status(device_id: UUID, status: str, *,
                               last_seen_at: datetime,
                               firmware_version: str | None = None,
                               boot_at: datetime | None = None,
                               capabilities: dict | None = None,
                               conn=None) -> bool:
    """Update device presence. False if the device has no row yet.

    COALESCE keeps previously-known firmware/boot/capabilities values: the
    last-will `offline` payload carries none of them, and blanking the columns
    on every disconnect would lose the data until the next reconnect.
    """
    row = await _x(conn).fetchval("""
        UPDATE devices
        SET status           = $2,
            last_seen_at     = $3,
            firmware_version = COALESCE($4, firmware_version),
            last_boot_at     = COALESCE($5, last_boot_at),
            capabilities     = COALESCE($6::jsonb, capabilities)
        WHERE id = $1
        RETURNING id
    """, device_id, status, last_seen_at, firmware_version, boot_at,
         json.dumps(capabilities) if capabilities is not None else None)
    return row is not None


# --- Dead letters ------------------------------------------------------------

async def insert_dead_letter(topic: str, raw_payload: str, error: str,
                             error_kind: str, *, conn=None) -> None:
    """Record a message the Consumer refused to process.

    The metric is bumped here rather than at each call site so every dead letter
    is counted exactly once.
    """
    metrics.mqtt_dead_letters_total.labels(error_kind=error_kind).inc()
    logger.warning("dead letter topic=%s kind=%s error=%s", topic, error_kind, error[:300])
    await _x(conn).execute("""
        INSERT INTO dead_letters (topic, raw_payload, error, error_kind)
        VALUES ($1, $2, $3, $4)
    """, topic, raw_payload, error, error_kind)


async def healthy(*, timeout: float = 3.0) -> bool:
    """Cheap liveness probe for the /health endpoint."""
    try:
        async with asyncio.timeout(timeout):
            return await pool().fetchval("SELECT 1") == 1
    except (TimeoutError, OSError, asyncpg.PostgresError, RuntimeError) as e:
        logger.warning("health check failed: %s", e)
        return False
