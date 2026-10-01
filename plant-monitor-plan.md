# Plant Monitor — Implementation Plan

## Project Overview

IoT system for monitoring and watering up to 8 plants via Raspberry Pi, with a Telegram Mini App as the user interface.

### Key Design Decisions
- **Pi is an edge device only** — no public IP, no server role; executes commands, no threshold or mode awareness
- **MQTT at the device edge** — Pi uses MQTT over TLS for sensor readings, device status, commands, and acknowledgements
- **Consumer subscribes directly to MQTT** — no intermediate message bus; TimescaleDB is the durable event log
- **Command Dispatcher as separate thin service** — polls `command_outbox`, publishes commands back to MQTT; independent restart from Consumer
- **Auto-watering owned by Consumer** — Consumer checks thresholds against DB config and enqueues `water_now`; Pi is purely reactive
- **Command acknowledgement pattern** — every watering command has a lifecycle: pending → dispatched → completed | failed | expired
- **Device registry and per-device identity** — every sensor reading and command is scoped by `device_id`
- **Extensible sensor addressing** — `address VARCHAR(50)` encodes both ADC channels (`adc:0`–`adc:7`) and GPIO sensors (`gpio:4:temp`, `gpio:4:hum`); new sensor types require no schema migrations
- **DB-driven alert thresholds** — `sensor_alert_config` table; Consumer reads thresholds per sensor, no hardcoded values
- **Output devices are Pi-local** — RGB LED, buzzer, and LCD respond to Pi's own operational state; no backend commands needed
- **TimescaleDB** — primary storage, optimized for time-series data
- **All backend in Python** — FastAPI + aiogram + asyncio-mqtt/paho-mqtt
- **Consumer notifies Bot directly** — Consumer calls Telegram Bot API over HTTP; no shared process needed
- **ML added later** — after 2–4 weeks of data accumulation
- **Redpanda when needed** — add Redpanda when you need replayable event streams, multiple independent consumers, or ingest isolation from a slow Consumer; device count alone is not the trigger

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     Raspberry Pi                          │
│  ADC sensors → MCP3008 (CH0–CH7)                        │
│  GPIO sensors: DHT22 (temp+hum), DS3231 RTC              │
│  outputs (Pi-local only): RGB LED, buzzer, LCD           │
│                                                           │
│  sensor_reader.py  (systemd timer, 60s)                  │
│  watering_controller.py  (GPIO relay + MQTT commands)    │
│         │                              ▲                  │
│         │ MQTT over TLS (readings,     │ commands         │
│         │ events, status)              │                  │
└─────────┼──────────────────────────────┼──────────────────┘
          │                              │
          ▼                              │
┌──────────────────────────────────────────────────────────┐
│              Oracle Cloud VPS (free tier)                 │
│                                                           │
│  ┌──────────────────────────────────────────────────┐    │
│  │  Mosquitto (MQTT broker)                         │    │
│  │  :8883 TLS  — Pi devices (external)              │    │
│  │  :1883 plain — Docker services (internal only)   │    │
│  └────────┬──────────────────────────────┬──────────┘    │
│           │ subscribe readings/events/    │ publish cmds  │
│           │ status (QoS 1, persistent)   │               │
│           ▼                              │               │
│  ┌──────────────────────┐   ┌────────────┴────────────┐  │
│  │  Consumer            │   │  Command Dispatcher     │  │
│  │  • validates topics  │   │  • polls command_outbox │  │
│  │  • inserts readings  │   │    every 2s             │  │
│  │  • threshold alerts  │   │  • publishes to MQTT    │  │
│  │  • auto-watering     │   │  • marks dispatched     │  │
│  │  • event handling    │   │  • retry + expiry       │  │
│  │  • device status     │   └────────────┬────────────┘  │
│  │  • stale cmd expiry  │                │               │
│  └──────────┬───────────┘                │               │
│             └──────────────┬─────────────┘               │
│                            │                              │
│                 ┌──────────▼──────────┐                  │
│                 │    TimescaleDB      │                  │
│                 │  readings, commands │                  │
│                 │  outbox, alerts     │                  │
│                 └──────────┬──────────┘                  │
│                            │                              │
│           ┌────────────────┼────────────────┐            │
│           ▼                ▼                ▼            │
│  ┌──────────────┐  ┌────────────┐  ┌─────────────────┐  │
│  │  FastAPI     │  │    Bot     │  │  Nginx + Mini   │  │
│  │  (port 8000) │  │  (8001)   │  │  App (443/80)   │  │
│  └──────────────┘  └─────┬──────┘  └─────────────────┘  │
│                          │ webhook                        │
│                    api.telegram.org                       │
└──────────────────────────────────────────────────────────┘
```

**Services: 7** — TimescaleDB, Mosquitto, Consumer, Command Dispatcher, FastAPI, Bot, Nginx.  
**Estimated RAM: ~1.5–2 GB peak** (leaves ~4 GB headroom on free-tier 6 GB VM).

---

### Messaging Layers

```
Raspberry Pi
  MQTT client over TLS
  publishes readings / events / status
  subscribes to commands
        |
        v
Mosquitto broker
  device sessions, topic ACLs (per-device namespace)
  QoS 1, retained status, last-will offline detection
        |
        +──────────────────────────────────────+
        │                                      │
        v                                      │
Consumer service                               │ (command publish)
  validates topic/device identity              │
  normalizes and persists readings             │
  threshold checks, auto-watering             │
  handles watering ack events                  │
  updates device status                        │
        |                                      │
        v                              Command Dispatcher
TimescaleDB                              polls command_outbox
  durable store for all events             publishes to MQTT
  command_outbox as reliable dispatch      marks dispatched
  outbox replaces need for Kafka
