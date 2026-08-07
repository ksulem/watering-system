# Plant Monitor

IoT system for monitoring and watering up to 8 plants via a Raspberry Pi, with a Telegram Mini App as the interface. Backend runs on an Oracle Cloud Always Free VM; TimescaleDB stores sensor history, MQTT carries Pi <-> backend traffic.

Full architecture, database schema, MQTT topic contracts, and the phased build plan live in [`plant-monitor-plan.md`](plant-monitor-plan.md) — start there for design rationale. This README covers what's actually built and how to run it.

## Status

| Phase | What | Status |
|---|---|---|
| 0 | Oracle Cloud VPS, Docker | Done — running on `VM.Standard.E2.1.Micro` (1 OCPU/1GB), not the originally planned 6GB Ampere shape (unavailable — see plan's degradation notes). A retry script for upgrading later lives in `scripts/oci-retry-launch.ps1`. |
| 1 | TimescaleDB schema | Done — schema, hypertable, continuous aggregates, seed data applied via Alembic (`migrations/`). |
| 2 | Pi sensor reading | Done — `pi_client/sensor_reader.py` runs on `plant-sensor.timer` every 60s, reading the 6 wired moisture sensors. `watering_controller.py` (relay control, plus the `status` topic that sets `devices.status`) is blocked on relay hardware not being wired yet. |
| 3 | Mosquitto + Command Dispatcher | Done — self-signed CA (IP SAN, no domain yet), per-user ACLs, `cmd_dispatcher/` polling `command_outbox` (idle — nothing creates commands until relays exist and a watering_config row is created). |
| 4 | Consumer | Done — `consumer/` validates and persists readings/events/status to TimescaleDB, applies server-side calibration, raises threshold alerts and decides auto-watering. Deployed and verified end-to-end: the Pi's readings land in `sensor_readings`. |
| 5 | FastAPI | Done — `api/` serves all plant/reading/watering/sensor/alert endpoints behind Telegram session auth. Deployed on the Docker network only (no published port). **Auth credentials are not set yet**, so it currently answers `/health` and locks everything else — see below. |
| 6+ | Bot, Mini App | Not started. |

### The API is deployed but locked

`BOT_TOKEN` and `OWNER_USER_ID` don't exist until the Telegram bot is created via
@BotFather (Phase 6). Until they're set in `.env`, `POST /auth/session` returns 503 and
every other endpoint returns 503 too — the API fails closed rather than falling open.
`SESSION_SECRET` is already generated on the VPS.

To finish wiring it up: create the bot, then set `BOT_TOKEN`, `OWNER_USER_ID` (your
numeric Telegram id, same as `OWNER_CHAT_ID` for a 1:1 chat) and restart
`docker compose up -d api consumer`. The same `BOT_TOKEN`/`OWNER_CHAT_ID` also switch on
the Consumer's Telegram alert pushes.

### A note on the Pi

The Pi used to run a pre-existing script, `pi_client`-unrelated: `/home/kwhatever/watering_system/monitor.py` (a systemd service, `watering-monitor.service`), logging the same 6 moisture sensors to CSV every 4 hours. It held the SPI bus open continuously, so it could not run alongside this repo's `sensor_reader.py`. **As of 2026-08-07 it is stopped and disabled** — the Phase 4 cutover — but the script and its CSV are left in place. That CSV history is worth importing into TimescaleDB once the API exists.

Its long-lived design also hid a bug that only surfaced once readings came from a 60-second one-shot: the first `xfer2` after a fresh `SpiDev.open()` returns ~0 regardless of the sensor, so whichever channel is read first would log a fabricated 100% moisture. `sensors/adc.py` now discards one conversion at import. See `test_spi_bus_is_warmed_up_at_import`.

### Device status is still `offline`

Nothing publishes to the `status` topic yet — that's `watering_controller.py`'s job (last-will + connect message), which is blocked on relay hardware. Until then `devices.status` stays `offline` and `last_seen_at` stays NULL even though readings are flowing. Liveness is observable from `sensor_readings.recorded_at` in the meantime.

### A note on `asyncio-mqtt`

The plan document specifies `asyncio-mqtt`, but that package was renamed upstream — 0.16.2 is its final release, and it's incompatible with `paho-mqtt` 2.x (crashes on `Client()` construction). This repo uses `aiomqtt`, the actively maintained successor with an identical API, in `pi_client/`, `cmd_dispatcher/`, and `requirements-dev.txt`.

## Repo layout

```
migrations/       TimescaleDB schema + Alembic migrations
pi_client/        Code that runs on the Raspberry Pi (sensors, systemd units)
consumer/         Subscribes to MQTT, validates payloads, writes to TimescaleDB,
                  calibrates readings, raises alerts, decides auto-watering
api/              FastAPI: plants, readings, watering, sensors, alerts
cmd_dispatcher/   Polls command_outbox, publishes commands to MQTT
mosquitto/        Broker config + ACLs (certs/ and passwd are gitignored — see Secrets)
scripts/          One-off ops tooling (Oracle instance retry-launch script)
docker-compose.yml
plant-monitor-plan.md   Full design doc — architecture, schema, MQTT contracts, phased plan
```

## Local / VPS setup

1. Copy `.env.example` to `.env` and set real values for `DB_PASSWORD`, `MQTT_CONSUMER_PASSWORD`, `MQTT_DISPATCHER_PASSWORD`. `BOT_TOKEN`/`OWNER_CHAT_ID` can stay blank until Phase 6 — the Consumer then records alerts without pushing them.
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
5. `docker compose up -d mosquitto consumer cmd_dispatcher api`
6. Open port 8883 (and 22) in the VPS's security list/firewall — nothing else needs to be externally reachable yet.

Migration `0002_seed` seeds the real device, the six plants and their `adc:0`–`adc:5`
moisture sensors. **The device UUID there must match `DEVICE_ID` in `pi_client/.env`
and the username in `mosquitto/acl`** — if they drift apart the Pi still connects and
publishes happily while every reading is dead-lettered as `unknown_sensor`, which looks
exactly like a healthy system with no data. `migrations/tests/test_0002_seed.py` asserts
all three stay in sync.

### Observing the Consumer

`consumer` serves `/health` and `/metrics` on port 8002, inside the Docker network only
(no host port published — scrape it from another container, or `docker exec`):

```bash
docker exec plant-monitor-consumer-1 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8002/metrics').read().decode())"
```

`mqtt_messages_total` counts deliveries and `db_insert_seconds_count` counts inserts;
the gap between them is QoS-1 redeliveries that `processed_sensor_events` deduplicated,
which is normal. A rising `mqtt_dead_letters_total` or `mqtt_unknown_sensor_total` is not
— inspect the `dead_letters` table, which keeps the raw payload for 30 days.

### Exercising the API against real data

The unit tests mock the database entirely, so they prove routing and validation but not
that the SQL matches the schema. `api/tests/smoke_live.py` closes that gap by running
every endpoint against the live database. It isn't part of the `pytest` run and isn't in
the Docker image — pipe it into a throwaway container that has auth configured:

```bash
docker compose run -d --no-deps --name api_smoke \
    -e BOT_TOKEN=smoke-not-a-real-bot -e OWNER_USER_ID=1 \
    -e SESSION_SECRET=$(openssl rand -hex 32) api
docker exec -i api_smoke python - < api/tests/smoke_live.py
docker rm -f api_smoke
```

It writes: a `__smoke_test__` plant, a watering config, a real watering command, one
alert config and one calibration row. It prints the cleanup SQL for everything it created
— run that afterwards. Note the watering command really does travel through the outbox,
the dispatcher and MQTT; with no `watering_controller.py` on the Pi nothing acts on it,
and the Consumer's cleanup task ages it to `expired` about two minutes later.

## Pi setup

1. `cd pi_client && python3 -m venv venv && venv/bin/pip install -r requirements.txt`
2. Copy `pi_client/.env.example` to `pi_client/.env` and fill in real values (`DEVICE_ID` should stay stable once assigned — it's referenced by the future `devices` DB row). Copy `mosquitto/certs/ca.crt` from the VPS setup to the path referenced by `MQTT_CA_FILE`.
3. Manual test run: `set -a && . ./.env && set +a && venv/bin/python3 sensor_reader.py`
4. Retire the legacy logger, then install the timer (done on this Pi 2026-08-07):
   ```bash
   sudo systemctl disable --now watering-monitor.service   # frees /dev/spidev0.0
   sudo cp ~/plant-monitor/pi_client/systemd/plant-sensor.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now plant-sensor.timer
   ```
   Both readers cannot run at once — they contend for the SPI bus. Expect a ~70s
   cadence rather than exactly 60s: systemd's default `AccuracySec` is 1 minute.

Requires `chrony` installed and NTP-synchronized (`chronyc tracking` should show `Leap status: Normal`) — `sensor_reader.py` refuses to publish otherwise, since unsynchronized timestamps corrupt TimescaleDB's continuous aggregates.

## Linting & tests

`pip install -r requirements-dev.txt`, then:

- `ruff check .` from the repo root — covers `migrations/`, `pi_client/` and `consumer/` without needing their separate runtime deps installed. `ruff check --fix .` applies safe auto-fixes.
- `pytest` — unit tests for `pi_client/` (sensor logic, outbox semantics, MQTT publish/drain), `consumer/` (payload validation, dispatch and dead-lettering, calibration, idempotency, alert cooldowns, auto-water guards), `api/` (initData HMAC and JWT handling, endpoint behaviour, and that the API's and Consumer's watering-command SQL stay identical) and `migrations/` (SQL-statement splitting, and that the seed stays consistent with the ACL and the Pi's env).

Neither `asyncpg` nor `aiohttp` is a dev dependency: like `spidev` on the Pi side, they're stubbed in the repo-root `conftest.py`, since every DB and outbound HTTP call under test is mocked and `asyncpg` ships no wheel for the newest CPython.

`api/tests/test_command_sql.py` deserves a note. Manual watering (API) and auto-watering (Consumer) are two independent writers racing for one physical relay, and they only serialize correctly if both take the same `SELECT ... FOR UPDATE` on the plant row. They live in separate Docker build contexts and can't share a module, so that test compares the two implementations' SQL structurally and fails if they drift apart.

One-time per clone, to run ruff automatically on `git commit`: `git config core.hooksPath .githooks`.

## Secrets

Real credentials live only in `.env`, `pi_client/.env`, `mosquitto/certs/` (CA + server key/cert), and `mosquitto/passwd` — all gitignored. `.env.example` / `pi_client/.env.example` are templates with placeholder values — copy and fill in, don't commit the real files. `mosquitto/certs/` and `mosquitto/passwd` have no example counterpart — regenerate them with the commands in "Local / VPS setup" instead.
