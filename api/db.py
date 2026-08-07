"""Database access for the API.

Same shape as `consumer/db.py` — raw asyncpg, module-level pool, optional `conn`
for statements that must share a transaction. FastAPI serves concurrent requests,
so every call takes its own connection from the pool.
"""
import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

import asyncpg

logger = logging.getLogger("api.db")

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 10,
                    attempts: int = 8, base: float = 1.0, cap: float = 30.0) -> None:
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
    async with pool().acquire() as conn, conn.transaction():
        yield conn


def _x(conn):
    return conn if conn is not None else pool()


async def healthy(*, timeout: float = 3.0) -> bool:
    try:
        async with asyncio.timeout(timeout):
            return await pool().fetchval("SELECT 1") == 1
    except (TimeoutError, OSError, asyncpg.PostgresError, RuntimeError) as e:
        logger.warning("health check failed: %s", e)
        return False


# --- Plants ------------------------------------------------------------------

async def list_plants(*, conn=None) -> list:
    return await _x(conn).fetch("""
        SELECT p.id, p.name, p.name_en, p.location, p.created_at,
               w.mode, w.auto_threshold,
               lw.last_watered_at
        FROM plants p
        LEFT JOIN watering_config w ON w.plant_id = p.id
        LEFT JOIN LATERAL (
            SELECT max(completed_at) AS last_watered_at
            FROM watering_events
            WHERE plant_id = p.id AND status = 'completed'
        ) lw ON true
        ORDER BY p.id
    """)


async def get_plant(plant_id: int, *, conn=None):
    return await _x(conn).fetchrow("""
        SELECT p.id, p.name, p.name_en, p.location, p.created_at,
               w.mode, w.auto_threshold,
               lw.last_watered_at
        FROM plants p
        LEFT JOIN watering_config w ON w.plant_id = p.id
        LEFT JOIN LATERAL (
            SELECT max(completed_at) AS last_watered_at
            FROM watering_events
            WHERE plant_id = p.id AND status = 'completed'
        ) lw ON true
        WHERE p.id = $1
    """, plant_id)


async def create_plant(name: str, name_en: str | None, location: str | None, *, conn=None):
    return await _x(conn).fetchrow("""
        INSERT INTO plants (name, name_en, location)
        VALUES ($1, $2, $3)
        RETURNING id, name, name_en, location, created_at
    """, name, name_en, location)


async def update_plant(plant_id: int, *, name: str | None, name_en: str | None,
                       location: str | None, conn=None):
    # COALESCE leaves unsupplied fields alone. Clearing a field to NULL isn't
    # expressible this way — an acceptable trade for a 3-column table, and the
    # alternative (dynamic SQL) isn't worth the injection surface.
    return await _x(conn).fetchrow("""
        UPDATE plants
        SET name     = COALESCE($2, name),
            name_en  = COALESCE($3, name_en),
            location = COALESCE($4, location)
        WHERE id = $1
        RETURNING id, name, name_en, location, created_at
    """, plant_id, name, name_en, location)


async def plant_exists(plant_id: int, *, conn=None) -> bool:
    return await _x(conn).fetchval("SELECT EXISTS (SELECT 1 FROM plants WHERE id = $1)",
                                   plant_id)


# --- Sensors -----------------------------------------------------------------

async def list_sensor_types(*, conn=None) -> list:
    return await _x(conn).fetch("SELECT id, code, unit, label FROM sensor_types ORDER BY id")


async def latest_readings(plant_id: int | None = None, *, conn=None) -> list:
    """Every sensor with its most recent reading (NULLs if it never reported).

    LEFT JOIN LATERAL rather than DISTINCT ON: this resolves to one index scan
    per sensor against (sensor_id, recorded_at DESC), instead of asking
    TimescaleDB to sort across every chunk of the hypertable.
    """
    return await _x(conn).fetch("""
        SELECT s.id AS sensor_id, s.plant_id, s.address, s.label,
               st.code AS sensor_type, st.unit,
               lr.value, lr.raw_adc, lr.recorded_at
        FROM sensors s
        JOIN sensor_types st ON st.id = s.sensor_type_id
        LEFT JOIN LATERAL (
            SELECT value, raw_adc, recorded_at
            FROM sensor_readings
            WHERE sensor_id = s.id
            ORDER BY recorded_at DESC
            LIMIT 1
        ) lr ON true
        WHERE ($1::int IS NULL OR s.plant_id = $1)
        ORDER BY s.id
    """, plant_id)


async def sensor_exists(sensor_id: int, *, conn=None) -> bool:
    return await _x(conn).fetchval("SELECT EXISTS (SELECT 1 FROM sensors WHERE id = $1)",
                                   sensor_id)