```

MQTT is the device protocol. TimescaleDB is the backend event log. The Pi never connects directly to the DB.

---

## Degradation Behavior (Network Partitions)

How the system behaves when one component is unreachable. The outbox patterns are designed so that **no input is silently lost during partitions** — recovery happens automatically when connectivity returns.

| Component down | What still works | What stops | Recovery on restore |
|---|---|---|---|
| Pi loses internet | LED/buzzer/LCD still reflect Pi state; relay can still fire if a queued command arrives via local SQLite | No new readings reach Consumer; no new commands reach Pi | Pi's `reading_outbox` drains (up to 2000 readings ≈ 33h backlog); broker's persistent queue delivers any queued `water_now` commands |
| Mosquitto down | Pi continues reading and staging in `reading_outbox`; Consumer continues running (just sees no messages); Dispatcher accumulates `pending` rows in `command_outbox` | All MQTT traffic | Readings drain from Pi outbox at next timer tick; Dispatcher publishes accumulated commands on next poll |
| Consumer down | Pi readings/events queue in Mosquitto persistent session (clean_session=False, broker-side `max_queued_messages`); Dispatcher continues publishing commands | No new readings persisted, no auto-water decisions, no Telegram alerts | Consumer reconnects with same `client_id`, broker replays queued messages; idempotency table prevents double-inserts |
| Dispatcher down | Consumer continues persisting readings; Pi continues executing already-received commands | No new commands reach Pi; `command_outbox` accumulates `pending` rows | Dispatcher resumes polling on restart; `FOR UPDATE SKIP LOCKED` handles overlap if a stale instance is still running |
| TimescaleDB down | Pi continues staging readings in its outbox; Mosquitto buffers Consumer-bound traffic | Consumer health-fails; Dispatcher health-fails; API returns 503 | Services reconnect via asyncpg pool retry; queued MQTT traffic replays on Consumer reconnect |
| DNS for `yourdomain.com` fails | Internal Docker DNS still resolves `mosquitto`, `timescaledb` — all backend traffic survives | Pi can't resolve `MQTT_HOST`; Telegram callbacks can't reach `/webhook` | Falls back to cached DNS on most resolvers; otherwise treated as "Pi loses internet" |
| Telegram API unreachable | Everything except outbound notifications and the Mini App auth flow | Push alerts queue (and are lost — no notification outbox) | Restored automatically; missed alerts are NOT replayed by design (would spam after long outages) |

**Bounds and caveats.**
- Pi `reading_outbox` is capped at 2000 rows. Beyond that, oldest readings are dropped to preserve recent data.
- Mosquitto `persistence` is on, and Consumer uses `clean_session=False`, but the broker's `max_queued_messages` (default 1000) caps queued messages per client. For a multi-day Consumer outage, beyond-cap messages are lost; readings are still safe in Pi's outbox, but device-status transitions are best-effort.
- `processed_sensor_events` 30-day retention bounds the redelivery window — a reading more than 30 days old can no longer be deduped if the broker somehow redelivers it. The Pi outbox cap makes this scenario unreachable in practice.
- **QoS 1 acknowledges delivery, not processing.** asyncio-mqtt/paho send PUBACK when a message is handed to the consumer loop — not when the spawned handler's DB transaction commits. A hard Consumer crash in that window (milliseconds per message) loses the message despite the persistent session; the Pi `reading_outbox` will not resend it, because it clears on broker ack. Expected loss at ~11 msg/min is a few readings per crash — accepted. For watering events, stale-command expiry still resolves the lifecycle, though with status `expired` rather than the true outcome.

---

## Repository Structure

```
plant-monitor/
├── pi_client/
│   ├── sensors/
│   │   ├── __init__.py
│   │   ├── base.py               # Sensor ABC — address, sensor_type, read() → list[dict]
│   │   ├── adc.py                # ADCSensor: reads MCP3008, returns one reading
│   │   └── dht22.py              # DHT22Sensor: reads GPIO, returns two readings (temp + hum)
│   ├── outputs/
│   │   ├── led.py                # RGB LED — connection / watering status (Pi-local)
│   │   ├── buzzer.py             # Active buzzer — audible feedback on relay events (Pi-local)
│   │   └── lcd.py                # LCD display — last reading + device status (optional)
│   ├── sensor_reader.py          # sensor-list driven; reads all sensors, publishes to MQTT
│   ├── watering_controller.py    # GPIO relay + MQTT command subscriber + output device control
│   ├── mqtt_client.py            # shared TLS MQTT connection, last-will config, helpers
│   ├── executed_commands.db      # local SQLite — idempotency + terminal event cache
│   └── ca.crt                    # CA certificate for MQTT broker TLS
│
├── cmd_dispatcher/
│   ├── Dockerfile
│   └── main.py                   # polls command_outbox → publishes to plant-monitor/{id}/commands
│
├── consumer/
│   ├── Dockerfile
│   ├── main.py                   # asyncio-mqtt subscriber loop (readings + events + status)
│   ├── db.py                     # SQLAlchemy async models + queries
│   ├── alerts.py                 # DB-driven threshold checks → Telegram notifications
│   ├── auto_water.py             # auto mode: check thresholds → enqueue water_now
│   ├── event_handler.py          # handles watering ack events from Pi
│   └── cleanup.py                # background task: expire stale pending commands
│
├── api/
│   ├── Dockerfile
│   ├── main.py                   # FastAPI app + CORS middleware
│   ├── auth.py                   # Telegram initData HMAC validation + session tokens (PyJWT)
│   ├── dependencies.py           # shared DB session
│   └── routers/
│       ├── readings.py           # GET readings, history, aggregated
│       ├── plants.py             # CRUD plants
│       ├── sensors.py            # sensor types, config, alert thresholds
│       ├── watering.py           # POST water, GET command status
│       └── alerts.py             # GET alerts, POST ack
│
├── bot/
│   ├── Dockerfile
│   ├── main.py                   # aiogram app + webhook setup + /health endpoint
│   └── handlers/
│       ├── status.py             # /status command
│       ├── water.py              # /water command
│       └── alerts.py             # push notifications
│
├── mini-app/
│   └── src/
│       ├── pages/
│       │   ├── Dashboard.tsx     # all plants overview (moisture + other sensor badges)
│       │   ├── PlantDetail.tsx   # single plant + charts + watering
│       │   └── Settings.tsx      # thresholds, plant names, alert config
│       ├── components/
│       │   ├── SensorCard.tsx    # generic sensor reading card (replaces MoistureCard)
│       │   ├── SensorChart.tsx   # Recharts line chart, any sensor type
│       │   ├── WateringButton.tsx
│       │   ├── ModeToggle.tsx    # AUTO / MANUAL switch
│       │   └── CommandStatus.tsx # pending → completed indicator
│       └── api/
│           └── client.ts         # typed fetch client for FastAPI
│
├── mosquitto/
│   ├── mosquitto.conf            # listener config (TLS :8883 + plain :1883)
│   ├── passwd                    # hashed device + backend credentials (mosquitto_passwd)
│   └── acl                       # per-device topic ACLs
│
├── docker-compose.yml
├── nginx.conf
└── .env.example
```

---

## Database Schema (TimescaleDB)

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Sensor types (extensible — add new types without migrations)
CREATE TABLE sensor_types (
    id      INT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    code    VARCHAR(50) NOT NULL UNIQUE,
    unit    VARCHAR(20) NOT NULL,
    label   VARCHAR(100)
);

-- Plants
CREATE TABLE plants (
    id          INT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    name        VARCHAR(100) NOT NULL,
    location    VARCHAR(100),
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Edge devices / controllers
CREATE TABLE devices (
    id               UUID PRIMARY KEY,
    code             VARCHAR(100) NOT NULL UNIQUE,
    label            VARCHAR(100),
    status           VARCHAR(20) NOT NULL DEFAULT 'offline',
    last_seen_at     TIMESTAMPTZ,
    -- Populated from the status message published by watering_controller on connect.
    -- Storing firmware_version here avoids a schema migration when adding new Pi
    -- capabilities; capabilities JSONB lets the Consumer make feature decisions
    -- (e.g. "does this device support tank sensors?") without code changes.
    firmware_version VARCHAR(50),
    last_boot_at     TIMESTAMPTZ,
    capabilities     JSONB NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT device_status_valid CHECK (status IN ('online', 'offline', 'degraded'))
);

-- Physical sensors on a device
-- address encodes sensor location:
--   ADC sensors:  'adc:0' .. 'adc:7'   (MCP3008 channel)
--   GPIO sensors: 'gpio:4:temp'         (DHT22 temperature on GPIO4)
--                 'gpio:4:hum'          (DHT22 humidity on GPIO4)
--   Future:       'i2c:0x77:pressure'  (BMP280 on I2C bus 1)
CREATE TABLE sensors (
    id              INT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    device_id       UUID NOT NULL REFERENCES devices(id),
    sensor_type_id  INT NOT NULL REFERENCES sensor_types(id),
    plant_id        INT REFERENCES plants(id),   -- NULL if not plant-specific (e.g. tank, light)
    address         VARCHAR(50) NOT NULL,
    label           VARCHAR(100),
    CONSTRAINT sensors_device_address_type_unique UNIQUE (device_id, address, sensor_type_id)
);

-- Per-sensor calibration. Capacitive moisture sensors vary unit-to-unit
-- (dry-air raw ≈ 600–800, fully-submerged raw ≈ 280–350) and drift over months.
-- Applied by the Consumer when a reading arrives: Pi sends raw_adc only, the
-- Consumer joins this table by sensor_id and computes value = linear map from
-- (raw_dry, raw_wet) to 0–100%. Missing row → fall back to sensor-type defaults
-- in consumer code. This keeps Pi a pure transducer (no DB / API dependency).
CREATE TABLE sensor_calibration (
    sensor_id     INT PRIMARY KEY REFERENCES sensors(id) ON DELETE CASCADE,
    raw_dry       INT NOT NULL,           -- raw ADC reading at 0% (sensor in dry air)
    raw_wet       INT NOT NULL,           -- raw ADC reading at 100% (sensor in water)
    calibrated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes         TEXT,
    CONSTRAINT sensor_calibration_distinct CHECK (raw_dry <> raw_wet)
);

-- Per-sensor alert thresholds (all sensor types, including moisture)
-- watering_config.auto_threshold triggers watering; this table triggers notifications
CREATE TABLE sensor_alert_config (
    id              INT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    sensor_id       INT NOT NULL REFERENCES sensors(id),
    alert_type      VARCHAR(10) NOT NULL,     -- 'too_low' | 'too_high'
    threshold       FLOAT NOT NULL,
    cooldown_min    INT NOT NULL DEFAULT 60,
    CONSTRAINT sensor_alert_type_valid CHECK (alert_type IN ('too_low', 'too_high')),
    CONSTRAINT sensor_alert_unique UNIQUE (sensor_id, alert_type)
);

-- All sensor readings (hypertable, partitioned by time)
CREATE TABLE sensor_readings (
    recorded_at  TIMESTAMPTZ NOT NULL,
    sensor_id    INT NOT NULL REFERENCES sensors(id),
    value        FLOAT NOT NULL,
    raw_adc      INT,                          -- NULL for non-ADC sensors (DHT22, etc.)
    ingested_at  TIMESTAMPTZ DEFAULT now()
);

SELECT create_hypertable('sensor_readings', 'recorded_at');
CREATE INDEX ON sensor_readings (sensor_id, recorded_at DESC);

ALTER TABLE sensor_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id',
    timescaledb.compress_orderby   = 'recorded_at DESC'
    -- segmentby + orderby together let time-range queries on compressed chunks
    -- skip decompression of unrelated row groups. Without orderby, TimescaleDB
    -- falls back to full chunk decompression for any range scan.
);
SELECT add_compression_policy('sensor_readings', INTERVAL '30 days');
SELECT add_retention_policy('sensor_readings', INTERVAL '12 months');

-- Continuous aggregates for dashboards
CREATE MATERIALIZED VIEW readings_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', recorded_at) AS bucket,
    sensor_id,
    avg(value)  AS avg_value,
    min(value)  AS min_value,
    max(value)  AS max_value,
    count(*)    AS sample_count
FROM sensor_readings
GROUP BY bucket, sensor_id;

SELECT add_continuous_aggregate_policy('readings_hourly',
    start_offset => INTERVAL '3 hours',
    end_offset   => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes');

CREATE MATERIALIZED VIEW readings_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', recorded_at) AS bucket,
    sensor_id,
    avg(value)  AS avg_value,
    min(value)  AS min_value,
    max(value)  AS max_value
FROM sensor_readings
GROUP BY bucket, sensor_id;

SELECT add_continuous_aggregate_policy('readings_daily',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');

-- Watering config per plant
CREATE TABLE watering_config (
    plant_id         INT PRIMARY KEY REFERENCES plants(id),
    device_id        UUID NOT NULL REFERENCES devices(id),
    mode             VARCHAR(10) NOT NULL DEFAULT 'auto',
    auto_threshold   FLOAT NOT NULL DEFAULT 30.0,
    duration_sec     INT NOT NULL DEFAULT 10,
    relay_channel    INT NOT NULL,
    min_interval_min INT NOT NULL DEFAULT 240,
    updated_at       TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT watering_mode_valid CHECK (mode IN ('auto', 'manual')),
    CONSTRAINT watering_relay_channel_range CHECK (relay_channel BETWEEN 1 AND 8),
    CONSTRAINT watering_duration_positive CHECK (duration_sec > 0),
    CONSTRAINT watering_min_interval_positive CHECK (min_interval_min > 0),
    -- A relay channel maps to a physical GPIO pin and a single solenoid.
    -- Assigning two plants to the same (device, relay) would either water
    -- the wrong plant or fire two `water_now` commands competing for one
    -- relay. Enforce one-plant-per-relay-per-device at the schema level.
    CONSTRAINT watering_relay_unique_per_device UNIQUE (device_id, relay_channel)
);

-- Watering events
--
-- Command lifecycle (`status`):
--   pending     — Row inserted by Consumer (auto-water) or API (manual). Outbox
--                 row exists with status='pending'; Pi has not received the
--                 command yet.
--   dispatched  — Command Dispatcher published the command to MQTT and the
--                 broker accepted the publish. This does NOT mean the Pi has
--                 received or executed it — only that the broker has it queued
--                 for delivery (QoS 1). If the Pi is offline, the broker holds
--                 the message until the Pi's persistent session resumes.
--   completed   — Pi published a `watering_completed` event for this
--                 command_id. Side effect (relay actuation) is confirmed.
--   failed      — Either the broker rejected dispatch after MAX_ATTEMPTS, or
--                 the Pi published a `watering_failed` event (bad relay,
--                 tank-empty, etc).
--   expired     — Either the cleanup task aged out a row (see grace note
--                 below), or the Pi received the command after `expires_at`
--                 and refused to execute it.
--
-- Terminal states (completed | failed | expired) are absorbing — the guarded
-- update_watering_event() refuses to overwrite a terminal row, so racing
-- terminal events from Pi vs. cleanup task are benign (first writer wins).
--
-- Cleanup grace window: the Pi checks `expires_at` only BEFORE starting the
-- relay, so an execution that starts just inside the window may legitimately
-- complete up to MAX_WATERING_DURATION_SEC (120s) after it. Cleanup therefore
-- expires rows only after `expires_at + 130s` — otherwise it could mark an
-- in-flight watering 'expired' (absorbing!), the real `watering_completed`
-- couldn't correct it, and last_successful_watering_at would miss a genuine
-- watering, letting auto-water fire again → double watering.
CREATE TABLE watering_events (
    id            BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    plant_id      INT NOT NULL REFERENCES plants(id),
    device_id     UUID NOT NULL REFERENCES devices(id),
    command_id    UUID NOT NULL UNIQUE,
    triggered_by  VARCHAR(10) NOT NULL,
    duration_sec  INT NOT NULL,
    status        VARCHAR(20) NOT NULL,
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- expires_at is the same value sent to the Pi in the MQTT command payload.
    -- Cleanup uses this column directly so that the DB "expired" state aligns
    -- with the Pi's expires_at check — otherwise the DB could mark a command
    -- expired while the Pi later receives and executes it (silent divergence).
    expires_at    TIMESTAMPTZ NOT NULL,
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT watering_trigger_valid CHECK (triggered_by IN ('auto', 'manual', 'bot')),
    CONSTRAINT watering_status_valid CHECK (status IN ('pending', 'dispatched', 'completed', 'failed', 'expired'))
);

CREATE INDEX ON watering_events (plant_id, status, requested_at DESC);
CREATE INDEX ON watering_events (plant_id, completed_at DESC);

-- Transactional outbox for reliable command dispatch to MQTT
CREATE TABLE command_outbox (
    id              BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    command_id      UUID NOT NULL UNIQUE REFERENCES watering_events(command_id),
    device_id       UUID NOT NULL REFERENCES devices(id),
    topic           VARCHAR(255) NOT NULL,
    payload         JSONB NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempts        INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at   TIMESTAMPTZ,
    CONSTRAINT command_outbox_status_valid CHECK (status IN ('pending', 'dispatched', 'failed'))
);

CREATE INDEX ON command_outbox (status, next_attempt_at);
-- Dispatcher cleanup job deletes by device_id + terminal status:
CREATE INDEX ON command_outbox (device_id, status);

-- Idempotency for Pi events and sensor readings consumed from MQTT
-- event_id = reading_id (for sensor readings) or event_id (for watering events)
CREATE TABLE processed_sensor_events (
    event_id      UUID PRIMARY KEY,
    command_id    UUID,
    event_type    VARCHAR(50) NOT NULL,    -- 'sensor_reading' | 'watering_completed' | ...
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Cleanup job deletes by processed_at range; PRIMARY KEY covers point lookups:
CREATE INDEX ON processed_sensor_events (processed_at);

-- Alerts
CREATE TABLE alerts (
    id            BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    plant_id      INT REFERENCES plants(id),   -- NULL for device-level alerts
    sensor_id     INT REFERENCES sensors(id),
    alert_type    VARCHAR(50) NOT NULL,         -- 'too_dry' | 'too_wet' | 'too_hot' | 'water_low' | 'sensor_error'
    value         FLOAT,
    sent_at       TIMESTAMPTZ DEFAULT now(),
    acknowledged  BOOLEAN DEFAULT false
);
-- Cleanup job filters acknowledged=true + age; alert cooldown check queries sensor_id:
CREATE INDEX ON alerts (sensor_id, alert_type, sent_at DESC);
CREATE INDEX ON alerts (acknowledged, sent_at DESC);

-- Dead letters: any MQTT payload that fails JSON parse or Pydantic validation
-- lands here for diagnosis. The Consumer never crashes on a malformed message
-- and never silently drops one — every failure is observable. Note: well-formed
-- messages for unknown (device_id, address, sensor_type) tuples are NOT
-- dead-lettered; that's an intentional design choice (not all ADC channels
-- need to be wired). A separate metrics counter tracks those skips.
CREATE TABLE dead_letters (
    id          BIGINT PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    topic       VARCHAR(255) NOT NULL,
    raw_payload TEXT NOT NULL,                  -- TEXT not JSONB — payload may not parse
    error       TEXT NOT NULL,
    error_kind  VARCHAR(50) NOT NULL,           -- 'json_decode' | 'schema_validation' | 'unsupported_version'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON dead_letters (created_at DESC);
-- 30-day retention pruned by cleanup task


-- Seed data
INSERT INTO sensor_types (code, unit, label) VALUES
    ('moisture',     '%',   'Soil Moisture'),
    ('temperature',  '°C',  'Air Temperature'),
    ('humidity',     '%RH', 'Air Humidity'),
    ('water_level',  '%',   'Water Tank Level'),
    ('light',        'lux', 'Light Intensity');
```

---

## MQTT Topics & Message Schemas

### Topic Namespace

| Direction | Topic | Publisher | Subscriber | Retain |
|---|---|---|---|---|
| Pi → broker | `plant-monitor/{device_id}/readings` | Pi | Consumer | `false` |
| Pi → broker | `plant-monitor/{device_id}/events` | Pi | Consumer | `false` |
| Pi → broker | `plant-monitor/{device_id}/status` | Pi + Mosquitto last-will | Consumer | **`true`** |
| broker → Pi | `plant-monitor/{device_id}/commands` | Command Dispatcher | Pi | `false` |

> **Only `status` is retained.** `commands` MUST publish with `retain=False`. A retained `water_now` would be replayed to the Pi on every reconnect and trigger spurious watering. The Pi already has an idempotency table (terminal events keyed by `command_id`), but defending in depth at publish time costs one keyword argument.

### MQTT Topic: `plant-monitor/{device_id}/readings`
**schema_version: 2** — `address` replaces `channel` (int). For ADC sensors, Pi sends `raw_adc` and leaves `value: null`; Consumer applies per-sensor calibration. For digital sensors (DHT22), Pi sends the final `value` and `raw_adc: null`.

```json
{
  "schema_version": 2,
  "reading_id":  "uuid4",
  "device_id":   "8d1d55d1-2222-4444-8888-111111111111",
  "address":     "adc:3",
  "value":       null,
  "raw_adc":     612,
  "sensor_type": "moisture",
  "recorded_at": "2026-05-07T10:23:00Z"
}
```

GPIO sensor examples (DHT22 produces two messages per read):

```json
{ "schema_version": 2, "reading_id": "uuid4", "device_id": "...",
  "address": "gpio:4:temp", "value": 22.5, "raw_adc": null,
  "sensor_type": "temperature", "recorded_at": "..." }
```

```json
{ "schema_version": 2, "reading_id": "uuid4", "device_id": "...",
  "address": "gpio:4:hum", "value": 65.0, "raw_adc": null,
  "sensor_type": "humidity", "recorded_at": "..." }
```

> Consumer resolves `(device_id, address, sensor_type) → sensors.id → plant_id`.
> Unknown `(device_id, address, sensor_type)` tuples are silently skipped — not all ADC channels are necessarily wired.
> `reading_id` is used for deduplication in `processed_sensor_events` (QoS 1 may redeliver on Consumer restart).

