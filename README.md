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
| 6 | Telegram Bot | Done — `@WateringKLBot` on webhooks, serving `/start`, `/status` and `/water`. Deployed and verified through the real webhook path. |
| 7 | Mini App | Not started. The bot's dashboard button stays hidden until `MINI_APP_URL` is set, rather than linking to a 404. |
| 8 | Nginx + TLS | Done — `https://wateringkl.org` serves the API behind a real Let's Encrypt certificate, with automated renewal. Log caps, resource limits and DB backups from Phase 8 are still outstanding. |

## Public endpoint

`https://wateringkl.org` → `92.5.47.47`. Domain registered at Cloudflare; DNS is
Cloudflare's, and the A record is **DNS-only (grey cloud), deliberately not proxied** —
proxying would break MQTT on 8883 and interfere with certificate renewal.

Only Nginx is exposed. The API, Consumer, Dispatcher, broker and database have no
published host ports and are reachable only on the Docker network.

| Path | Goes to |
|---|---|
| `/health` | API health check (unauthenticated) |
| `/api/…` | FastAPI on `api:8000` |
| `/webhook` | Telegram bot — **added in Phase 6** |
| `/app` | Mini App static files — **added in Phase 7** |

The last two are intentionally absent from `nginx/default.conf.template`: nginx resolves
`proxy_pass` hostnames at startup, so a block pointing at a container that doesn't exist
yet would stop nginx booting at all.

### Setting this up from scratch

1. Point an `A` record at the VPS.
2. Open 80 and 443 in the OCI security list. The host's iptables `INPUT` chain rejects
   everything but SSH and does *not* need changing — Docker-published ports are handled
   in the `FORWARD`/`DOCKER` chains and bypass `INPUT`. (Which is also why 8883 works.)
3. Set `DOMAIN` and `LETSENCRYPT_EMAIL` in `.env`, then run
   `./scripts/init-letsencrypt.sh`. Rehearse with `STAGING=1` first — production
   Let's Encrypt allows only 5 failures per hour.
4. `docker compose --profile web up -d nginx`. Nginx sits behind a compose profile so a
   plain `docker compose up -d` can never start it half-configured.

Two deviations from the plan document, both deliberate:

- The plan uses `certbot --nginx`. That plugin rewrites a *host* nginx's configuration,
  but nginx here runs in a container holding port 80, so renewals would need it stopped
  every 60 days. `init-letsencrypt.sh` uses the webroot method, which leaves nginx up.
- The script checks DNS against a public resolver (`dig @1.1.1.1`), not the local one. A
  resolver that queried the name *before* it was registered caches the NXDOMAIN for the
  zone's negative TTL — 30 minutes on a fresh Cloudflare zone — long after the record is
  live everywhere else. Let's Encrypt resolves from its own servers, so the local cache
  is irrelevant and actively misleading. Use
  `curl --resolve wateringkl.org:443:92.5.47.47 …` to test from a machine with a stale
  cache.

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
bot/              Telegram bot (aiogram) — /status, /water, on webhooks
nginx/            TLS termination and routing (compose profile "web")
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

## Backups

Nightly `pg_dump` to Oracle Object Storage (`plant-monitor-backups`), via
`scripts/backup-db.sh`.

**No credential lives on the VPS.** The script authenticates by *instance principal*:
the VM's identity is asserted by Oracle's metadata service, and an IAM policy grants the
`plant-monitor-vm` dynamic group — whose only member is this VM — permission to write to
that one bucket and nothing else. There is no key to steal if the box is compromised, and
access is revoked by deleting the policy.

Retention is a server-side Object Storage lifecycle rule (delete under `daily/` after 30
days) rather than script logic, so a broken script can't silently stop expiring dumps.
Three recent copies are also kept in `/var/backups/plant-monitor` for a fast restore; the
bucket is the real backup.

The script fails loudly rather than uploading something useless. It rejects a dump under
5 kB, runs `pg_restore --list` to prove the archive's table of contents actually parses
(a truncated dump fails here, not on the day you need it), and after uploading it re-reads
the object's size from Object Storage to confirm the bytes really landed.

### Restoring

TimescaleDB needs more than a plain `pg_restore` — skipping the pre/post steps corrupts
the hypertable catalog, and the restore *appears* to succeed before chunk routing starts
misbehaving:

```bash
oci os object get --auth instance_principal -bn plant-monitor-backups \
    --name daily/plantmonitor-<stamp>.dump --file restore.dump
createdb -U plantuser plantmonitor_restore
psql -U plantuser -d plantmonitor_restore -c "CREATE EXTENSION timescaledb;"
psql -U plantuser -d plantmonitor_restore -c "SELECT timescaledb_pre_restore();"
pg_restore -U plantuser -d plantmonitor_restore --no-owner restore.dump
psql -U plantuser -d plantmonitor_restore -c "SELECT timescaledb_post_restore();"
```

### How much space this needs

Effectively none, which is worth stating because it's counterintuitive. The whole
database is **under 1 MB** — `sensor_readings` holds ~2,000 rows in 400 kB. At 7,624
rows/day the uncompressed projection is ~548 MB/year, but compression kicks in at 30 days
and a 12-month retention policy drops older chunks, so it plateaus around 50–100 MB.
Thirty daily dumps sit comfortably inside the Always Free 20 GB Object Storage
allowance — roughly 0.15% of it.

The thing that *does* grow here is Docker build cache: every image rebuild adds layers.
`/etc/cron.d/plantmonitor-docker-prune` clears it weekly. Note it prunes **build cache
only** — `docker image prune -a` must never be used on this host, because
`certbot/certbot` shows zero containers (the renewal cron runs it with `--rm`) and would
be deleted along with the certificate renewal tooling.

### The bot

`@WateringKLBot`, on webhooks at `https://wateringkl.org/webhook`. Three design points
worth knowing:

- **It holds no database credentials.** Everything goes through the API over the Docker
  network, so the watering rules — mode check, in-flight-command guard, the plant-row
  lock — have one implementation rather than a third copy that could drift.
- **It signs its own session tokens** with `SESSION_SECRET` instead of calling
  `/auth/session`, which only a Mini App can satisfy (it validates Telegram initData).
  Both processes are server-side and equally trusted.
  `bot/tests/test_api_client.py` asserts what the bot mints is exactly what the API's
  verifier accepts, so the two can't drift apart silently.
- **`WEBHOOK_SECRET` is mandatory**, and the bot refuses to start without it. `/webhook`
  on a known domain is trivially guessable; Telegram echoes the secret in
  `X-Telegram-Bot-Api-Secret-Token` and aiogram returns 401 on a mismatch, so forged
  updates never reach a handler. Verified: a POST with no secret, and one with a wrong
  secret, are both rejected.

`OwnerOnlyMiddleware` is registered on the dispatcher rather than per-router, so a
handler added later cannot forget the owner check. Non-owner updates are answered with
200 (Telegram must not be told to retry) but dropped before any handler runs.

To drive the real webhook path without using Telegram, pipe a script into the container
so it can read `WEBHOOK_SECRET` from its own environment — the same trick the API smoke
test uses.

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
