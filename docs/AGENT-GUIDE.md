# Agent Guide: Connecting to the Pi, VPS and Database

For an LLM coding agent (Claude Code or similar) working on this repo. It covers how to reach
every running part of the system, how to inspect it safely, and what needs the user's OK
before you do it. For background read [`PROGRESS.md`](PROGRESS.md), [`OPERATIONS.md`](OPERATIONS.md)
and [`HARDWARE.md`](HARDWARE.md).

## 0. Ground rules

1. **Never print secrets.** No `cat .env`, no `echo $PASSWORD`, no `docker inspect` dumps
   of env. If you need to confirm a secret is set, print its length:
   `awk -F= '/^BOT_TOKEN=/{print length($2)}' .env`.
2. **Load secrets into the shell; don't paste them into commands.** Source the `.env` and
   refer to the variable (`"$PI_PASSWORD"`), so the value never shows up in your transcript.
3. **Reading is free; changing things needs the user's OK.** Without asking you may run
   SELECTs, read logs, check `systemctl status`/`docker ps`, and subscribe to MQTT.
   **Ask first** for any of these:
   - writing to the DB
   - restarting or recreating containers or systemd units
   - deploying files
   - running migrations
   - firing a relay
   - deleting anything
   - changing OCI/Cloudflare settings
4. **Never make Windows registry changes** (e.g. long-path support, PuTTY host-key cache)
   without asking in the moment.
5. **Never run `docker image prune -a` or `docker system prune -a` on the VPS.** They delete
   the certbot and migrate images.
6. **Never re-enable `watering-monitor.service` on the Pi.** It holds the SPI bus and silently
   starves the real sensor pipeline.
7. Plant names are Russian. Always show the English name next to them (`plants.name_en`).

## 1. Where the secrets are

All of these are gitignored. If they're missing, ask the user to copy them over. Don't recreate them.

| File | Contains |
|---|---|
| `.env` (repo root, local) | `PI_HOSTNAME`, `PI_USERNAME`, `PI_PASSWORD`, plus a partial copy of the bot vars |
| `~/plant-monitor/.env` **on the VPS** | The real service config: `DB_PASSWORD`, MQTT passwords, `BOT_TOKEN`, `OWNER_*`, `SESSION_SECRET`, `WEBHOOK_SECRET`, `DOMAIN`. **The services read this file, not the local one.** |
| `pi_client/.env` (local + on the Pi) | `DEVICE_ID`, MQTT creds for `pi_klwater` |
| `~/.ssh/oci_e2_micro` | SSH key for the VPS |
| `mosquitto/certs/`, `mosquitto/passwd` | Broker TLS and user database |

## 2. VPS (Oracle Cloud, Ubuntu 22.04, 1 GB RAM)

```bash
ssh -i ~/.ssh/oci_e2_micro ubuntu@92.5.47.47
cd ~/plant-monitor          # NOT a git repo; holds docker-compose.yml, .env, migrations/, mosquitto/, nginx/, scripts/
```

Non-interactive one-liners are best for agents:

```bash
VPS="ssh -i ~/.ssh/oci_e2_micro -o BatchMode=yes ubuntu@92.5.47.47"
$VPS 'cd ~/plant-monitor && docker compose ps'
$VPS 'free -m; df -h /'
$VPS 'cd ~/plant-monitor && docker compose logs --tail=100 consumer'
```

Services in `docker compose`: `timescaledb`, `mosquitto`, `consumer`, `cmd_dispatcher`, `api`,
`bot`, `nginx` (profile `web`, so add `--profile web` when acting on it), `migrate` (one-shot).

Health endpoints are only reachable from inside the containers:

```bash
$VPS 'cd ~/plant-monitor && docker compose exec -T api python -c "import urllib.request;print(urllib.request.urlopen(\"http://localhost:8000/health\").read())"'
# bot: localhost:8001/health   consumer: localhost:8002/health (and /metrics)
```

Public check from anywhere: `curl https://wateringkl.org/health`. It must be **GET**;
`curl -I` returns 405.

Memory is tight (about 376 MiB free). Check `free -m` before starting anything new.

## 3. Database (TimescaleDB / Postgres 16)

- DB `plantmonitor`, user `plantuser`. The password is `DB_PASSWORD` in the VPS `.env`.
- **Not reachable from the internet.** Compose publishes 5432, but the OCI security list only
  opens 22/80/443/8883. Two ways in:

