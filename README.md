# Plant Monitor

IoT system for monitoring and watering up to 8 plants via a Raspberry Pi, with a Telegram Mini App as the interface. Backend runs on an Oracle Cloud Always Free VM; TimescaleDB stores sensor history, MQTT carries Pi <-> backend traffic.

Full architecture, database schema, MQTT topic contracts, and the phased build plan live in [`plant-monitor-plan.md`](plant-monitor-plan.md) — start there for design rationale. This README covers what's actually built and how to run it.

## Status

| Phase | What | Status |
|---|---|---|
| 0 | Oracle Cloud VPS, Docker | Done — running on `VM.Standard.E2.1.Micro` (1 OCPU/1GB), not the originally planned 6GB Ampere shape (unavailable — see plan's degradation notes). A retry script for upgrading later lives in `scripts/oci-retry-launch.ps1`. |
| 1 | TimescaleDB schema | Done — schema, hypertable, continuous aggregates, seed data applied via Alembic (`migrations/`). |
| 2 | Pi sensor reading | Partial — `pi_client/sensor_reader.py` reads the 6 wired moisture sensors and stages readings locally; not yet cut over to run continuously (see below). `watering_controller.py` (relay control) is blocked on relay hardware not being wired yet. |
| 3+ | Mosquitto, Consumer, API, Bot, Mini App | Not started. |

### A note on the Pi

The Pi already runs a pre-existing script, `pi_client`-unrelated: `/home/kwhatever/watering_system/monitor.py` (a systemd service, `watering-monitor.service`), logging the same 6 moisture sensors to CSV every 4 hours. It holds the SPI bus open continuously, so it **cannot run at the same time** as this repo's `pi_client/sensor_reader.py` / `plant-sensor.timer`. The cutover (stop the legacy service, enable the new timer) is intentionally deferred until Mosquitto + Consumer exist, so new readings have somewhere real to land instead of just queuing indefinitely. The legacy CSV history is worth importing into TimescaleDB once the API exists.

## Repo layout

```
migrations/       TimescaleDB schema + Alembic migrations
pi_client/        Code that runs on the Raspberry Pi (sensors, systemd units)
scripts/          One-off ops tooling (Oracle instance retry-launch script)
docker-compose.yml
plant-monitor-plan.md   Full design doc — architecture, schema, MQTT contracts, phased plan
```

## Local / VPS setup

1. Copy `.env.example` to `.env` and set a real `DB_PASSWORD`.
2. `docker compose up -d timescaledb`
3. `docker compose run --rm migrate` — applies the schema via Alembic.

## Pi setup

1. `cd pi_client && python3 -m venv venv && venv/bin/pip install -r requirements.txt`
2. Copy `pi_client/.env.example` to `pi_client/.env` and fill in real values (`DEVICE_ID` should stay stable once assigned — it's referenced by the future `devices` DB row).
3. Manual test run: `set -a && . ./.env && set +a && venv/bin/python3 sensor_reader.py`
4. Once Mosquitto/Consumer exist: install `pi_client/systemd/plant-sensor.{service,timer}` into `/etc/systemd/system/` and enable the timer.

Requires `chrony` installed and NTP-synchronized (`chronyc tracking` should show `Leap status: Normal`) — `sensor_reader.py` refuses to publish otherwise, since unsynchronized timestamps corrupt TimescaleDB's continuous aggregates.

## Linting & tests

`pip install -r requirements-dev.txt`, then:

- `ruff check .` from the repo root — covers `migrations/` and `pi_client/` without needing their separate runtime deps installed. `ruff check --fix .` applies safe auto-fixes.
- `pytest` — unit tests for `pi_client/` (sensor logic, outbox semantics, MQTT publish/drain — hardware like `spidev` is mocked, see `pi_client/tests/conftest.py`) and `migrations/` (the SQL-statement-splitting behavior in `0001_init.py`).

One-time per clone, to run ruff automatically on `git commit`: `git config core.hooksPath .githooks`.

## Secrets

Real credentials live only in `.env` and `pi_client/.env`, both gitignored. `.env.example` / `pi_client/.env.example` are templates with placeholder values — copy and fill in, don't commit the real files.
