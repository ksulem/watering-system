# Plant Monitor

IoT system for monitoring and watering up to 8 plants via a Raspberry Pi, with a Telegram Mini App as the interface. Backend runs on an Oracle Cloud Always Free VM; TimescaleDB stores sensor history, MQTT carries Pi <-> backend traffic.

Full architecture, database schema, MQTT topic contracts, and the phased build plan live in [`plant-monitor-plan.md`](plant-monitor-plan.md) — start there for design rationale. This README covers what's actually built and how to run it.

## Status

| Phase | What | Status |
|---|---|---|
| 0 | Oracle Cloud VPS, Docker | Done — running on `VM.Standard.E2.1.Micro` (1 OCPU/1GB), not the originally planned 6GB Ampere shape (unavailable — see plan's degradation notes). A retry script for upgrading later lives in `scripts/oci-retry-launch.ps1`. |
| 1 | TimescaleDB schema | Done — schema, hypertable, continuous aggregates, seed data applied via Alembic (`migrations/`). |
| 2 | Pi sensor reading | Partial — `pi_client/sensor_reader.py` reads the 6 wired moisture sensors and publishes them through the live broker (verified end-to-end); not yet cut over to run continuously (see below). `watering_controller.py` (relay control) is blocked on relay hardware not being wired yet. |
| 3 | Mosquitto + Command Dispatcher | Done — self-signed CA (IP SAN, no domain yet), per-user ACLs, `cmd_dispatcher/` polling `command_outbox` (idle — nothing creates commands until Phase 5's API exists). |
| 4+ | Consumer, API, Bot, Mini App | Not started. |

### A note on the Pi

The Pi already runs a pre-existing script, `pi_client`-unrelated: `/home/kwhatever/watering_system/monitor.py` (a systemd service, `watering-monitor.service`), logging the same 6 moisture sensors to CSV every 4 hours. It holds the SPI bus open continuously, so it **cannot run at the same time** as this repo's `pi_client/sensor_reader.py` / `plant-sensor.timer`. The cutover (stop the legacy service, enable the new timer) is intentionally deferred until Consumer exists (Phase 4), so readings land in the DB instead of just sitting in Mosquitto's queue. The legacy CSV history is worth importing into TimescaleDB once the API exists.

### A note on `asyncio-mqtt`

The plan document specifies `asyncio-mqtt`, but that package was renamed upstream — 0.16.2 is its final release, and it's incompatible with `paho-mqtt` 2.x (crashes on `Client()` construction). This repo uses `aiomqtt`, the actively maintained successor with an identical API, in `pi_client/`, `cmd_dispatcher/`, and `requirements-dev.txt`.

## Repo layout

```
migrations/       TimescaleDB schema + Alembic migrations
pi_client/        Code that runs on the Raspberry Pi (sensors, systemd units)
cmd_dispatcher/   Polls command_outbox, publishes commands to MQTT
mosquitto/        Broker config + ACLs (certs/ and passwd are gitignored — see Secrets)
scripts/          One-off ops tooling (Oracle instance retry-launch script)
docker-compose.yml
plant-monitor-plan.md   Full design doc — architecture, schema, MQTT contracts, phased plan
```

## Local / VPS setup

1. Copy `.env.example` to `.env` and set real values for `DB_PASSWORD`, `MQTT_CONSUMER_PASSWORD`, `MQTT_DISPATCHER_PASSWORD`.
2. `docker compose up -d timescaledb`
3. `docker compose run --rm migrate` — applies the schema via Alembic.
4. Generate Mosquitto TLS certs and credentials (not committed — see Secrets):
   ```bash
   cd mosquitto/certs
   openssl genrsa -out ca.key 4096
   openssl req -new -x509 -days 3650 -key ca.key -out ca.crt -subj "/CN=PlantMonitor CA"
   openssl genrsa -out server.key 2048
   # Replace the IP below with your VPS's public IP (or a real domain's CN if you have one)
   openssl req -new -key server.key -out server.csr -subj "/CN=<VPS_IP>" -addext "subjectAltName=IP:<VPS_IP>"
   openssl x509 -req -days 3650 -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -copy_extensions copy
   cd ../..
   docker run --rm -v "$(pwd)/mosquitto:/mosquitto/config" eclipse-mosquitto:2 mosquitto_passwd -b -c /mosquitto/config/passwd pi_<device> '<password>'
   docker run --rm -v "$(pwd)/mosquitto:/mosquitto/config" eclipse-mosquitto:2 mosquitto_passwd -b /mosquitto/config/passwd backend_consumer '<password>'
   docker run --rm -v "$(pwd)/mosquitto:/mosquitto/config" eclipse-mosquitto:2 mosquitto_passwd -b /mosquitto/config/passwd backend_dispatcher '<password>'
   sudo chmod 644 mosquitto/passwd   # readable by the container's non-root mosquitto user
   ```
   Update `mosquitto/acl` with the real device username/UUID before starting.
5. `docker compose up -d mosquitto cmd_dispatcher`
6. Open port 8883 (and 22) in the VPS's security list/firewall — nothing else needs to be externally reachable yet.

## Pi setup

1. `cd pi_client && python3 -m venv venv && venv/bin/pip install -r requirements.txt`
2. Copy `pi_client/.env.example` to `pi_client/.env` and fill in real values (`DEVICE_ID` should stay stable once assigned — it's referenced by the future `devices` DB row). Copy `mosquitto/certs/ca.crt` from the VPS setup to the path referenced by `MQTT_CA_FILE`.
3. Manual test run: `set -a && . ./.env && set +a && venv/bin/python3 sensor_reader.py`
4. Once Consumer exists (Phase 4): install `pi_client/systemd/plant-sensor.{service,timer}` into `/etc/systemd/system/` and enable the timer — this is also when the legacy `monitor.py` gets retired (see below).

Requires `chrony` installed and NTP-synchronized (`chronyc tracking` should show `Leap status: Normal`) — `sensor_reader.py` refuses to publish otherwise, since unsynchronized timestamps corrupt TimescaleDB's continuous aggregates.

## Linting & tests

`pip install -r requirements-dev.txt`, then:

- `ruff check .` from the repo root — covers `migrations/` and `pi_client/` without needing their separate runtime deps installed. `ruff check --fix .` applies safe auto-fixes.
- `pytest` — unit tests for `pi_client/` (sensor logic, outbox semantics, MQTT publish/drain — hardware like `spidev` is mocked, see `pi_client/tests/conftest.py`) and `migrations/` (the SQL-statement-splitting behavior in `0001_init.py`).

One-time per clone, to run ruff automatically on `git commit`: `git config core.hooksPath .githooks`.

## Secrets

Real credentials live only in `.env`, `pi_client/.env`, `mosquitto/certs/` (CA + server key/cert), and `mosquitto/passwd` — all gitignored. `.env.example` / `pi_client/.env.example` are templates with placeholder values — copy and fill in, don't commit the real files. `mosquitto/certs/` and `mosquitto/passwd` have no example counterpart — regenerate them with the commands in "Local / VPS setup" instead.