**A. psql inside the container (simplest, no password needed):**

```bash
$VPS 'cd ~/plant-monitor && docker compose exec -T timescaledb psql -U plantuser -d plantmonitor -c "SELECT id, name, name_en FROM plants ORDER BY id;"'
```

For longer SQL, pipe a file in:
`$VPS 'cd ~/plant-monitor && docker compose exec -T timescaledb psql -U plantuser -d plantmonitor' < query.sql`

**B. SSH tunnel, for a local client or script:**

```bash
ssh -i ~/.ssh/oci_e2_micro -N -L 15432:127.0.0.1:5432 ubuntu@92.5.47.47
# then connect to localhost:15432; get DB_PASSWORD from the user or the VPS .env, and don't print it
```

**Tables:** `devices`, `plants` (`name` ru, `name_en`), `sensor_types`, `sensors` (`address` such as
`adc:0`), `sensor_calibration`, `sensor_alert_config`, `sensor_readings` (hypertable),
`readings_hourly` / `readings_daily` (continuous aggregates), `watering_config`
(`relay_channel`, `mode`), `watering_events`, `command_outbox`, `processed_sensor_events`,
`alerts`, `dead_letters`.

**Useful read-only queries:**

```sql
-- Is the Pi alive?
SELECT id, status, last_seen_at FROM devices;
-- Latest reading per sensor
SELECT DISTINCT ON (s.address) s.address, p.name_en, r.*
FROM sensor_readings r JOIN sensors s ON s.id = r.sensor_id LEFT JOIN plants p ON p.id = s.plant_id
ORDER BY s.address, r.recorded_at DESC;
-- Recent waterings and commands
SELECT * FROM watering_events ORDER BY started_at DESC LIMIT 10;
SELECT * FROM command_outbox ORDER BY created_at DESC LIMIT 10;
-- Problems
SELECT * FROM dead_letters ORDER BY 1 DESC LIMIT 20;
SELECT * FROM alerts ORDER BY 1 DESC LIMIT 20;
```

Column names above are from memory. Check with `\d <table>` before you rely on them.

**Gotchas:**
- `command_outbox.status='dispatched'` is the normal final state, not a stuck row.
- `watering_events` has no `error` or `event_id` columns.
- Schema changes go through Alembic (`migrations/versions/`), never ad-hoc DDL. The deploy
  steps are in [OPERATIONS.md](OPERATIONS.md).
- Restoring a backup requires `timescaledb_pre_restore()` / `timescaledb_post_restore()`.

## 4. Raspberry Pi (Pi 4B, Debian 13, user `kwhatever`)

The Pi is on the user's home LAN and is reached as `klwater.local` (mDNS). **It is only
reachable from a machine on the same network.** If the name doesn't resolve, ask the user for
the Pi's IP. Login is by password, from `PI_PASSWORD` in the local `.env`.

**Windows (PuTTY's plink/pscp, from Git Bash):**

```bash
set -a; . ./.env; set +a
HK='SHA256:NHCp1NMn/OMDPamyIJ/HhasZnMXOd3tentFppMFpeRE'
plink -ssh -batch -hostkey "$HK" -pw "$PI_PASSWORD" "$PI_USERNAME@$PI_HOSTNAME" 'systemctl status plant-sensor.timer watering-controller.service --no-pager'
pscp  -batch -hostkey "$HK" -pw "$PI_PASSWORD" pi_client/relay.py "$PI_USERNAME@$PI_HOSTNAME:plant-monitor/pi_client/"
```

Always pass `-hostkey`. Otherwise plink either aborts in `-batch` mode or caches the key in the
Windows registry (see rule 4).

**Linux / macOS:**

```bash
set -a; . ./.env; set +a
SSHPASS="$PI_PASSWORD" sshpass -e ssh "$PI_USERNAME@$PI_HOSTNAME" 'uptime'
```

Setting up an SSH key once (`ssh-copy-id`) is better. Ask the user first. Check the host key
against the SHA256 fingerprint above on first connect.

**On the Pi:**

| What | Where |
|---|---|
| Repo code | `~/plant-monitor/pi_client/` (own `venv`, own `.env`) |
| Sensor run | `plant-sensor.timer` → `plant-sensor.service` (one-shot, ~70s) |
| Relays + status | `watering-controller.service` (always on, systemd watchdog) |
| Local outbox | `~/plant-monitor/pi_client/executed_commands.db` (SQLite) |
| Legacy (do not touch) | `~/watering_system/` (old `monitor.py`, its CSV, a stale `test_relay.py`) |

```bash
journalctl -u watering-controller -n 100 --no-pager
journalctl -u plant-sensor -n 50 --no-pager
```

**Running the sensor script by hand:** stop the timer first, or the two runs fight over SPI. Then:
`cd ~/plant-monitor/pi_client && set -a && . ./.env && set +a && venv/bin/python3 sensor_reader.py`.
Re-enable the timer afterwards.

**Relays:** only the relay board is connected (no pumps), so firing a channel just clicks.
Even so, ask before firing one. Use `~/plant-monitor/pi_client/test_relay.py`, never the
legacy copy. Channel → GPIO mapping is in `pi_client/relay.py` and [HARDWARE.md](HARDWARE.md).

**`RPi.GPIO` here is the apt `python3-rpi-lgpio` shim.** Don't `pip install RPi.GPIO`.

## 5. MQTT broker (Mosquitto)

- External: `92.5.47.47:8883`, TLS with the project's own CA (`mosquitto/certs/ca.crt`,
  IP SAN). Only the Pi uses this.