---

### MQTT Topic: `plant-monitor/{device_id}/commands`
**Producer:** Command Dispatcher  
**Consumer:** Pi (`watering_controller.py`)

```json
{
  "schema_version": 2,
  "command_id":    "550e8400-e29b-41d4-a716-446655440000",
  "command":       "water_now",
  "device_id":     "8d1d55d1-2222-4444-8888-111111111111",
  "plant_id":      2,
  "relay_channel": 2,
  "duration_sec":  10,
  "expires_at":    "2026-05-07T11:00:00Z"
}
```

---

### MQTT Topic: `plant-monitor/{device_id}/events`
**Producer:** Pi (`watering_controller.py`)  
**Consumer:** Consumer service (`event_handler.py`)

```json
{ "schema_version": 2, "event_id": "uuid4", "event": "watering_started",
  "command_id": "550e8400-...", "device_id": "...", "plant_id": 2,
  "relay_channel": 2, "recorded_at": "2026-05-07T10:54:50Z" }
```

```json
{ "schema_version": 2, "event_id": "uuid4", "event": "watering_completed",
  "command_id": "550e8400-...", "device_id": "...", "plant_id": 2,
  "relay_channel": 2, "duration_sec": 10, "triggered_by": "manual",
  "recorded_at": "2026-05-07T10:55:00Z" }
```

```json
{ "schema_version": 2, "event_id": "uuid4", "event": "watering_failed",
  "command_id": "550e8400-...", "device_id": "...", "plant_id": 2,
  "relay_channel": 2, "error": "relay_not_responding", "recorded_at": "..." }
```

```json
{ "schema_version": 2, "event_id": "uuid4", "event": "command_expired",
  "command_id": "550e8400-...", "device_id": "...", "plant_id": 2,
  "relay_channel": 2, "recorded_at": "..." }
```

> The Pi stores the original terminal event for each `command_id` in local SQLite after the command reaches a final outcome (`watering_completed`, `watering_failed`, or `command_expired`). If MQTT redelivers the same command later, the Pi republishes that same terminal event with a new `event_id` instead of inventing a separate duplicate event. This lets the Consumer resolve a stuck `pending`/`dispatched` command immediately while preserving the true outcome. The stale-expiry remains as the safety net for genuine timeouts (Pi offline, relay failure, network loss).

---

### MQTT Topic: `plant-monitor/{device_id}/status`
**Producer:** Pi (on connect: `online`) + Mosquitto last-will (on unexpected disconnect: `offline`)  
**Consumer:** Consumer service (updates `devices.status` + `last_seen_at`)

```json
{ "schema_version": 2, "device_id": "...", "status": "online", "recorded_at": "...",
  "firmware_version": "1.2.0", "boot_at": "2026-05-13T09:00:00Z",
  "capabilities": {"tank_sensor": true, "dht22": true} }
{ "schema_version": 2, "device_id": "...", "status": "offline" }
```

> `firmware_version`, `boot_at`, and `capabilities` are included only in the `online` message (the last-will `offline` payload is pre-baked at connect time and cannot be updated). The Consumer writes these to `devices.firmware_version`, `devices.last_boot_at`, and `devices.capabilities` whenever it processes an `online` status event. Fields are optional in the Pydantic model — older firmware that doesn't send them leaves the DB columns NULL.

Use `retain=True` so new Consumer instances immediately get the last known device status.

