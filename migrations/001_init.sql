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