- Internal: `mosquitto:1883`, plaintext, Docker network only.
- Users: `pi_klwater` (Pi), `backend_consumer` (read readings/events/status),
  `backend_dispatcher` (write commands). ACLs are in `mosquitto/acl`.
- Topics: `plant-monitor/260922ba-03f3-4c20-a7e4-1d7dbf8d9f42/{readings,events,status,commands}`.

To watch live traffic without handling a password (the consumer password is already in the
mosquitto container's env):

```bash
$VPS 'cd ~/plant-monitor && docker compose exec -T mosquitto sh -c '"'"'mosquitto_sub -h 127.0.0.1 -p 1883 -u backend_consumer -P "$MQTT_CONSUMER_PASSWORD" -t "plant-monitor/+/readings" -t "plant-monitor/+/events" -t "plant-monitor/+/status" -v -C 10'"'"
```

Don't publish to `commands` by hand. Watering commands should go through the API so the
outbox and the relay lock apply.

## 6. API and Telegram bot

- `https://wateringkl.org/api/...` needs a Telegram session token. Only a Mini App's initData
  or the bot can get one, so an agent can't simply curl it.
- To test against real data, run `api/tests/smoke_live.py` inside a container with
  `docker exec -i ... python -`. It **writes** test rows and prints cleanup SQL, so ask first
  and run the cleanup afterwards.
- Bot: `@WateringKLBot`, webhook `https://wateringkl.org/webhook`. Its logs:
  `docker compose logs --tail=100 bot`.

## 7. Oracle Cloud and Cloudflare

- OCI CLI: Windows `C:\oci-cli\Scripts\oci.exe` (not on PATH), config in `~/.oci/config`. On
  the VPS, `/usr/local/bin/oci` uses instance-principal auth.
- Region `eu-frankfurt-1`, instance `kl-water-plant-app`. Backups bucket
  `plant-monitor-backups` (namespace `frf5kwsmijo7`).
- Cloudflare: domain `wateringkl.org`. The A record must stay **DNS-only (grey cloud)**.
- CLI gotchas (`file://` JSON with no BOM, security-list updates replace the whole list, and so
  on) are in [OPERATIONS.md](OPERATIONS.md#gotchas).

## 8. Quick health sweep (all read-only)

```bash
curl -s https://wateringkl.org/health
$VPS 'cd ~/plant-monitor && docker compose ps && free -m | head -2'
$VPS 'cd ~/plant-monitor && docker compose exec -T timescaledb psql -U plantuser -d plantmonitor -c "SELECT status, last_seen_at FROM devices;" -c "SELECT max(recorded_at) FROM sensor_readings;" -c "SELECT count(*) FROM dead_letters;"'
# on the Pi:
systemctl is-active plant-sensor.timer watering-controller.service
```

If all of these pass, the system is healthy:
- `/health` answers.
- All containers are `Up`/`healthy`.
- `devices.status = 'online'`.
- The newest reading is under about 2 minutes old.
- Both Pi units are `active`.