> The last-will payload intentionally omits `recorded_at`: Mosquitto fires the last-will at an unknown future moment (whenever the broker detects the Pi's TCP/MQTT keepalive has lapsed), so any timestamp the Pi pre-baked into the will would be misleadingly early. The `StatusMessage` Pydantic model marks `recorded_at` as optional, and the Consumer handler substitutes `now()` when it's missing so `devices.last_seen_at` reflects when the broker observed the transition.

---

## FastAPI Endpoints

```
# Auth
POST   /auth/session                        → exchange Telegram initData for a 24h session token

# Plants
GET    /plants                              → list all plants with latest readings per sensor
GET    /plants/{id}                         → single plant detail
POST   /plants                              → create plant
PATCH  /plants/{id}                         → update plant name/location

# Readings
GET    /plants/{id}/readings/latest         → most recent value per sensor
GET    /plants/{id}/readings/history        → raw readings, params: sensor_type, from, to
GET    /plants/{id}/readings/aggregated     → aggregated, params: sensor_type, bucket=1h|1d, from, to

# Watering
GET    /plants/{id}/watering/config         → current mode + thresholds
PATCH  /plants/{id}/watering/config         → update mode (auto|manual), threshold, duration
POST   /plants/{id}/water                   → issue water_now command → returns command_id
GET    /commands/{command_id}/status        → pending | dispatched | completed | failed | expired

# Sensors
GET    /sensors/types                       → list all sensor types
GET    /sensors                             → list all sensors with plant assignment
GET    /sensors/{id}/alert-config           → alert thresholds for a sensor
PATCH  /sensors/{id}/alert-config           → set too_low / too_high threshold + cooldown
GET    /sensors/{id}/calibration            → calibration row for a sensor
PUT    /sensors/{id}/calibration            → set raw_dry / raw_wet (recalibration)

# Alerts
GET    /alerts                              → params: unacked=true
POST   /alerts/{id}/ack                     → mark as acknowledged

# Health
GET    /health                              → 200 OK when app + DB are reachable
```

`POST /auth/session` is the only endpoint that accepts raw initData:
```
X-Telegram-Init-Data: <initData from Telegram.WebApp.initData>
```
It validates the HMAC and 5-minute freshness, then returns a signed 24 h session token. All other endpoints require:
```
Authorization: Bearer <session token>
```
(initData is regenerated only when the Mini App is reopened — validating it per-request would 401 any dashboard left open longer than 5 minutes.)

---

## Implementation Phases

### Phase 0 — Infrastructure

**0.1 Oracle Cloud VPS**
- Register at cloud.oracle.com → Always Free tier
- Create VM: Ubuntu 22.04, shape `VM.Standard.A1.Flex` (1 vCPU, 6 GB RAM)
- Open ports in Security List:
  - `22` — SSH
  - `80`, `443` — HTTP/HTTPS (Nginx)
  - `8883` — MQTT over TLS (restrict to known device IPs when possible)

**0.2 Domain + SSL**
- Point A record to VPS IP
- `snap install --classic certbot && certbot --nginx -d yourdomain.com`

**0.3 Docker**
```bash
apt update && apt install -y docker.io docker-compose-plugin
usermod -aG docker ubuntu
```

**0.4 Telegram Bot**
- Create bot via @BotFather → get `BOT_TOKEN`
- Create Mini App → `newapp` → URL: `https://yourdomain.com/app`

**0.5 TimescaleDB backups**

A free-tier VPS volume failure = total loss of months of sensor history, watering events, and calibration. A nightly `pg_dump` to Oracle Object Storage (20 GB free tier) is the minimum viable backup.

```bash
# /usr/local/bin/plantmonitor-backup.sh
#!/usr/bin/env bash
set -euo pipefail
TS=$(date -u +%Y%m%dT%H%M%SZ)
DUMP=/tmp/plantmonitor-${TS}.dump
docker compose exec -T timescaledb pg_dump \
    -U plantuser -d plantmonitor -F c -Z 9 > "$DUMP"
# Upload to Oracle Object Storage (or s3-compatible bucket). 14-day retention.
oci os object put -bn plantmonitor-backups --file "$DUMP" --name "${TS}.dump"
find /tmp -name 'plantmonitor-*.dump' -mtime +1 -delete
```

```cron
# /etc/cron.d/plantmonitor-backup
0 3 * * * root /usr/local/bin/plantmonitor-backup.sh >> /var/log/plantmonitor-backup.log 2>&1
```

**Restore drill** — at least once after Phase 1, verify a dump restores into a scratch container:
```bash
docker run --rm -e POSTGRES_PASSWORD=x -d --name pg_restore_test timescale/timescaledb:latest-pg16
docker exec -i pg_restore_test pg_restore -U postgres -d postgres --create < /tmp/plantmonitor-*.dump
```

If restore drill is skipped, treat the backup as non-existent.

**0.6 Let's Encrypt renewal**

`certbot --nginx` provisions the cert during Phase 0.2, but the cert lifetime is 90 days. Since Nginx runs in a container and mounts `/etc/letsencrypt:ro`, the renewal command must run on the host and trigger a container reload:

```cron
# /etc/cron.d/certbot-renew
0 4 * * * root certbot renew --quiet --deploy-hook "docker compose -f /opt/plant-monitor/docker-compose.yml exec nginx nginx -s reload"
```

Verify with `certbot renew --dry-run` after install.

---

### Phase 1 — TimescaleDB

**1.1 docker-compose.yml (TimescaleDB only first)**
```yaml
services:
  timescaledb:
    image: timescale/timescaledb:latest-pg16
    environment:
      POSTGRES_DB: plantmonitor
      POSTGRES_USER: plantuser
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - timescaledb_data:/var/lib/postgresql/data
      - ./migrations:/docker-entrypoint-initdb.d
    ports:
      - "5432:5432"   # close externally after setup

volumes:
  timescaledb_data:
```

**1.2 Run migrations**
- Place schema SQL in `./migrations/001_init.sql`
- `docker compose up timescaledb`
- Schema runs automatically on first start

> **`docker-entrypoint-initdb.d` only fires on an empty volume.** It's fine for bootstrap, but adding `002_add_X.sql` later does nothing on existing databases. Adopt Alembic from day one — even if `001` is just a `op.execute(open('001_init.sql').read())` shim — so subsequent schema changes have a real upgrade path. Run `alembic upgrade head` as a one-shot init container in `docker-compose.yml` before Consumer/API start.

```yaml
# docker-compose.yml — add before consumer/api/dispatcher
migrate:
  build: ./migrations              # Dockerfile installs alembic + asyncpg
  command: ["alembic", "upgrade", "head"]
  environment:
    DATABASE_URL: postgresql+asyncpg://plantuser:${DB_PASSWORD}@timescaledb/plantmonitor
  depends_on:
    timescaledb:
      condition: service_healthy
  restart: "no"
```

Other services then `depends_on: { migrate: { condition: service_completed_successfully } }`.

> **Backup before every prod migration.** Alembic has no automatic rollback for TimescaleDB schemas (compression policies, hypertables, continuous aggregates don't downgrade cleanly). The practical rollback is "restore yesterday's dump". Before running `alembic upgrade head` on the prod VPS, take an out-of-band dump:
>
> ```bash
> /usr/local/bin/plantmonitor-backup.sh   # the same script from Phase 0.5
> ```
>
> If the migration goes wrong, restore from that dump and roll back the application image — do not rely on `alembic downgrade`.

> **Image tagging and application rollback.** Never deploy `latest` to production — it breaks rollbacks and makes auditing impossible. Tag every build with a version and keep the previous image available:
>
> ```bash
> # Build and tag
> docker build -t plant-monitor-consumer:v1.2.0 ./consumer
> docker build -t plant-monitor-api:v1.2.0 ./api
> # ... same for bot, cmd_dispatcher, migrations
>
> # Deploy by pinning the version in .env or directly in compose:
> # CONSUMER_IMAGE=plant-monitor-consumer:v1.2.0
>
> # Rollback: re-pin to the previous tag and restart the affected service
> docker compose up -d --no-deps consumer
> ```
>
> A simple convention: `vMAJOR.MINOR.PATCH` incremented manually, or use the short git SHA (`git rev-parse --short HEAD`) as the tag for automatic traceability. The migrate service image should be versioned independently — never roll back app code past a migration without also restoring the DB dump from that point.

**1.3 Seed reference data**
```sql
INSERT INTO sensor_types ...
INSERT INTO devices ...
INSERT INTO plants ...
INSERT INTO sensors (device_id, sensor_type_id, plant_id, address, label) VALUES
    -- ADC moisture sensors
    ('<device_uuid>', <moisture_type_id>, 1, 'adc:0', 'Monstera moisture'),
    ('<device_uuid>', <moisture_type_id>, 2, 'adc:1', 'Cactus moisture'),
    -- ADC analog sensors
    ('<device_uuid>', <water_level_type_id>, NULL, 'adc:6', 'Water tank'),
    ('<device_uuid>', <light_type_id>,       NULL, 'adc:7', 'Ambient light'),
    -- GPIO sensors (DHT22 — two logical sensors, one physical device)
    ('<device_uuid>', <temperature_type_id>, NULL, 'gpio:4:temp', 'Room temperature'),
    ('<device_uuid>', <humidity_type_id>,    NULL, 'gpio:4:hum',  'Room humidity');
INSERT INTO watering_config ...
INSERT INTO sensor_alert_config ...
```

**Validation:** `psql` — verify tables exist and `SELECT * FROM timescaledb_information.hypertables`.

---

### Phase 2 — Raspberry Pi Client

**2.0 Time sync (required)**

`recorded_at` is generated on the Pi and powers TimescaleDB continuous aggregates. A 60-second clock drift puts readings in the wrong hourly bucket; a multi-minute drift breaks ordering between Pi and Consumer wall-clocks (`now()` on the DB side) for command expiry checks. NTP is therefore **not optional**:

```bash
sudo apt install -y chrony
sudo systemctl enable --now chrony
chronyc tracking   # expect 'System time' offset < 100 ms
```

Add a startup check in `sensor_reader.py` (or a systemd unit dependency) that refuses to publish if `chronyc tracking` reports `Leap status: Not synchronised`. The DS3231 RTC remains an *enhancement* for offline-recovery timestamps, not a replacement for NTP.

**2.1 Sensor abstraction layer**

```python
# pi_client/sensors/base.py
from abc import ABC, abstractmethod

class Sensor(ABC):
    def __init__(self, address: str, sensor_type: str):
        self.address = address
        self.sensor_type = sensor_type

    @abstractmethod
    async def read(self) -> list[dict]:
        """Returns a list of reading dicts (one per value). Each dict carries
        its own `address` — multi-value sensors (DHT22) emit sub-addresses of
        the sensor's base address (e.g. "gpio:4" → "gpio:4:temp")."""
```

```python
# pi_client/sensors/adc.py
import spidev
from .base import Sensor

class ADCSensor(Sensor):
    """Pi is a transducer — it publishes raw_adc only. The Consumer applies
    per-sensor calibration server-side so recalibration never requires a Pi
    deploy or a Pi → API auth channel."""

    def __init__(self, channel: int, sensor_type: str):
        super().__init__(f"adc:{channel}", sensor_type)
        self.channel = channel

    async def read(self) -> list[dict]:
        raw = read_mcp3008(self.channel)      # SPI read
        return [{"address": self.address, "value": None,
                 "raw_adc": raw, "sensor_type": self.sensor_type}]
```

```python
# pi_client/sensors/dht22.py
import asyncio
import adafruit_dht, board
from .base import Sensor

DHT22_READ_TIMEOUT_SEC = 3.0   # adafruit_dht can block on bad timing; cap it.

class DHT22Sensor(Sensor):
    def __init__(self, gpio_pin: int):
        # Base address is the physical attachment point ("gpio:4"), used for
        # logging only; read() emits per-value sub-addresses ("gpio:4:temp",
        # "gpio:4:hum") that match the sensors table.
        super().__init__(f"gpio:{gpio_pin}", "dht22")
        self._dht = adafruit_dht.DHT22(getattr(board, f"D{gpio_pin}"))
        self._gpio_pin = gpio_pin

    def _blocking_read(self) -> tuple[float | None, float | None]:
        # Property access is what actually triggers the GPIO read and may block.
        return self._dht.temperature, self._dht.humidity

    async def read(self) -> list[dict]:
        # Run the sync library in an executor with a hard timeout so a wedged
        # GPIO read can't stall the entire sensor cycle. On timeout, raise so
        # sensor_reader's per-sensor try/except logs it and moves on.
        loop = asyncio.get_running_loop()
        temp, hum = await asyncio.wait_for(
            loop.run_in_executor(None, self._blocking_read),
            timeout=DHT22_READ_TIMEOUT_SEC,
        )
        base = f"gpio:{self._gpio_pin}"
        return [
            {"address": f"{base}:temp", "value": temp,
             "raw_adc": None, "sensor_type": "temperature"},
            {"address": f"{base}:hum",  "value": hum,
             "raw_adc": None, "sensor_type": "humidity"},
        ]
```

**2.2 sensor_reader.py**

Sensor list is config-driven — add sensors in `.env` without code changes.

**Local reading outbox.** Pi readings carry no special value if dropped during a broker outage, BUT continuous aggregates rely on dense time-series — gaps distort hourly/daily averages. To survive transient MQTT failures, every reading is staged in a local SQLite outbox (`reading_outbox` table in `executed_commands.db`) before publish. On each run, we first drain pending rows, then read fresh sensors. A reading is deleted from the outbox only after a successful MQTT publish (broker-confirmed). MQTT QoS 1 takes over from there.

```sql
-- pi_client/executed_commands.db
CREATE TABLE IF NOT EXISTS reading_outbox (
    reading_id   TEXT PRIMARY KEY,        -- UUID, also dedups across Consumer
    payload      TEXT NOT NULL,           -- full JSON message
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    attempts     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS reading_outbox_created ON reading_outbox(created_at);
```

```python
#!/usr/bin/env python3
import asyncio, json, os, sqlite3
from uuid import uuid4
from datetime import datetime, timezone
from asyncio_mqtt import Client, TLSParameters, MqttError
from sensors.adc   import ADCSensor
from sensors.dht22 import DHT22Sensor

DEVICE_ID     = os.environ["DEVICE_ID"]
SENSOR_CONFIG = os.environ["SENSOR_CONFIG"]
OUTBOX_DB     = os.environ.get("PI_OUTBOX_DB", "/home/pi/plant-monitor/pi_client/executed_commands.db")
OUTBOX_MAX_ROWS = 2000   # bound disk usage; older readings drop if broker is down for ~33h at 1/min

def build_sensors(config: str) -> list:
    sensors = []
    for entry in config.split(","):
        parts = entry.strip().split(":")
        if parts[0] == "adc":
            sensors.append(ADCSensor(channel=int(parts[1]), sensor_type=parts[2]))
        elif parts[0] == "gpio" and parts[2] == "dht22":
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

async def main():
    sensors = build_sensors(SENSOR_CONFIG)
    tls = TLSParameters(ca_certs=os.environ["MQTT_CA_FILE"])
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
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    **reading,
                })
        except Exception as e:
            print(f"Sensor {sensor.address} read error: {e}")

    # sensor_reader.py is one-shot and exits cleanly each run — no last-will, no
    # persistent session needed; the outbox provides offline tolerance instead.
    try:
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

asyncio.run(main())
```

> The outbox sits between the sensor read and the broker, so a broker outage no longer creates time-series holes. `reading_id` is still a UUID generated once at stage time, so duplicate publishes (e.g. timer fires before a slow drain completes) are deduped by Consumer's `processed_sensor_events` table.

**2.3 systemd timer + watchdog**

```ini
# /etc/systemd/system/plant-sensor.service
[Unit]
Description=Plant Sensor Reader
[Service]
Type=simple
ExecStart=/usr/bin/python3 /home/pi/plant-monitor/pi_client/sensor_reader.py
User=pi
EnvironmentFile=/home/pi/plant-monitor/pi_client/.env
Restart=on-failure
RestartSec=10
# sensor_reader runs ≤ 60s per invocation; if it hangs past 90s, kill it so
# the next timer tick can fire. Hangs typically come from blocked SPI or a
# wedged TLS handshake — neither benefits from waiting.
# RuntimeMaxSec, NOT TimeoutStartSec: with Type=simple, "startup" completes at
# exec, so TimeoutStartSec would never fire and a wedged run would stay active
# forever. RuntimeMaxSec bounds total runtime; the kill counts as failure, so
# Restart=on-failure relaunches it and the timer re-arms on that activation.
RuntimeMaxSec=90
```

```ini
# /etc/systemd/system/plant-sensor.timer
[Unit]
Description=Run sensor reader every 60 seconds
[Timer]
OnBootSec=10s
# OnUnitActiveSec counts from the last activation START, not finish. Overlap is
# still impossible (starting an already-active unit is a no-op), but an elapsed
# timer does NOT re-arm until the unit's next activation — so a run that never
# exits would silently stop ALL future ticks, not just delay one. The liveness
# guard is RuntimeMaxSec=90 on the .service: it kills a hung run, the
# on-failure restart re-activates the unit, and this timer re-arms. Do not
# raise RuntimeMaxSec without reconsidering this 60s interval.
OnUnitActiveSec=60s
[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/plant-watering.service
# Always-on process — owns MQTT last-will, persistent session, relay GPIO.
# Uses systemd Type=notify watchdog so a hung event loop is detected and
# the process restarted automatically.
[Unit]
Description=Plant Watering Controller
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/python3 /home/pi/plant-monitor/pi_client/watering_controller.py
User=pi
EnvironmentFile=/home/pi/plant-monitor/pi_client/.env
Restart=always
RestartSec=5
WatchdogSec=30

[Install]
WantedBy=multi-user.target
```

The Python side calls `sdnotify` periodically from the main MQTT loop:

```python
# pi_client/watering_controller.py
from sdnotify import SystemdNotifier
notifier = SystemdNotifier()
notifier.notify("READY=1")
# in the main loop, every iteration or every N seconds:
notifier.notify("WATCHDOG=1")
```

If the loop blocks (deadlocked SPI, hung MQTT call, GPIO library bug), `WATCHDOG=1` stops firing, systemd kills the unit at `WatchdogSec`, and `Restart=always` brings it back. Without this, a hung event loop is invisible to systemd — the process is "alive" but not doing work.

**2.4 watering_controller.py**

Key responsibilities (Pi is purely reactive, but enforces physical safety locally):
- Own MQTT last-will and device status — `watering_controller.py` is the always-on process; it sets a last-will of `{"status": "offline"}` on connect and publishes `{"status": "online"}` with `retain=True`; if it crashes unexpectedly, Mosquitto fires the last-will automatically. `sensor_reader.py` is one-shot and exits cleanly each run, so its last-will never fires.
- Use a **persistent MQTT session** — `clean_session=False` with a fixed `client_id=f"watering_{DEVICE_ID}"`. Without this, any `water_now` command published while the Pi is offline is dropped by the broker on reconnect, defeating QoS 1. With it, queued commands are delivered on reconnect; Pi's SQLite dedup table catches anything that's already terminal. *(This is MQTT v3.1.1 semantics — `clean_session`. In MQTT v5 the equivalent is `clean_start=False` + `session_expiry_interval`. The codebase uses v3.1.1 throughout; persistent sessions last until the broker restarts or a client reconnects with `clean_session=True`. This is safe for a single long-lived Pi.)*
- Subscribe to `plant-monitor/{DEVICE_ID}/commands` (QoS 1)
- Check `expires_at` — skip if expired, publish `command_expired`
- Check local SQLite for `command_id` — if already seen, republish the stored terminal event (`watering_completed`, `watering_failed`, or `command_expired`) with a new `event_id` and return
- **Validate relay channel** — reject if not in `RELAY_PINS`, publish `watering_failed`
- **Cap duration** — clamp to `MAX_WATERING_DURATION_SEC` regardless of command payload
- **Tank-low lockout** — if water level sensor reads below `TANK_MIN_PCT`, refuse and publish `watering_failed` with `error: "tank_empty"` (optional if water level sensor is wired)
- **Initialise all relay pins to the de-energised level at startup** before connecting to MQTT — ensures no relay fires in the window between GPIO init and first command delivery, and protects against phantom actuation if the process restarts with queued broker messages. **The wired board is active-LOW: de-energised is HIGH, not LOW.** Use `GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)`, which sets the level as part of setup — a separate `setup()` then `output(HIGH)` leaves a brief window at the pin's default level. Never hardcode LOW/HIGH at call sites; derive both from one `RELAY_ACTIVE_HIGH = False` constant so a board swap is a one-line change.
- Control GPIO relay — wrap execution in `asyncio.wait_for(timeout=MAX_WATERING_DURATION_SEC + 5)` and always drive the pin to the de-energised level (HIGH on this board) in `finally` so the relay de-energises even on crash or timeout; publish `watering_started` immediately before activating, then `watering_completed` or `watering_failed` when done
- Drive output devices locally (LED colour, buzzer) — no backend commands needed
- Publish result to `plant-monitor/{DEVICE_ID}/events`

```python
# Keys are relay board input labels (IN1..IN8), matching watering_config.relay_channel.
# 4 and 7 are absent: no GPIO drives them and no solenoid is attached, so a command
# naming one fails fast as `invalid_relay_channel`. Do NOT renumber to close the gap —
# the numbers are the board silkscreen, which is what you read when debugging.
RELAY_PINS = {1: 5, 2: 6, 3: 13, 5: 19, 6: 26, 8: 21}
RELAY_ACTIVE_HIGH = False         # wired board energises on LOW
MAX_WATERING_DURATION_SEC = 120   # hard cap — protects against flooding if cloud sends bad value
TANK_MIN_PCT = 5.0                # refuse watering if tank this low (0 to disable)

# Persistent MQTT session — receive commands queued while Pi was offline
status_topic = f"plant-monitor/{DEVICE_ID}/status"
will = Will(status_topic,
            payload=json.dumps({"schema_version": 2, "device_id": DEVICE_ID,
                                "status": "offline"}),
            qos=1, retain=True)

async with Client(
    MQTT_HOST, port=MQTT_PORT,
    username=MQTT_USERNAME, password=MQTT_PASSWORD,
    tls_params=tls,
    client_id=f"watering_{DEVICE_ID}",   # fixed — broker resumes session on reconnect
    clean_session=False,                 # queue commands while we're offline
    will=will,
) as client:
    # Publish online status with retain=True, then subscribe to commands at QoS 1
    ...

# Runtime relay lock — prevents concurrent GPIO activation from duplicate MQTT
# deliveries, reconnect replays, or Consumer bugs. The DB unique constraint
# (watering_relay_unique_per_device) only prevents a second command being
# *created*; it does not protect the Pi from receiving the same command twice.
active_relays: set[int] = set()

async def handle_command(command: dict) -> None:
    relay_channel = command["relay_channel"]

    if relay_channel not in RELAY_PINS:
        await publish_event("watering_failed", command, error="invalid_relay_channel")
        return

    if relay_channel in active_relays:
        # Relay already running — this is a duplicate delivery or replay.
        # Reject rather than queue: the original command owns this relay slot.
        await publish_event("watering_failed", command, error="relay_busy")
        return

    duration = min(command["duration_sec"], MAX_WATERING_DURATION_SEC)
    if TANK_MIN_PCT > 0 and water_level_pct() < TANK_MIN_PCT:
        await publish_event("watering_failed", command, error="tank_empty")
        return

    active_relays.add(relay_channel)
    try:
        # GPIO initialised LOW at startup (see startup block below) — relay is
        # always safe even if this try block is never entered.
        await publish_event("watering_started", command)
        await asyncio.wait_for(
            execute_relay(RELAY_PINS[relay_channel], duration),
            timeout=MAX_WATERING_DURATION_SEC + 5,
        )
        await publish_event("watering_completed", command, duration_sec=duration)
    except asyncio.TimeoutError:
        await publish_event("watering_failed", command, error="relay_timeout")
    except Exception as e:
        await publish_event("watering_failed", command, error="relay_error")
        logger.exception("relay error on channel %d: %s", relay_channel, e)
    finally:
        GPIO.output(RELAY_PINS[relay_channel], DE_ENERGISED)  # safe regardless of outcome
        active_relays.discard(relay_channel)

# Startup: drive ALL relay pins to DE_ENERGISED before connecting to MQTT so no
# relay fires between GPIO init and the first command. Prevents phantom actuation
# if the process restarts while a command is queued in the broker session.
#
# NOTE this closes the window from *process start*, not from *power-on*. On an
# active-low board the SoC's boot pulls (GPIO0-8 up, GPIO9-27 down) can leave
# channels energised before any software runs. Measured on the fitted board and
# it does not: its IN lines carry their own pull-up, which beats the internal
# pull-down, so all six pins read high at power-on. See pi_client/relay.py for
# the measurement — repeat it if the relay module is ever swapped.
for pin in RELAY_PINS.values():
    GPIO.setup(pin, GPIO.OUT, initial=DE_ENERGISED)

# Output devices respond to Pi's own state — no backend commands
# LED: green=idle, yellow=watering, red=relay_error/tank_empty
# Buzzer: short beep on watering_completed; two beeps on watering_failed
```

**2.5 Output devices (Pi-local only)**

```python
# pi_client/outputs/led.py
# RGB LED driven by GPIO — reflects Pi operational state
# States: idle (green), watering (yellow), error (red), offline (off)
# Called from watering_controller.py state transitions
```

```python
# pi_client/outputs/buzzer.py
# Short pulse on watering_completed; two pulses on watering_failed
# Called from watering_controller.py after relay events
```

Output devices never need backend commands — the Pi already knows when it's executing a relay command. This preserves the "Pi is purely reactive" design.

**2.6 DS3231 RTC (optional)**

If the Pi may be offline when sensor readings occur, use the RTC for accurate `recorded_at` timestamps. `sensor_reader.py` reads time from RTC instead of system clock when available.

---

### Phase 3 — Mosquitto + Command Dispatcher

**3.1 Mosquitto config**

```
# mosquitto/mosquitto.conf

# External TLS listener for Pi devices
listener 8883 0.0.0.0
protocol mqtt
cafile   /mosquitto/certs/ca.crt
certfile /mosquitto/certs/server.crt
keyfile  /mosquitto/certs/server.key
require_certificate false
tls_version tlsv1.2

# Internal plaintext listener for Docker services
listener 1883

allow_anonymous false
password_file /mosquitto/config/passwd
acl_file      /mosquitto/config/acl

persistence true
persistence_location /mosquitto/data/
# Default autosave_interval is 1800s — a broker crash within that window loses
# up to 30 minutes of persistent queue state (Consumer's clean_session=False
# queue, retained statuses). 30s caps the worst-case loss.
autosave_interval 30

# Persistent-session queue limits. Defaults are 1000 / unlimited; bumping to
# 5000 / 10 MiB lets the Consumer be down for ~3 hours at 30 msg/min (9 ADC
# channels + DHT22 temp+hum = ~11 readings/min, plus events and status
# messages) before the broker starts dropping queued messages. Beyond that,
# the Pi outbox still preserves readings — but status transitions during the
# outage are best-effort once these caps are hit.
max_queued_messages 5000
max_queued_bytes 10485760

# Cap message size at 8 KiB — the largest legitimate payload is a watering
# event with error string, well under 1 KiB. Anything larger is malformed
# or hostile; reject at the broker rather than buffering it in Consumer.
message_size_limit 8192
```

```
# mosquitto/acl

# Per-device: scoped to own namespace only
user pi_living_room_01
topic write plant-monitor/8d1d55d1-2222-4444-8888-111111111111/readings
topic write plant-monitor/8d1d55d1-2222-4444-8888-111111111111/events
topic write plant-monitor/8d1d55d1-2222-4444-8888-111111111111/status
topic read  plant-monitor/8d1d55d1-2222-4444-8888-111111111111/commands

# Backend Consumer — read-only on inbound topics, no command-write capability.
user backend_consumer
topic read  plant-monitor/+/readings
topic read  plant-monitor/+/events
topic read  plant-monitor/+/status
topic write health/check

# Backend Command Dispatcher — write-only on commands, no inbound read access.
# Splitting from backend_consumer enforces least privilege: a compromised
# Consumer cannot publish commands, and a compromised Dispatcher cannot
# exfiltrate readings.
user backend_dispatcher
topic write plant-monitor/+/commands
topic write health/check
```

Generate credentials:
```bash
mosquitto_passwd -c mosquitto/passwd pi_living_room_01
mosquitto_passwd    mosquitto/passwd backend_consumer
mosquitto_passwd    mosquitto/passwd backend_dispatcher
```

Generate TLS certificates:
```bash
# CA key + cert
openssl genrsa -out ca.key 4096
openssl req -new -x509 -days 3650 -key ca.key -out ca.crt -subj "/CN=PlantMonitor CA"

# Broker key + cert
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr -subj "/CN=yourdomain.com"
openssl x509 -req -days 3650 -in server.csr -CA ca.crt -CAkey ca.key \
    -CAcreateserial -out server.crt

# Copy ca.crt to Pi as ca.crt
```

**3.2 Add Mosquitto to docker-compose.yml**

```yaml
  mosquitto:
    image: eclipse-mosquitto:2
    ports:
      - "8883:8883"     # external TLS — Pi devices
      # 1883 not exposed externally — Docker network only
    environment:
      MQTT_CONSUMER_PASSWORD: ${MQTT_CONSUMER_PASSWORD}
    volumes:
      - ./mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - ./mosquitto/passwd:/mosquitto/config/passwd:ro
      - ./mosquitto/acl:/mosquitto/config/acl:ro
      - ./mosquitto/certs:/mosquitto/certs:ro
      - mosquitto_data:/mosquitto/data
    healthcheck:
      test: ["CMD-SHELL", "mosquitto_pub -h 127.0.0.1 -p 1883 -u backend_consumer -P $$MQTT_CONSUMER_PASSWORD -t 'health/check' -m '1' -q 0"]
      interval: 10s
      timeout: 5s
      retries: 5
```

**3.3 Command Dispatcher**

Polls `command_outbox` and dispatches pending commands to MQTT. Retry policy: up to 3 attempts with exponential backoff; marks `failed` after max attempts.

```python
# cmd_dispatcher/main.py
import asyncio, json, os
from datetime import datetime, timezone, timedelta
import asyncpg
from asyncio_mqtt import Client as MQTTClient

POLL_INTERVAL = 2       # seconds between outbox polls
MAX_ATTEMPTS  = 3
BACKOFF_BASE  = 10      # seconds; doubled per attempt

async def dispatch_loop(pool, mqtt):
    while True:
        # Each iteration acquires its own connection from the pool. `FOR UPDATE
        # SKIP LOCKED` is defensive: it makes the dispatcher safe to run with
        # multiple instances (rolling restarts, future scale-out) without
        # double-publishing commands. The transaction holds the row lock until
        # the row is updated to 'dispatched' (or its backoff bumped), so a
        # concurrent dispatcher silently skips it.
        async with pool.acquire() as db, db.transaction():
            rows = await db.fetch("""
                SELECT id, command_id, device_id, topic, payload, attempts
                FROM command_outbox
                WHERE status = 'pending' AND next_attempt_at <= now()
                ORDER BY created_at
                LIMIT 20
                FOR UPDATE SKIP LOCKED
            """)
            for row in rows:
                try:
                    # retain=False is mandatory — a retained water_now would be
                    # redelivered to the Pi on every reconnect and trigger watering
                    # for as long as the topic exists on the broker.
                    await mqtt.publish(row["topic"], json.dumps(dict(row["payload"])),
                                       qos=1, retain=False)
                    # Update both tables in the same transaction — watering_events
                    # is the canonical lifecycle table queried by the API;
                    # command_outbox is dispatch state.
                    await db.execute("""
                        UPDATE command_outbox
                        SET status = 'dispatched', dispatched_at = now()
                        WHERE id = $1
                    """, row["id"])
                    await db.execute("""
                        UPDATE watering_events
                        SET status = 'dispatched'
                        WHERE command_id = $1 AND status = 'pending'
                    """, row["command_id"])
                    # started_at is intentionally NOT set here — the Dispatcher only
                    # knows the command reached the broker (dispatched), not that the
                    # relay actually fired. Pi sets started_at by publishing a
                    # watering_started event; Consumer writes it via update_watering_event.
                except Exception:
                    attempts = row["attempts"] + 1
                    if attempts >= MAX_ATTEMPTS:
                        # Mirror failure into watering_events so the API/UI don't show a stuck command.
                        await db.execute("""
                            UPDATE command_outbox SET status = 'failed', attempts = $1
                            WHERE id = $2
                        """, attempts, row["id"])
                        await db.execute("""
                            UPDATE watering_events
                            SET status = 'failed', completed_at = now()
                            WHERE command_id = $1 AND status IN ('pending', 'dispatched')
                        """, row["command_id"])
                    else:
                        backoff = timedelta(seconds=BACKOFF_BASE * (2 ** attempts))
                        await db.execute("""
                            UPDATE command_outbox
                            SET attempts = $1, next_attempt_at = now() + $2
                            WHERE id = $3
                        """, attempts, backoff, row["id"])
        await asyncio.sleep(POLL_INTERVAL)

async def main():
    # Pool, not single connection — even with one dispatcher instance, holding a
    # row lock across an MQTT publish on a single shared connection would block
    # health checks and queries from the same process. Use the same
    # create_pool_with_retry helper as Consumer for startup robustness.
    pool = await create_pool_with_retry(
        os.environ["DATABASE_URL"], min_size=2, max_size=5)
    async with MQTTClient(
        os.environ["MQTT_HOST"], port=int(os.environ.get("MQTT_PORT", "1883")),
        username=os.environ["MQTT_DISPATCHER_USERNAME"],
        password=os.environ["MQTT_DISPATCHER_PASSWORD"],
    ) as mqtt:
        await dispatch_loop(pool, mqtt)
```

**3.4 Validate**
```bash
# From Pi, run sensor_reader.py manually
# On VPS, watch Consumer logs — should see readings being inserted
docker compose logs -f consumer
```

---

### Phase 4 — Consumer

**4.1 consumer/main.py**

Single asyncio process subscribing to three MQTT topics with a persistent session, plus a background cleanup task. Also exposes a minimal `GET /health` HTTP endpoint (e.g. via `aiohttp.web` on port 8002) so Docker's healthcheck can confirm the process is alive and the DB connection is reachable — without this, the `healthcheck` in docker-compose.yml will always fail and mark the service unhealthy.

```python
import asyncio, json, os, signal
import asyncpg
from asyncio_mqtt import Client as MQTTClient

# Module-level pool — DB session per-call from a single pool, NOT a single
# shared connection. Concurrent _spawn'd tasks would serialize on one
# connection and deadlock under bursts.
db_pool: asyncpg.Pool | None = None
inflight: set[asyncio.Task] = set()
shutdown = asyncio.Event()

async def create_pool_with_retry(dsn, *, attempts=8, base=1.0, cap=30.0, **kwargs):
    """asyncpg.create_pool with bounded exponential backoff. Even with
    docker-compose's depends_on:service_healthy, TCP-readiness and
    auth-readiness can lag, and DNS or transient network blips inside the
    container can fail the very first connect."""
    delay = base
    for n in range(1, attempts + 1):
        try:
            return await asyncpg.create_pool(dsn, **kwargs)
        except (OSError, asyncpg.PostgresError) as e:
            if n == attempts:
                raise
            logger.warning("DB pool init failed (attempt %d/%d): %s; retrying in %.1fs",
                           n, attempts, e, delay)
            await asyncio.sleep(delay + random.uniform(0, delay))
            delay = min(delay * 2, cap)

async def main():
    global db_pool
    db_pool = await create_pool_with_retry(
        os.environ["DATABASE_URL"], min_size=2, max_size=10)

    # Graceful shutdown: stop accepting new messages, drain inflight tasks,
    # close DB pool. Without this, SIGTERM mid-task aborts the reading insert
    # AFTER its idempotency mark commits → readings lost.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    # Minimal /health server on :8002 — see Phase 4.1 note in plan
    health_task = asyncio.create_task(start_health_server())

    try:
        async with MQTTClient(
            os.environ["MQTT_HOST"],
            port=int(os.environ.get("MQTT_PORT", "1883")),
            username=os.environ["MQTT_CONSUMER_USERNAME"],
            password=os.environ["MQTT_CONSUMER_PASSWORD"],
            client_id="consumer_service",
            clean_session=False,    # persistent session: broker queues messages while Consumer is down
        ) as client:
            await client.subscribe("plant-monitor/+/readings", qos=1)
            await client.subscribe("plant-monitor/+/events",   qos=1)
            await client.subscribe("plant-monitor/+/status",   qos=1)

            cleanup_task = asyncio.create_task(background_cleanup())

            async def consume():
                async for message in client.messages:
                    if shutdown.is_set():
                        break
                    topic   = str(message.topic)
                    payload = json.loads(message.payload)

                    # Validate topic/payload device_id match before processing
                    topic_device_id = topic.split("/")[1]
                    if topic_device_id != payload.get("device_id"):
                        continue

                    if topic.endswith("/readings"):
                        _spawn(process_reading(topic_device_id, payload))
                    elif topic.endswith("/events"):
                        _spawn(event_handler.process(payload))
                    elif topic.endswith("/status"):
                        _spawn(update_device_status(topic_device_id, payload))

            consume_task = asyncio.create_task(consume())
            await shutdown.wait()
            consume_task.cancel()
            cleanup_task.cancel()
    finally:
        # Drain in-flight tasks up to a hard timeout — terminal events and
        # readings should finish committing before exit.
        if inflight:
            await asyncio.wait(inflight, timeout=10)
        health_task.cancel()
        await db_pool.close()

_db_semaphore = asyncio.Semaphore(50)  # cap concurrent DB-acquiring tasks

def _spawn(coro):
    """Create a task and log any unhandled exception — asyncio silently drops them."""
    task = asyncio.create_task(coro)
    inflight.add(task)
    task.add_done_callback(inflight.discard)
    task.add_done_callback(
        lambda t: logger.error("Task error", exc_info=t.exception())
        if not t.cancelled() and t.exception() else None
    )
    return task

async def _with_semaphore(coro):
    async with _db_semaphore:
        await coro
```

`_spawn` keeps the MQTT loop non-blocking while ensuring exceptions surface in logs. All spawned coroutines acquire `_db_semaphore` before touching the DB pool — this bounds simultaneous pool checkouts to 50 regardless of burst message rate, preventing pool exhaustion without replacing the task model. At the expected steady-state rate (~10–15 sensor readings/min plus sporadic events) the semaphore is never contended; it only kicks in during a reconnect storm or broker replay of a large queued backlog. Call `_spawn(_with_semaphore(process_reading(...)))` rather than `_spawn(process_reading(...))` for any handler that touches the DB.

**Per-task DB usage.** Each helper in `db.py` should accept the pool (or acquire from it internally) rather than receiving a shared connection — e.g. `async with db_pool.acquire() as conn: await conn.execute(...)`. The `async with db.transaction()` blocks shown elsewhere in this plan are shorthand for `async with db_pool.acquire() as conn, conn.transaction():`.

**Reconnect with jitter.** `asyncio-mqtt` raises `MqttError` on disconnect; without a reconnect wrapper, the service either dies (and systemd restarts at a fixed 10s interval — a synchronized stampede after a broker outage) or sits doing nothing. Wrap the MQTT session in an exponential-backoff loop with jitter so all long-running services (`watering_controller` on Pi, `consumer`, `cmd_dispatcher` on the VPS) recover gracefully and don't reconnect in lockstep.

```python
# Shared by consumer, cmd_dispatcher, watering_controller.
import random
from asyncio_mqtt import MqttError

async def run_with_reconnect(coro_factory, *, base=1.0, cap=60.0):
    """Run an MQTT-session coroutine, reconnecting with exponential backoff + jitter."""
    delay = base
    while not shutdown.is_set():
        try:
            await coro_factory()
            delay = base   # clean exit (shutdown) — reset
        except asyncio.CancelledError:
            # Shutdown path — let cancellation propagate so the supervising
            # task can finish draining. Explicit clause prevents a future
            # `except Exception` from accidentally swallowing it.
            raise
        except MqttError as e:
            logger.warning("mqtt disconnect: %s; reconnecting in %.1fs", e, delay)
            await asyncio.sleep(delay + random.uniform(0, delay))
            delay = min(delay * 2, cap)
```

Use as `await run_with_reconnect(session_main)` where `session_main()` opens the `async with MQTTClient(...) as client:` block. The `delay = base` reset on clean exit ensures the next intentional shutdown doesn't pre-load a high backoff.

**Schema-validated dispatch.** Every message is parsed by a Pydantic v2 model before reaching its handler. Failures land in `dead_letters` instead of crashing the task or polluting `processed_sensor_events` with garbage. This catches malformed firmware updates, partial writes, and prompt-injection-style payloads at the MQTT boundary.

```python
# consumer/messages.py
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field, NonNegativeInt

SUPPORTED_SCHEMA_VERSIONS = {2}

class ReadingMessage(BaseModel):
    schema_version: int
    reading_id:     UUID
    device_id:      UUID
    address:        str = Field(min_length=1, max_length=50)
    value:          float | None
    raw_adc:        NonNegativeInt | None = None
    sensor_type:    str = Field(min_length=1, max_length=50)
    recorded_at:    datetime

class WateringEventMessage(BaseModel):
    schema_version: int
    event_id:       UUID
    event:          Literal["watering_completed", "watering_failed", "command_expired"]
    command_id:     UUID
    device_id:      UUID
    plant_id:       int
    relay_channel:  int = Field(ge=1, le=8)
    duration_sec:   NonNegativeInt | None = None
    triggered_by:   Literal["auto", "manual", "bot"] | None = None
    error:          str | None = None
    recorded_at:    datetime

class StatusMessage(BaseModel):
    schema_version: int
    device_id:      UUID
    status:         Literal["online", "offline", "degraded"]
    recorded_at:    datetime | None = None
```

```python
# Consumer dispatch — replace the message loop body in main():
from pydantic import ValidationError
from consumer.messages import (
    ReadingMessage, WateringEventMessage, StatusMessage,
    SUPPORTED_SCHEMA_VERSIONS,
)

MODEL_BY_SUFFIX = {
    "readings": ReadingMessage,
    "events":   WateringEventMessage,
    "status":   StatusMessage,
}

async def dispatch(topic: str, raw: bytes) -> None:
    suffix = topic.rsplit("/", 1)[-1]
    model  = MODEL_BY_SUFFIX.get(suffix)
    if model is None:
        return  # unknown topic — shouldn't happen given ACL, ignore

    # Decode explicitly: json.loads(bytes) raises UnicodeDecodeError (not
    # JSONDecodeError) on invalid UTF-8, so a bare `except json.JSONDecodeError`
    # would crash the dispatch task on garbage bytes. Decoding first keeps the
    # error path uniform.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        await db.insert_dead_letter(topic, raw.decode("utf-8", errors="replace"),
                                    str(e), "json_decode")
        return
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        await db.insert_dead_letter(topic, text, str(e), "json_decode")
        return

    if payload.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        await db.insert_dead_letter(topic, json.dumps(payload),
                                    f"schema_version={payload.get('schema_version')}",
                                    "unsupported_version")
        return

    try:
        msg = model.model_validate(payload)
    except ValidationError as e:
        await db.insert_dead_letter(topic, json.dumps(payload),
                                    e.json(), "schema_validation")
        return

    # Validate topic device_id matches payload (was previously string comparison)
    topic_device_id = topic.split("/")[1]
    if str(msg.device_id) != topic_device_id:
        await db.insert_dead_letter(topic, json.dumps(payload),
                                    "topic/payload device_id mismatch",
                                    "schema_validation")
        return

    if suffix == "readings":
        _spawn(process_reading(topic_device_id, msg))
    elif suffix == "events":
        _spawn(event_handler.process(msg))
    elif suffix == "status":
        _spawn(update_device_status(topic_device_id, msg))
```

> Handlers below receive **typed Pydantic objects**, not dicts. Update `process_reading`, `event_handler.process`, and `update_device_status` to access `msg.field` instead of `payload["field"]`. The idempotency mark inside `process_reading` keys on `msg.reading_id` (UUID) directly.

**4.2 consumer/db.py**

Key functions:
- `get_sensor_by_device_address(device_id, address, sensor_type)` → sensor row or `None`
- `get_sensor_calibration(sensor_id)` → calibration row or `None` (consumer applies linear map to `raw_adc`)
- `insert_reading(sensor_id, value, raw_adc, recorded_at)`
- `mark_event_processed(event_id, event_type, command_id=None)` → `True` if inserted, `False` if duplicate (used for both `reading_id` and watering `event_id`)
- `get_alert_configs(sensor_id)` → list of `sensor_alert_config` rows
- `recent_alert_exists(sensor_id, alert_type, minutes)` → dedup check (`WHERE sent_at > now() - make_interval(mins => $3)`)
- `get_watering_config(plant_id)` → mode, thresholds, relay config
- `has_active_watering_command(plant_id)` → True when pending or dispatched
- `last_successful_watering_at(plant_id)` → cooldown check
- `try_create_pending_watering_command(plant_id, command_id, relay_channel, expires_at, ...)` → `bool` — single transaction: `SELECT ... FOR UPDATE` on the plant row, check for active command, then insert both `watering_events` (with `expires_at` mirrored from the command payload) and `command_outbox` together; returns `False` if an active command already exists; no separate enqueue step, no compensation path. The FOR UPDATE on the plant row is the optimistic-concurrency guard: two Consumer tasks replaying the same dry-soil reading simultaneously will serialize here — the second task's `has_active_watering_command` check sees the first task's insert and returns `False`
- `update_watering_event(command_id, status, *, started_at=None, completed_at=None)` → `bool` — guarded update; applies only while current status is `pending` or `dispatched`, so duplicate terminal events never overwrite a prior final status; returns `True` only when the row changed; `started_at` is only written when provided (set from `watering_started` event's `recorded_at`)
- `update_device_status(device_id, status)` → updates `devices.status` + `last_seen_at`
- `insert_dead_letter(topic, raw_payload, error, error_kind)` → records a malformed message for offline diagnosis

**4.3 consumer/alerts.py**

DB-driven thresholds — no hardcoded values:

```python
async def check_thresholds(sensor_id: int, sensor_type: str, value: float):
    configs = await db.get_alert_configs(sensor_id)
    for cfg in configs:
        triggered = (
            (cfg.alert_type == 'too_low'  and value < cfg.threshold) or
            (cfg.alert_type == 'too_high' and value > cfg.threshold)
        )
        if not triggered:
            continue
        # cooldown_min is minutes — passing it through `// 60` would silently
        # drop sub-hour cooldowns to zero and re-fire on every reading.
        if await db.recent_alert_exists(sensor_id, cfg.alert_type, minutes=cfg.cooldown_min):
            continue
        await db.create_alert(sensor_id, cfg.alert_type, value)
        await notify_telegram(format_alert(sensor_type, cfg.alert_type, value))
```

**4.4 consumer/auto_water.py**

```python
async def maybe_auto_water(plant_id: int, sensor_type: str, value: float):
    if sensor_type != "moisture":
        return
    config = await db.get_watering_config(plant_id)
    if config.mode != "auto" or value >= config.auto_threshold:
        return
    if await db.has_active_watering_command(plant_id):
        return
    if await db.watered_within(plant_id, minutes=config.min_interval_min):
        return

    command_id = str(uuid4())
    expires_at = datetime.now(UTC) + timedelta(minutes=5)

    # try_create_pending_watering_command creates both watering_events and command_outbox
    # rows in a single transaction. There is no separate enqueue step and no compensation
    # path — either both rows exist or neither does.
    created = await db.try_create_pending_watering_command(
        plant_id=plant_id, device_id=config.device_id,
        command_id=command_id, triggered_by="auto",
        duration_sec=config.duration_sec,
        relay_channel=config.relay_channel,
        expires_at=expires_at,
    )
    if not created:
        return   # concurrent guard fired (SELECT FOR UPDATE), skip silently
```

**4.5 consumer/event_handler.py**

```python
async def process(event: dict):
    notify_text = None

    # Event idempotency and command status update must commit atomically.
    # If the Consumer crashes after marking an event processed but before updating
    # watering_events, the terminal acknowledgement would be lost forever.
    async with db.transaction():
        if not await db.mark_event_processed(
            event_id=event["event_id"],
            command_id=event.get("command_id"),
            event_type=event["event"],
        ):
            return  # duplicate

        match event["event"]:
            case "watering_started":
                # Pi publishes this immediately before activating the relay.
                # Sets started_at to actual relay-execution time, not broker-delivery time.
                await db.update_watering_event(
                    command_id=event["command_id"], status="dispatched",
                    started_at=event["recorded_at"])
                return  # no Telegram notification for started

            case "watering_completed":
                changed = await db.update_watering_event(
                    command_id=event["command_id"], status="completed",
                    completed_at=datetime.now(UTC))
                if changed:
                    notify_text = (
                        f"✅ Plant {event['plant_id']} watered for "
                        f"{event['duration_sec']}s"
                    )

            case "watering_failed":
                changed = await db.update_watering_event(
                    command_id=event["command_id"], status="failed",
                    completed_at=datetime.now(UTC))
                if changed:
                    notify_text = f"❌ Watering failed — {event['error']}"

            case "command_expired":
                await db.update_watering_event(
                    command_id=event["command_id"], status="expired",
                    completed_at=datetime.now(UTC))

    # Side effects happen after the DB transaction commits. A Telegram failure
    # must not roll back or repeat the command lifecycle update.
    if notify_text:
        await notify_telegram(notify_text)
```

**4.6 consumer/metrics.py — Prometheus instrumentation**

Lightweight in-process metrics, exposed at `GET /metrics` on the same aiohttp server as `/health` (port 8002). No Prometheus container in compose — scrape externally (Grafana Cloud free tier, or skip entirely until something breaks). The whole instrumentation pass costs <10 MB RAM and gives operators a real signal source instead of guessing from logs.

```python
# consumer/metrics.py
from prometheus_client import Counter, Histogram, Gauge

# Message processing
mqtt_messages_total       = Counter("mqtt_messages_total", "Messages received", ["topic_suffix"])
mqtt_dead_letters_total   = Counter("mqtt_dead_letters_total", "Malformed messages", ["error_kind"])
mqtt_unknown_sensor_total = Counter("mqtt_unknown_sensor_total",
                                    "Well-formed readings for unregistered (device, address, type) tuples",
                                    ["device_id"])
# DB
db_insert_seconds         = Histogram("db_insert_seconds", "Reading insert latency")
# Outbox
command_outbox_pending    = Gauge("command_outbox_pending", "Rows in command_outbox with status=pending")
# Commands
watering_commands_total   = Counter("watering_commands_total", "Watering commands by terminal status",
                                    ["status"])  # completed | failed | expired
```

Each service (Consumer, Dispatcher, API, Bot) exposes its own `/metrics` from its existing health-server. Apply the same pattern in `cmd_dispatcher` (`mqtt_publish_seconds`, `outbox_attempts_total`) and `api` (FastAPI has `prometheus-fastapi-instrumentator` if you want auto-instrumentation).

`mqtt_unknown_sensor_total` is a labeled counter for readings that arrive for a `(device_id, address, sensor_type)` tuple that has no `sensors` row. Pi only publishes channels in `SENSOR_CONFIG`, so an unknown reading almost always means a misconfiguration or a not-yet-seeded sensor row — both worth seeing without grepping logs. The counter is incremented inside `process_reading` alongside the dead-letter write (see Phase 4.5).

**4.7 consumer/cleanup.py**

```python
async def expire_stale_commands():
    # Expire by the same expires_at the Pi sees. Using a hardcoded
    # "requested_at < now() - 10 minutes" instead would let the DB diverge
    # from Pi reality for any command with a non-default TTL (e.g. manual
    # waterings with longer expiry windows).
    #
    # 130s grace = MAX_WATERING_DURATION_SEC (120) + 10s margin. The Pi checks
    # expires_at only BEFORE starting the relay, so a watering that starts 1s
    # inside the window legitimately completes up to ~120s after it. Expiring
    # exactly at expires_at would race that in-flight run: 'expired' is
    # absorbing, the later watering_completed couldn't correct it, and the
    # cooldown check (last_successful_watering_at) would miss a real watering —
    # letting auto-water fire again → double watering.
    await db.execute("""
        UPDATE watering_events
        SET status = 'expired', completed_at = now()
        WHERE status IN ('pending', 'dispatched')
          AND expires_at + INTERVAL '130 seconds' < now()
    """)
    # Prune tables that have no TimescaleDB retention policy and grow unboundedly.
    # processed_sensor_events: ~8 rows/min = ~4M rows/year at steady state.
    await db.execute("""
        DELETE FROM processed_sensor_events
        WHERE processed_at < now() - INTERVAL '30 days'
    """)
    # command_outbox: keep terminal rows for 30 days for audit, then drop.
    await db.execute("""
        DELETE FROM command_outbox
        WHERE status IN ('dispatched', 'failed')
          AND created_at < now() - INTERVAL '30 days'
    """)
    # alerts: keep acknowledged alerts for 90 days.
    await db.execute("""
        DELETE FROM alerts
        WHERE acknowledged = true
          AND sent_at < now() - INTERVAL '90 days'
    """)
    # dead_letters: 30-day window for diagnosis. If something's still broken
    # after 30 days, the field-debug window has long passed.
    await db.execute("""
        DELETE FROM dead_letters
        WHERE created_at < now() - INTERVAL '30 days'
    """)
```

**`notify_telegram` helper** — single `aiohttp.ClientSession` created at process startup. Log non-2xx and connection errors so silently-dropped alerts are recoverable from logs. No retry — Telegram 429s would compound the problem.

```python
async def notify_telegram(text: str) -> None:
    try:
        resp = await _http_session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": OWNER_CHAT_ID, "text": text},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if resp.status >= 400:
            body = await resp.text()
            logger.warning("telegram send failed status=%s body=%s text=%r",
                           resp.status, body[:200], text)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("telegram send error: %s text=%r", e, text)
```

**Server-side calibration** — `consumer/calibration.py`:

```python
# Sensor-type fallback defaults if no calibration row exists for an ADC sensor.
SENSOR_TYPE_DEFAULTS = {
    "moisture":    {"raw_dry": 720, "raw_wet": 300},   # capacitive in air vs. water
    "water_level": {"raw_dry": 1023, "raw_wet": 0},    # tank empty vs. full
    "light":       {"raw_dry": 0, "raw_wet": 1023},    # linear LDR mapping
}

def calibrated_value(raw_adc: int, sensor_type: str, cal_row) -> float:
    raw_dry = cal_row.raw_dry if cal_row else SENSOR_TYPE_DEFAULTS[sensor_type]["raw_dry"]
    raw_wet = cal_row.raw_wet if cal_row else SENSOR_TYPE_DEFAULTS[sensor_type]["raw_wet"]
    # Linear map raw_dry → 0%, raw_wet → 100%. Clamp to [0, 100].
    pct = 100.0 * (raw_dry - raw_adc) / (raw_dry - raw_wet)
    return max(0.0, min(100.0, pct))
```

**Process reading** — ties it all together:

```python
async def process_reading(device_id: str, msg: ReadingMessage):
    # Resolve the sensor FIRST. If it isn't registered, dead-letter the payload
    # for manual replay and DO NOT mark as processed — leaving the idempotency
    # row open means a future delivery (e.g. after the sensor is seeded into
    # the DB) can still be inserted normally. The dead-letter row preserves the
    # raw payload so an operator can backfill if needed.
    sensor = await db.get_sensor_by_device_address(
        device_id, msg.address, msg.sensor_type)
    if not sensor:
        mqtt_unknown_sensor_total.labels(device_id=device_id).inc()
        await db.insert_dead_letter(
            topic=f"plant-monitor/{device_id}/readings",
            raw_payload=msg.model_dump_json(),
            error=f"no sensors row for address={msg.address} sensor_type={msg.sensor_type}",
            error_kind="unknown_sensor",
        )
        return

    # Idempotency mark and reading insert must be a single transaction. A crash
    # after mark_event_processed but before insert_reading would permanently
    # lose the reading — the idempotency table would block all future redeliveries.
    async with db.transaction():
        if not await db.mark_event_processed(
                msg.reading_id, event_type="sensor_reading"):
            return  # duplicate delivery, already processed

        # Pi sends raw_adc for ADC sensors and pre-computed value for digital
        # sensors (DHT22). Apply server-side calibration only when raw_adc is
        # present AND value is null — this keeps recalibration a backend-only
        # operation (no Pi re-deploy) and removes any need for Pi to read the DB.
        if msg.value is None and msg.raw_adc is not None:
            cal = await db.get_sensor_calibration(sensor.id)   # may be None
            value = calibrated_value(msg.raw_adc, msg.sensor_type, cal)
        else:
            value = msg.value

        await db.insert_reading(
            sensor_id=sensor.id, value=value,
            raw_adc=msg.raw_adc, recorded_at=msg.recorded_at)

    # Post-transaction: threshold checks and auto-water run outside the reading transaction
    # so a slow Telegram call or second DB query doesn't hold the write lock.
    await check_thresholds(sensor.id, msg.sensor_type, value)

    if sensor.plant_id:
        await maybe_auto_water(sensor.plant_id, msg.sensor_type, value)
```

> The earlier "mark unknown sensors as processed to avoid re-processing" comment was wrong: with MQTT QoS 1, the broker drops a message once Consumer acks it, regardless of whether we wrote to `processed_sensor_events`. The idempotency table only protects against Pi-side outbox redeliveries — which we *want* to succeed once the sensor row exists. Dead-lettering preserves the raw payload for manual replay.

---

### Phase 5 — FastAPI

**5.1 api/auth.py — Telegram WebApp validation**

```python
import hashlib, hmac, json, time
from urllib.parse import parse_qsl

# 5 minutes — short enough to block captured-initData replay attacks. Telegram
# regenerates initData only when the Mini App is (re)opened, NOT while it stays
# open — so initData is verified ONCE at session creation (POST /auth/session)
# and exchanged for a session token. Validating initData on every request would
# 401 any dashboard left open longer than 5 minutes.
MAX_AUTH_AGE_SECONDS = 300
SESSION_TTL_SECONDS  = 24 * 3600   # session token lifetime

def verify_telegram_init_data(init_data: str, bot_token: str, allowed_user_id: int) -> bool:
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return False

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        return False

    auth_date = int(parsed.get("auth_date", "0"))
    if time.time() - auth_date > MAX_AUTH_AGE_SECONDS:
        return False

    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        return False

    return user.get("id") == allowed_user_id


# --- Session tokens ---------------------------------------------------------
# initData is verified ONCE (POST /auth/session) and exchanged for a signed,
# stateless session token; every other endpoint validates the Bearer token.
# HS256 via PyJWT. SESSION_SECRET is a dedicated env var (openssl rand -hex 32)
# so the token-signing key is independent of the bot token.
import jwt

def create_session_token(user_id: int, secret: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + SESSION_TTL_SECONDS},
        secret, algorithm="HS256")