# --- Readings ----------------------------------------------------------------

async def reading_history(plant_id: int, sensor_type: str | None,
                          frm: datetime, to: datetime, limit: int, *, conn=None) -> list:
    return await _x(conn).fetch("""
        SELECT r.recorded_at, r.value, r.raw_adc,
               s.id AS sensor_id, s.address, st.code AS sensor_type
        FROM sensor_readings r
        JOIN sensors s ON s.id = r.sensor_id
        JOIN sensor_types st ON st.id = s.sensor_type_id
        WHERE s.plant_id = $1
          AND ($2::text IS NULL OR st.code = $2)
          AND r.recorded_at >= $3 AND r.recorded_at < $4
        ORDER BY r.recorded_at DESC
        LIMIT $5
    """, plant_id, sensor_type, frm, to, limit)


# Continuous aggregates are separate views with slightly different columns —
# readings_daily has no sample_count. The view name can't be a bind parameter,
# so the two statements are written out and selected from a fixed mapping;
# nothing user-supplied ever reaches the SQL text.
_AGGREGATE_SQL = {
    "1h": """
        SELECT a.bucket, a.avg_value, a.min_value, a.max_value, a.sample_count,
               s.id AS sensor_id, s.address, st.code AS sensor_type
        FROM readings_hourly a
        JOIN sensors s ON s.id = a.sensor_id
        JOIN sensor_types st ON st.id = s.sensor_type_id
        WHERE s.plant_id = $1
          AND ($2::text IS NULL OR st.code = $2)
          AND a.bucket >= $3 AND a.bucket < $4
        ORDER BY a.bucket DESC
        LIMIT $5
    """,
    "1d": """
        SELECT a.bucket, a.avg_value, a.min_value, a.max_value,
               NULL::bigint AS sample_count,
               s.id AS sensor_id, s.address, st.code AS sensor_type
        FROM readings_daily a
        JOIN sensors s ON s.id = a.sensor_id
        JOIN sensor_types st ON st.id = s.sensor_type_id
        WHERE s.plant_id = $1
          AND ($2::text IS NULL OR st.code = $2)
          AND a.bucket >= $3 AND a.bucket < $4
        ORDER BY a.bucket DESC
        LIMIT $5
    """,
}


async def reading_aggregated(plant_id: int, sensor_type: str | None, bucket: str,
                             frm: datetime, to: datetime, limit: int, *, conn=None) -> list:
    return await _x(conn).fetch(_AGGREGATE_SQL[bucket], plant_id, sensor_type, frm, to, limit)


# --- Watering ----------------------------------------------------------------

async def get_watering_config(plant_id: int, *, conn=None):
    return await _x(conn).fetchrow("""
        SELECT plant_id, device_id, mode, auto_threshold, duration_sec,
               relay_channel, min_interval_min, updated_at
        FROM watering_config WHERE plant_id = $1
    """, plant_id)


async def put_watering_config(*, plant_id: int, device_id: UUID, mode: str,
                              auto_threshold: float, duration_sec: int,
                              relay_channel: int, min_interval_min: int, conn=None):
    return await _x(conn).fetchrow("""
        INSERT INTO watering_config
            (plant_id, device_id, mode, auto_threshold, duration_sec,
             relay_channel, min_interval_min)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (plant_id) DO UPDATE SET
            device_id        = EXCLUDED.device_id,
            mode             = EXCLUDED.mode,
            auto_threshold   = EXCLUDED.auto_threshold,
            duration_sec     = EXCLUDED.duration_sec,
            relay_channel    = EXCLUDED.relay_channel,
            min_interval_min = EXCLUDED.min_interval_min,
            updated_at       = now()
        RETURNING plant_id, device_id, mode, auto_threshold, duration_sec,
                  relay_channel, min_interval_min, updated_at
    """, plant_id, device_id, mode, auto_threshold, duration_sec,
         relay_channel, min_interval_min)


async def patch_watering_config(plant_id: int, *, mode: str | None,
                                auto_threshold: float | None, duration_sec: int | None,
                                min_interval_min: int | None, conn=None):
    # relay_channel and device_id are deliberately not patchable: they describe
    # which solenoid is physically plumbed to this plant, not a preference.
    # Rewiring goes through PUT, which states the whole mapping at once.
    return await _x(conn).fetchrow("""
        UPDATE watering_config
        SET mode             = COALESCE($2, mode),
            auto_threshold   = COALESCE($3, auto_threshold),
            duration_sec     = COALESCE($4, duration_sec),
            min_interval_min = COALESCE($5, min_interval_min),
            updated_at       = now()
        WHERE plant_id = $1
        RETURNING plant_id, device_id, mode, auto_threshold, duration_sec,
                  relay_channel, min_interval_min, updated_at
    """, plant_id, mode, auto_threshold, duration_sec, min_interval_min)


