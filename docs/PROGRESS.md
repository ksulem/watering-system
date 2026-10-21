# Progress

Snapshot as of **2026-10-01** (last recorded work: 2026-08-09). Design rationale lives in
[`plant-monitor-plan.md`](../plant-monitor-plan.md); how to operate things lives in
[`OPERATIONS.md`](OPERATIONS.md); physical wiring lives in [`HARDWARE.md`](HARDWARE.md).

## Phase status

| Phase | What | Status |
|---|---|---|
| 0 | Oracle Cloud VPS + Docker | **Done.** `VM.Standard.E2.1.Micro` (1 OCPU / 1 GB RAM + 2 GB swap), not the planned 6 GB Ampere A1 (no capacity). |
| 1 | TimescaleDB schema | **Done.** Migrations `0001_init`, `0002_seed`, `0003_relays` applied on the VPS. |
| 2 | Pi sensor reading | **Done.** `sensor_reader.py` on `plant-sensor.timer` (~70s cadence), 6 moisture sensors. |
| 2.4 | Pi watering controller | **Done 2026-08-09.** `watering_controller.py` as `watering-controller.service`; relay board wired; first real `water_now` completed in 10.24s vs `duration_sec=10`. `devices.status` now `online`. |
| 3 | Mosquitto + Command Dispatcher | **Done.** TLS on 8883, per-user ACLs, `cmd_dispatcher` polling `command_outbox`. |
| 4 | Consumer | **Done.** Readings/events/status → TimescaleDB, calibration, alerts, auto-water decision, disconnected-sensor detection. |
| 5 | FastAPI | **Done.** Docker-network only (via Nginx). Telegram auth is live; `smoke_live.py` 49/49. |
| 6 | Telegram bot | **Done.** `@WateringKLBot` on webhooks at `https://wateringkl.org/webhook`; `/start`, `/status`, `/water` verified. |
| 7 | Mini App (React) | **Not started.** No `/app` block in Nginx; `MINI_APP_URL` unset so the bot hides the button. |
| 8 | Nginx + TLS + ops | **Done.** Let's Encrypt cert (expires 2026-11-05, auto-renew cron dry-run verified), nightly DB backup to Object Storage (restore tested), log caps, weekly build-cache prune. |

## Uncommitted work (as of 2026-10-01)

The Phase 2.4 work is **deployed on the VPS and Pi but not committed to git** (branch `dev`):

- `migrations/003_relays.sql`, `migrations/versions/0003_relays.py`, `migrations/tests/test_0003_relays.py`
- `pi_client/relay.py`, `pi_client/watering_controller.py`, `pi_client/test_relay.py`
- `pi_client/systemd/watering-controller.service`, `pi_client/tests/test_watering_controller.py`
- edits to `api/tests/smoke_live.py`, `conftest.py`, `pi_client/.env.example`,
  `pi_client/requirements.txt`, `pi_client/tests/conftest.py`, `plant-monitor-plan.md`

At the time: 50 `pi_client` tests, 299 repo-wide, all passing.

## Not done / open items

1. **Mini App (Phase 7).** Build the React app, add an Nginx `/app` block *after* the
   container exists (Nginx won't boot if a `proxy_pass` host is missing), set `MINI_APP_URL`.
2. **Watering hardware.** Only the relay board is connected — no pumps, solenoids, tubing or
   tank. Firing a relay just clicks.
3. **Auto-watering.** All six `watering_config` rows are `mode='manual'` on purpose. Switch to
   `auto` (and set `auto_threshold`) only once pumps are attached and tested.
4. **Extra sensors.** DHT22 (temp/humidity on GPIO4), tank water-level (`adc:6`), light
   (`adc:7`) are planned in the schema/config scheme but not wired.
5. **Legacy CSV import.** `/home/kwhatever/watering_system/logs/moisture_log.csv` on the Pi has
   months of readings to import into TimescaleDB.
6. **`adc:0` (Coffee tree) glitches.** Intermittent near-zero reads (~1.5%) even after the SPI
   warm-up fix — possibly a loose connector. The Consumer's plausibility guard catches them.
7. **RAM headroom.** ~376 MiB available with all services. Adding the Mini App container is
   the next pressure point. `scripts/oci-retry-launch.ps1` can retry for a 6 GB A1 instance.
8. **Stale legacy file.** `/home/kwhatever/watering_system/test_relay.py` on the Pi uses old
   1–6 numbering. Correct copy is `~/plant-monitor/pi_client/test_relay.py`. Not deleted yet.
9. **README status table** was partly stale before this doc was written — keep this file as
   the source of truth for status.

## Moving to a new dev machine — checklist

Gitignored files that must be copied manually (securely, not via git):

- `.env` (repo root) — `BOT_TOKEN`, `OWNER_USER_ID`, Pi SSH creds (`PI_HOSTNAME`, `PI_USERNAME`, `PI_PASSWORD`)
- `pi_client/.env` — `DEVICE_ID` (`260922ba-03f3-4c20-a7e4-1d7dbf8d9f42`, never regenerate), MQTT creds
- `mosquitto/certs/` — CA + server key/cert (IP SAN cert; Let's Encrypt is separate, on the VPS)
- `mosquitto/passwd`
- SSH key `~/.ssh/oci_e2_micro` (VPS access) and `~/.ssh/oci_plant_monitor` (for the A1 retry script)
- `~/.oci/config` + its API key (OCI CLI)

Note the **VPS `.env` is the one the services actually read** — the local `.env` is only for
local tooling.

Then: `pip install -r requirements-dev.txt`, `git config core.hooksPath .githooks`, `pytest`.