def verify_session_token(token: str, secret: str, allowed_user_id: int) -> bool:
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"])   # enforces exp
    except jwt.InvalidTokenError:
        return False
    return claims.get("sub") == str(allowed_user_id)
```

`POST /auth/session` reads `X-Telegram-Init-Data`, calls `verify_telegram_init_data`, and returns `{"token": ...}`. A FastAPI dependency (`require_session`) guards every other router by validating `Authorization: Bearer <token>`. The Mini App keeps the token in memory and re-exchanges `initData` on any 401; if that exchange also fails (initData older than 5 minutes), it prompts the user to reopen the Mini App — which regenerates initData.

**5.2 api/main.py — CORS**

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[f"https://{os.environ['DOMAIN']}"],
    allow_methods=["GET", "POST", "PATCH", "PUT"],   # PUT: calibration endpoint
    allow_headers=["X-Telegram-Init-Data", "Authorization"],
)
```

**5.3 api/routers/watering.py**

`POST /plants/{id}/water`:
1. Check plant exists
2. Verify mode is `manual` (reject if `auto`)
3. Read `device_id`, `duration_sec`, `relay_channel` from `watering_config`
4. Generate `command_id` (UUID4)
5. Call `db.try_create_pending_watering_command(...)` — same function used by auto-water; handles the active-command check and both inserts atomically via `SELECT FOR UPDATE`; returns `False` → 409 Conflict
6. Return `{ command_id, status: "pending" }`