async def has_active_watering_command(plant_id: int, *, conn=None) -> bool:
    return await _x(conn).fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM watering_events
            WHERE plant_id = $1 AND status IN ('pending', 'dispatched')
        )
    """, plant_id)


async def try_create_pending_watering_command(*, plant_id: int, device_id: UUID,
                                              command_id: UUID, triggered_by: str,
                                              duration_sec: int, relay_channel: int,
                                              expires_at: datetime) -> bool:
    """Create the watering_events + command_outbox rows in one transaction.

    IMPORTANT: this is deliberately identical to
    `consumer/db.try_create_pending_watering_command`. Manual watering from here
    and auto-watering from the Consumer are two writers racing for the same
    relay, and they only serialize correctly if both take the same plant-row
    lock and run the same active-command check. `api/tests/test_command_sql.py`
    fails if the two implementations drift apart.
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
            return False
        if await has_active_watering_command(plant_id, conn=conn):
            return False

        await conn.execute("""
            INSERT INTO watering_events
                (plant_id, device_id, command_id, triggered_by, duration_sec,
                 status, expires_at)
            VALUES ($1, $2, $3, $4, $5, 'pending', $6)
        """, plant_id, device_id, command_id, triggered_by, duration_sec, expires_at)

        await conn.execute("""
            INSERT INTO command_outbox (command_id, device_id, topic, payload)
            VALUES ($1, $2, $3, $4::jsonb)
        """, command_id, device_id, topic, json.dumps(payload))
    return True


async def get_command(command_id: UUID, *, conn=None):
    return await _x(conn).fetchrow("""
        SELECT command_id, plant_id, device_id, triggered_by, duration_sec, status,
               requested_at, expires_at, started_at, completed_at
        FROM watering_events WHERE command_id = $1
    """, command_id)


# --- Alert config ------------------------------------------------------------

async def get_alert_configs(sensor_id: int, *, conn=None) -> list:
    return await _x(conn).fetch("""
        SELECT id, sensor_id, alert_type, threshold, cooldown_min
        FROM sensor_alert_config WHERE sensor_id = $1 ORDER BY alert_type
    """, sensor_id)


async def upsert_alert_config(sensor_id: int, alert_type: str, threshold: float,
                              cooldown_min: int, *, conn=None):
    return await _x(conn).fetchrow("""
        INSERT INTO sensor_alert_config (sensor_id, alert_type, threshold, cooldown_min)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (sensor_id, alert_type) DO UPDATE SET
            threshold    = EXCLUDED.threshold,
            cooldown_min = EXCLUDED.cooldown_min
        RETURNING id, sensor_id, alert_type, threshold, cooldown_min
    """, sensor_id, alert_type, threshold, cooldown_min)


# --- Calibration -------------------------------------------------------------

async def get_calibration(sensor_id: int, *, conn=None):
    return await _x(conn).fetchrow("""
        SELECT sensor_id, raw_dry, raw_wet, calibrated_at, notes
        FROM sensor_calibration WHERE sensor_id = $1
    """, sensor_id)


async def put_calibration(sensor_id: int, raw_dry: int, raw_wet: int,
                          notes: str | None, *, conn=None):
    return await _x(conn).fetchrow("""
        INSERT INTO sensor_calibration (sensor_id, raw_dry, raw_wet, notes)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (sensor_id) DO UPDATE SET
            raw_dry       = EXCLUDED.raw_dry,
            raw_wet       = EXCLUDED.raw_wet,
            notes         = EXCLUDED.notes,
            calibrated_at = now()
        RETURNING sensor_id, raw_dry, raw_wet, calibrated_at, notes
    """, sensor_id, raw_dry, raw_wet, notes)


# --- Alerts ------------------------------------------------------------------

async def list_alerts(*, unacked: bool, limit: int, conn=None) -> list:
    return await _x(conn).fetch("""
        SELECT a.id, a.plant_id, a.sensor_id, a.alert_type, a.value,
               a.sent_at, a.acknowledged, s.label AS sensor_label
        FROM alerts a
        LEFT JOIN sensors s ON s.id = a.sensor_id
        WHERE ($1::bool IS false OR a.acknowledged = false)
        ORDER BY a.sent_at DESC
        LIMIT $2
    """, unacked, limit)


async def ack_alert(alert_id: int, *, conn=None):
    return await _x(conn).fetchrow("""
        UPDATE alerts SET acknowledged = true
        WHERE id = $1
        RETURNING id, plant_id, sensor_id, alert_type, value, sent_at, acknowledged
    """, alert_id)
