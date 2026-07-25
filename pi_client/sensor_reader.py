#!/usr/bin/env python3
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from uuid import uuid4

from asyncio_mqtt import Client, MqttError, TLSParameters
from sensors.adc import ADCSensor

DEVICE_ID = os.environ["DEVICE_ID"]
SENSOR_CONFIG = os.environ["SENSOR_CONFIG"]
OUTBOX_DB = os.environ.get("PI_OUTBOX_DB", "/home/kwhatever/plant-monitor/pi_client/executed_commands.db")
OUTBOX_MAX_ROWS = 2000   # bound disk usage; older readings drop if broker is down for ~33h at 1/min


def build_sensors(config: str) -> list:
    sensors = []
    for entry in config.split(","):
        parts = entry.strip().split(":")
        if parts[0] == "adc":
            sensors.append(ADCSensor(channel=int(parts[1]), sensor_type=parts[2]))
        elif parts[0] == "gpio" and parts[2] == "dht22":
            # Imported lazily: adafruit_dht/board aren't installed unless a
            # DHT22 is actually configured, so ADC-only setups don't need them.
            from sensors.dht22 import DHT22Sensor
            sensors.append(DHT22Sensor(gpio_pin=int(parts[1])))
    return sensors


def outbox() -> sqlite3.Connection:
    conn = sqlite3.connect(OUTBOX_DB, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    # sensor_reader and watering_controller touch different tables in the same
    # DB. WAL allows concurrent readers/writer, but a concurrent VACUUM or
    # checkpoint can still cause SQLITE_BUSY briefly — wait up to 5s instead
    # of failing fast.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reading_outbox (
            reading_id   TEXT PRIMARY KEY,
            payload      TEXT NOT NULL,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            attempts     INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS reading_outbox_created ON reading_outbox(created_at)")
    return conn


def stage(conn, msg: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO reading_outbox(reading_id, payload) VALUES (?, ?)",
        (msg["reading_id"], json.dumps(msg)))
    # Trim oldest if we exceed bound — prefer recent data over ancient gaps.
    # Keep the N newest rows; delete everything older. Using rowid keeps the
    # query simple and avoids SQLite's LIMIT/OFFSET-in-subquery quirks.
    conn.execute("""
        DELETE FROM reading_outbox
        WHERE rowid NOT IN (
            SELECT rowid FROM reading_outbox
            ORDER BY created_at DESC
            LIMIT ?
        )
    """, (OUTBOX_MAX_ROWS,))


async def publish_drain(client, conn) -> None:
    """Publish every staged reading, oldest first. Delete on broker ack."""
    rows = conn.execute(
        "SELECT reading_id, payload FROM reading_outbox ORDER BY created_at"
    ).fetchall()
    topic = f"plant-monitor/{DEVICE_ID}/readings"
    for reading_id, payload in rows:
        await client.publish(topic, payload, qos=1)   # raises on broker error
        conn.execute("DELETE FROM reading_outbox WHERE reading_id = ?", (reading_id,))


def clock_synchronized() -> bool:
    try:
        out = subprocess.run(["chronyc", "tracking"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    for line in out.splitlines():
        if line.startswith("Leap status"):
            return line.split(":", 1)[1].strip() == "Normal"
    return False


async def main():
    # recorded_at is generated below from the Pi's own clock and drives
    # TimescaleDB continuous aggregates - an unsynchronized clock silently
    # corrupts bucket placement, so refuse to publish rather than stage
    # readings with a wrong timestamp.
    if not clock_synchronized():
        print("Clock not NTP-synchronized (chronyc tracking), skipping this run.")
        sys.exit(1)

    sensors = build_sensors(SENSOR_CONFIG)
    conn = outbox()

    # Stage fresh readings to SQLite BEFORE attempting publish. If MQTT is down,
    # they wait in the outbox; next systemd-timer run will drain them.
    for sensor in sensors:
        try:
            for reading in await sensor.read():
                stage(conn, {
                    "schema_version": 2,
                    "reading_id":  str(uuid4()),
                    "device_id":   DEVICE_ID,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    **reading,
                })
        except Exception as e:
            print(f"Sensor {sensor.address} read error: {e}")

    # sensor_reader.py is one-shot and exits cleanly each run — no last-will, no
    # persistent session needed; the outbox provides offline tolerance instead.
    try:
        tls = TLSParameters(ca_certs=os.environ["MQTT_CA_FILE"])
        async with Client(
            os.environ["MQTT_HOST"],
            port=int(os.environ.get("MQTT_PORT", "8883")),
            username=os.environ["MQTT_USERNAME"],
            password=os.environ["MQTT_PASSWORD"],
            tls_params=tls,
        ) as client:
            await publish_drain(client, conn)
    except MqttError as e:
        print(f"Broker unreachable, readings remain queued: {e}")
    except (OSError, FileNotFoundError) as e:
        # Broker/CA infra (Phase 3) may not exist yet during phased rollout -
        # readings stay safely queued in the outbox either way.
        print(f"MQTT infra not ready, readings remain queued: {e}")


if __name__ == "__main__":
    asyncio.run(main())