`GET /health`:
- DB connectivity check; return 200 when ready.

---

### Phase 6 — Telegram Bot (aiogram)

**6.1 Webhook + health endpoint**

aiogram does not expose `/health` out of the box — its webhook handler lives on an `aiohttp.web.Application`. The Docker healthcheck in Phase 8 hits `http://localhost:8001/health`, so a `/health` route must be added to the same aiohttp app that serves the webhook. Without this, the bot container is always marked unhealthy and dependent services (Nginx) won't start.

```python
# bot/main.py
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

async def health(request):
    # Cheap liveness probe — DB ping is optional; presence is enough for Docker.
    return web.json_response({"status": "ok"})

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher()
    # ... register handlers ...

    app = web.Application()
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    await bot.set_webhook(f"https://{DOMAIN}/webhook")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=8001)
    await site.start()
    await asyncio.Event().wait()   # run until cancelled
```

**6.2 /status handler**
```
🌿 Plant Status

🪴 Monstera (Kitchen)
   💧 Moisture: 34% ⚠️ Low
   🌡 Temperature: 22°C  💧 Humidity: 65%
   🔄 Mode: AUTO
   💦 Last watered: 3 hours ago

🌵 Cactus (Bedroom)
   💧 Moisture: 67% ✅
   🔄 Mode: MANUAL

🪣 Water tank: 72%  ☀️ Light: 450 lux
```

**6.3 Mini App button**
```python
InlineKeyboardButton(
    text="🌿 Open Dashboard",
    web_app=WebAppInfo(url=f"https://{DOMAIN}/app")
)
```

**6.4 /water command**

Only works in MANUAL mode — calls same logic as API.

**6.5 Push notifications** (sent by Consumer directly via HTTP — Bot service not involved):
- `⚠️ Monstera is too dry! Moisture: 18%`
- `⚠️ Water tank is low! Level: 12%`
- `✅ Monstera watered for 10 seconds`
- `❌ Failed to water Ficus — relay not responding`

---

### Phase 7 — Mini App (React)

**7.1 Init**
```bash
npm create vite@latest mini-app -- --template react-ts
npm install @twa-dev/sdk recharts
```

**7.2 Telegram SDK**
```typescript
import WebApp from '@twa-dev/sdk'
WebApp.ready()
WebApp.expand()
// Exchanged ONCE via POST /auth/session for a 24h Bearer token — initData is
// only regenerated on app (re)open, so it must not be sent per-request.
const initData = WebApp.initData
```

**7.3 Pages**

`Dashboard.tsx`:
- Grid of `SensorCard` components — generic, handles any sensor type
- Each plant card: moisture %, mode badge, last watered time
- Device-level cards: water tank %, ambient light
- Tap plant → `PlantDetail`

`PlantDetail.tsx`:
- `SensorChart` — Recharts line chart, sensor type selector (moisture / temperature / humidity)
- Time range: 24h / 7d / 30d — uses `readings_hourly` aggregate for >24h
- `ModeToggle` — AUTO / MANUAL → `PATCH /plants/{id}/watering/config`
- `WateringButton` — disabled in AUTO mode; shows `CommandStatus` after trigger
- Status transitions: `⏳ Pending` → `📡 Dispatched` → `✅ Done` | `❌ Failed`

`Settings.tsx`:
- Plant names and locations
- Moisture auto-water threshold + watering duration per plant
- Alert thresholds (too_low / too_high) per sensor via `PATCH /sensors/{id}/alert-config`

---

### Phase 8 — Docker Compose (Final)

```yaml
# Bound container log size — default json-file driver grows unbounded and will
# fill the VPS disk over months. 10 MB × 3 files per service ≈ 210 MB worst case
# across 7 services. Use the YAML anchor on every service below.
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

services:
  timescaledb:
    image: timescale/timescaledb:latest-pg16
    logging: *default-logging
    environment:
      POSTGRES_DB: plantmonitor
      POSTGRES_USER: plantuser
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - timescaledb_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U plantuser -d plantmonitor"]
      interval: 10s
      timeout: 5s
      retries: 5

  # One-shot Alembic migration runner. Exits 0 on success; app services
  # gate on service_completed_successfully so they never see a stale schema.
  # The initdb.d mount is intentionally removed — it only fires on empty
  # volumes and silently does nothing on upgrades.
  migrate:
    build: ./migrations
    logging: *default-logging
    command: ["alembic", "upgrade", "head"]
    environment:
      DATABASE_URL: postgresql+asyncpg://plantuser:${DB_PASSWORD}@timescaledb/plantmonitor
    depends_on:
      timescaledb:
        condition: service_healthy
    restart: "no"

  mosquitto:
    image: eclipse-mosquitto:2
    logging: *default-logging
    ports:
      - "8883:8883"
    environment:
      MQTT_CONSUMER_PASSWORD: ${MQTT_CONSUMER_PASSWORD}
    volumes:
      - ./mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro
      - ./mosquitto/passwd:/mosquitto/config/passwd:ro
      - ./mosquitto/acl:/mosquitto/config/acl:ro
      - ./mosquitto/certs:/mosquitto/certs:ro
      - mosquitto_data:/mosquitto/data
    healthcheck:
      test: ["CMD-SHELL", "mosquitto_pub -h 127.0.0.1 -p 1883 -u backend_consumer -P $$MQTT_CONSUMER_PASSWORD -t 'health/check' -m '1' -q 0"]
      interval: 10s
      timeout: 5s
      retries: 5

  cmd_dispatcher:
    build: ./cmd_dispatcher
    logging: *default-logging
    environment:
      DATABASE_URL: postgresql+asyncpg://plantuser:${DB_PASSWORD}@timescaledb/plantmonitor
      MQTT_HOST: mosquitto
      MQTT_PORT: 1883
      MQTT_DISPATCHER_USERNAME: backend_dispatcher
      MQTT_DISPATCHER_PASSWORD: ${MQTT_DISPATCHER_PASSWORD}
    depends_on:
      timescaledb:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
      mosquitto:
        condition: service_healthy
    deploy:
      resources:
        limits:
          memory: 128M
    restart: always

  consumer:
    build: ./consumer
    logging: *default-logging
    environment:
      DATABASE_URL: postgresql+asyncpg://plantuser:${DB_PASSWORD}@timescaledb/plantmonitor
      MQTT_HOST: mosquitto
      MQTT_PORT: 1883
      MQTT_CONSUMER_USERNAME: backend_consumer
      MQTT_CONSUMER_PASSWORD: ${MQTT_CONSUMER_PASSWORD}
      BOT_TOKEN: ${BOT_TOKEN}
      OWNER_CHAT_ID: ${OWNER_CHAT_ID}
    depends_on:
      timescaledb:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
      mosquitto:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8002/health', timeout=3)\""]
      interval: 15s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits:
          memory: 256M
    restart: always

  api:
    build: ./api
    logging: *default-logging
    environment:
      DATABASE_URL: postgresql+asyncpg://plantuser:${DB_PASSWORD}@timescaledb/plantmonitor
      BOT_TOKEN: ${BOT_TOKEN}
      OWNER_CHAT_ID: ${OWNER_CHAT_ID}
      DOMAIN: ${DOMAIN}
      SESSION_SECRET: ${SESSION_SECRET}
    depends_on:
      timescaledb:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)\""]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits:
          memory: 256M
    restart: always

  bot:
    build: ./bot
    logging: *default-logging
    environment:
      DATABASE_URL: postgresql+asyncpg://plantuser:${DB_PASSWORD}@timescaledb/plantmonitor
      BOT_TOKEN: ${BOT_TOKEN}
      OWNER_CHAT_ID: ${OWNER_CHAT_ID}
      WEBHOOK_URL: https://${DOMAIN}/webhook
      DOMAIN: ${DOMAIN}
    depends_on:
      timescaledb:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8001/health', timeout=3)\""]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits:
          memory: 256M
    restart: always

  nginx:
    image: nginx:alpine
    logging: *default-logging
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf
      - ./mini-app/dist:/usr/share/nginx/html/app
      - /etc/letsencrypt:/etc/letsencrypt:ro
    depends_on:
      api:
        condition: service_healthy
      bot:
        condition: service_healthy

volumes:
  timescaledb_data:
  mosquitto_data:
```

**nginx.conf routing**
```
/app        → static Mini App files
/api        → FastAPI (port 8000)
/webhook    → Telegram Bot (port 8001)
```

**Estimated RAM at steady state**

| Service | RAM |
|---|---|
| TimescaleDB | 1.0–1.5 GB (tune `shared_buffers`) |
| Mosquitto | ~5 MB |
| Command Dispatcher | ~50 MB |
| Consumer | ~80 MB |
| FastAPI | ~80 MB |
| Bot | ~80 MB |
| Nginx | ~20 MB |
| **Total** | **~1.5–2 GB** |

> Add Redpanda when you have a functional need, not just more devices. MQTT + TimescaleDB can handle dozens of devices at this message rate (1 reading/min/sensor) without strain. Redpanda earns its operational cost when you need: (1) multiple independent consumers of the same event stream, (2) replayable event history for debugging or backfill, or (3) strict ingest isolation so a slow Consumer doesn't affect ingestion. Device count alone is not the trigger. The outbox and command patterns in the plan are already compatible with that future migration.

---

### Testing Strategy (cross-cutting)

Written alongside Phases 2–8, not as a final phase. The highest-defect-risk logic — calibration, auto-water guards, the command state machine, message validation — is pure Python with no hardware dependency: cheap to unit-test, expensive to debug live against a relay.

**Unit tests** — `pytest` + `pytest-asyncio`, `db` mocked at the module boundary:

| Target | Cases |
|---|---|
| `calibrated_value()` | normal map; clamping at 0 and 100; inverted range (light: `raw_wet > raw_dry`); missing calibration row → sensor-type defaults |
| `maybe_auto_water()` | mode=manual, value ≥ threshold, active command exists, cooldown not elapsed → all no-op; happy path → exactly one command created |
| `update_watering_event()` | terminal states absorbing (duplicate terminal event → `False`); `started_at` written only when provided |
| Consumer `dispatch()` | invalid UTF-8, bad JSON, wrong `schema_version`, Pydantic failure, topic/payload `device_id` mismatch → correct `dead_letters.error_kind`, handler NOT called; valid messages of all three types → correct handler |
| `check_thresholds()` | too_low / too_high trigger; cooldown suppression; no config → no alert |
| `verify_telegram_init_data()` + session tokens | known-good fixture passes; tampered hash, stale `auth_date`, wrong user id, expired token → all fail |
| Pi `handle_command()` | expired → `command_expired`; unknown relay → `watering_failed`; busy relay → `relay_busy`; oversize duration clamped (GPIO + publish mocked) |

**Integration harness** — `tests/integration/` against a local `docker compose up timescaledb mosquitto migrate consumer cmd_dispatcher`:

- `fake_pi.py` — paho-mqtt script that publishes reading/event fixtures into the real topic namespace and subscribes to `commands`
- Publish 3 readings → 3 rows in `sensor_readings` with calibrated values
- Publish the same `reading_id` twice → exactly 1 row (idempotency)
- Insert a `command_outbox` row → fake_pi receives the command; `watering_events` transitions to `dispatched`
- Publish `watering_completed` → terminal status set; duplicate event ignored
- `docker compose kill consumer`, publish while down, restart → queued messages replay, no duplicate rows
- Publish garbage payloads → `dead_letters` rows with correct `error_kind`; Consumer stays healthy

**Out of scope:** GPIO/SPI internals (require hardware — keep those functions thin and dumb), Telegram sends (already log-and-continue), TimescaleDB policy internals (verified once via the Phase 1 checkpoint).

---

### Phase 9 — ML (after 2–4 weeks of data)

> Do not start this phase until at least 2 weeks of sensor data is collected.

**9.1 Feature preparation (DuckDB)**

DuckDB as an analytical layer on top of TimescaleDB:
```python
import duckdb
conn = duckdb.connect()
conn.execute("ATTACH 'postgresql://...' AS db (TYPE postgres)")

features = conn.execute("""
    SELECT
        s.plant_id,
        st.code AS sensor_type,
        avg(sr.value) OVER (
            PARTITION BY sr.sensor_id
            ORDER BY sr.recorded_at
            ROWS BETWEEN 1440 PRECEDING AND CURRENT ROW
        ) AS avg_24h,
        avg(sr.value) OVER (
            PARTITION BY sr.sensor_id
            ORDER BY sr.recorded_at
            ROWS BETWEEN 10080 PRECEDING AND CURRENT ROW
        ) AS avg_7d
    FROM db.sensor_readings sr
    JOIN db.sensors s      ON sr.sensor_id = s.id
    JOIN db.sensor_types st ON s.sensor_type_id = st.id
    WHERE st.code = 'moisture'
""").df()
```

**9.2 Watering prediction (scikit-learn)**

Goal: predict hours until moisture drops below threshold.  
Model: `GradientBoostingRegressor` or linear regression as baseline.  
Features: avg moisture 24h/7d, temperature, humidity, time of day, days since last watering.  
Endpoint: `GET /plants/{id}/predictions/next-watering`

**9.3 Anomaly detection**

Goal: detect broken/stuck sensors.  
Model: `IsolationForest` on per-sensor rolling statistics.  
Trigger: value unchanged >30 min, or outside physical range → `sensor_error` alert.

**9.4 Natural language bot (Claude API)**

```python
async def handle_chat(message: Message):
    plants_status  = await db.get_all_plants_status()
    recent_alerts  = await db.get_recent_alerts(hours=24)

    response = await claude.messages.create(
        model="claude-sonnet-5",
        system="You are a plant care assistant. Answer questions about the user's plants based on the data provided. Be concise and friendly.",
        messages=[{
            "role": "user",
            "content": f"Plant data: {json.dumps(plants_status)}\nRecent alerts: {json.dumps(recent_alerts)}\nUser question: {message.text}"
        }]
    )
    await message.answer(response.content[0].text)
```

---

## Secret Management

`.env` files are acceptable for a single-developer deployment but have three concrete risks to manage:

1. **Backups leak secrets** — `pg_dump` tarballs and Object Storage uploads don't contain `.env`, but if you back up the whole VPS directory naively they might. Keep `.env` out of backup scripts and never commit it to git (`.gitignore` it explicitly).
2. **`docker inspect` exposes env vars in plaintext** — anyone with Docker socket access sees all environment variables. On a single-user VPS this is usually fine; if you ever add a second SSH user, use Docker secrets instead:
   ```yaml
   secrets:
     db_password:
       file: ./secrets/db_password.txt
   # Then in service: environment: DB_PASSWORD_FILE=/run/secrets/db_password
   ```
3. **Log leakage** — never log the full environment (`os.environ`) at startup. Log only non-secret config keys (host, port, mode).

For v1, `.env` with strict file permissions (`chmod 600 .env`) and explicit `.gitignore` is the right trade-off. Upgrade to Docker secrets when a second person needs server access.

---

## Environment Variables

**`.env.example` (VPS — Docker Compose services)**
```bash
# Database
DB_PASSWORD=your_secure_password

# MQTT (Mosquitto) — separate creds enforce least privilege between services
MQTT_CONSUMER_PASSWORD=your_consumer_mqtt_password
MQTT_DISPATCHER_PASSWORD=your_dispatcher_mqtt_password

# Telegram
BOT_TOKEN=your_bot_token_from_botfather
OWNER_CHAT_ID=your_telegram_user_id   # get via @userinfobot
DOMAIN=yourdomain.com
SESSION_SECRET=   # openssl rand -hex 32 — signs Mini App session tokens (JWT HS256)

# Claude API (Phase 9 only)
ANTHROPIC_API_KEY=
```

**`pi_client/.env.example` (Raspberry Pi only)**
```bash
DEVICE_ID=8d1d55d1-2222-4444-8888-111111111111
MQTT_HOST=yourdomain.com
MQTT_PORT=8883
MQTT_USERNAME=pi_living_room_01
MQTT_PASSWORD=your_device_mqtt_password
MQTT_CA_FILE=/home/pi/plant-monitor/pi_client/ca.crt
# Comma-separated sensor definitions: type:pin/channel:sensor_type
# adc:N:sensor_type  — MCP3008 ADC channel N
# gpio:N:dht22       — DHT22 on GPIO pin N (produces temp + humidity)
SENSOR_CONFIG=adc:0:moisture,adc:1:moisture,adc:6:water_level,adc:7:light,gpio:4:dht22
```

---

## Implementation Order

```
Phase 0  →  Phase 1  →  Phase 3               →  Phase 2
(VPS)        (DB)       (Mosquitto + Dispatcher)  (Pi client)
                                   │
                        verify readings arrive in Consumer
                                   │
                                   ▼
                               Phase 4
                              (Consumer)
                                   │
                        verify data lands in TimescaleDB
                                   │
                                   ▼
                               Phase 5
                              (FastAPI)
                                   │
                        verify API responds correctly
                                   │
                                   ▼
                         Phase 6 + Phase 7
                        (Bot + Mini App)
                                   │
                        verify full end-to-end flow
                                   │
                                   ▼
                               Phase 8
                           (Docker Compose)
                                   │
                        collect data for 2–4 weeks
                                   │
                                   ▼
                               Phase 9
                                 (ML)
```

---

## Key Validation Checkpoints

| After Phase | Verify |
|---|---|
| 1 | TimescaleDB up, schema created, hypertable confirmed, `sensor_alert_config` table exists |
| 2 | `chronyc tracking` shows synchronised clock on Pi; Pi connects to Mosquitto over TLS; `mosquitto_sub` on broker confirms readings topic receives messages from manual `sensor_reader.py` run; reading_outbox empties after publish |
| 3 | Command Dispatcher picks up a test row inserted into `command_outbox` and publishes to MQTT; `watering_events` row transitions to `dispatched` |
| 4 | Consumer running; `SELECT COUNT(*) FROM sensor_readings` grows every 60s; device status updates in `devices` table; Consumer `/health` returns 200 |
| 5 | All API endpoints return correct data via curl; CORS header present on responses |
| 6 | `/status` returns plant + tank + temperature data in Telegram |
| 7 | Mini App opens in Telegram, sensor charts render for all types, watering button works end-to-end |
| 8 | All services start cleanly with `docker compose up`, SSL works, MQTT TLS connection from Pi succeeds |
| any code change | Unit tests green (`pytest`); integration harness passes against local compose before deploying Consumer / Dispatcher changes |
