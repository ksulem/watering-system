# Operations

Runbook and hard-won gotchas. Secrets are never written here — see the gitignored `.env`
files and the checklist in [`PROGRESS.md`](PROGRESS.md).

## Access

| Target | How |
|---|---|
| VPS | `ssh -i ~/.ssh/oci_e2_micro ubuntu@92.5.47.47` (Oracle `eu-frankfurt-1`, instance `kl-water-plant-app`) |
| Public site | `https://wateringkl.org` (Cloudflare Registrar + DNS, A record **DNS-only / grey cloud** — proxying breaks MQTT 8883 and ACME) |
| Pi (from Windows) | `plink -ssh -hostkey SHA256:NHCp1NMn/OMDPamyIJ/HhasZnMXOd3tentFppMFpeRE -pw <PI_PASSWORD> kwhatever@klwater.local` — always pass `-hostkey` so plink doesn't write the key to the Windows registry |
| Telegram bot | `@WateringKLBot` |
| OCI CLI (local) | `C:\oci-cli\Scripts\oci.exe` (not on PATH; short path avoids the long-path issue without a registry edit). On the VPS: `/usr/local/bin/oci`. |

## VPS layout

- `~/plant-monitor` on the VPS is **not a git repo** — only `docker-compose.yml`,
  `migrations/`, `mosquitto/`, `nginx/`, `scripts/`, `.env`. Deploy by `scp`.
- Containers: timescaledb, mosquitto, consumer, cmd_dispatcher, api, bot, nginx (profile
  `web`), migrate, certbot (run with `--rm`).
- Open ports (OCI security list): 22, 80, 443, 8883. Docker-published ports bypass the host
  iptables `INPUT` chain — only the security list matters.
- Memory with everything up: ~376 MiB available, ~300 MB swap used.

## Common procedures

- **New migration:** `scp` the `.sql` + `versions/*.py`, then
  `docker compose build migrate && docker compose run --rm migrate alembic upgrade head`
  (the migrate image bakes the files in — build is required).
- **Changed a bind-mounted file / nginx template:** `docker compose up -d --force-recreate <svc>`.
  Nginx renders its template at container start: `docker compose --profile web up -d --force-recreate nginx`.
- **Add an nginx `proxy_pass` block only after its container exists** — nginx resolves hosts
  at startup and won't boot otherwise. It `depends_on` bot and api being healthy.
- **Run a check inside a container without handling secrets:** `docker exec -i <container> python - < script.py`.
- **Live API smoke test:** `api/tests/smoke_live.py` piped via `docker exec -i`. It prints
  cleanup SQL — run it afterwards.
- **Moving secrets:** pipe `grep`ped lines over ssh; verify with char-counts, never print values.
  Remember the services read the **VPS** `.env`, not the local one.

## Scheduled jobs on the VPS

| Cron file | When | What |
|---|---|---|
| `/etc/cron.d/plantmonitor-backup` | 03:15 UTC daily | `scripts/backup-db.sh` → bucket `plant-monitor-backups` (namespace `frf5kwsmijo7`), `daily/` deleted after 30 days. Logs: `journalctl -t plantmonitor-backup`. |
| `/etc/cron.d/plantmonitor-certbot` | 04:00 | `certbot renew` + nginx reload. |
| `/etc/cron.d/plantmonitor-docker-prune` | Sun 04:30 | Build-cache prune only. |

Backups use instance-principal auth (dynamic group `plant-monitor-vm`, policy
`plant-monitor-backup`) — no credentials on the box.

## Gotchas

**Docker / VPS**
- **Never `docker image prune -a`** — it deletes the certbot image (no running container) and
  the migrate image. `docker system df` "reclaimable" is misleading; only build cache is real.
- Mosquitto `passwd` created via `docker run` is `600 root:root`; the broker runs as uid 1883 →
  `chmod 644` or it crash-loops with "Unable to open pwfile".
- Ubuntu 22.04's package is `docker-compose-v2`, not `docker-compose-plugin`.
- Disk is not a concern: DB steady state ~50–100 MB with compression + retention; 36 GB free.

**Database**
- **TimescaleDB restore** needs `timescaledb_pre_restore()` / `timescaledb_post_restore()`
  around `pg_restore`. Documented in `scripts/backup-db.sh`.
- asyncpg returns `jsonb` as `str`; writes need an explicit `$n::jsonb` cast.
- `watering_events` has no `error`/`event_id` columns — terminal columns are `status`,
  `started_at`, `completed_at`.
- `command_outbox` rows stay `dispatched` after success — that's final, not stuck.
- `try_create_pending_watering_command` is duplicated in `api/db.py` and `consumer/db.py`;
  `api/tests/test_command_sql.py` fails if they drift.

**MQTT**
- Use `aiomqtt` (2.5.1), not `asyncio-mqtt` (dead, breaks with paho-mqtt 2.x). aiomqtt's
  `client_id` param is `identifier`.
- Broker TLS cert uses an IP SAN (`92.5.47.47`); certs are gitignored — regenerate via README
  commands if lost.

**OCI CLI**
- Pass JSON via `file://`, written without BOM (`[System.IO.File]::WriteAllText(..., UTF8Encoding($false))`).
- Security-list update replaces the whole list; input JSON is camelCase while `get` output is kebab-case.
- `oci iam policy update` needs both `--statements` and `--version-date ""`.
- Object Storage lifecycle rules also need the `objectstorage-eu-frankfurt-1` service principal granted.
- `oci os object head` returns headers at top level: `--query '"content-md5"'`.

**DNS / TLS**
- A resolver that queried the domain before it existed caches NXDOMAIN (30 min). Test with
  `dig @1.1.1.1` or `curl --resolve wateringkl.org:443:92.5.47.47 ...`.
- `curl -I /health` returns 405 — the route is GET-only.

## Local dev environment notes

- Don't make Windows registry changes (long-path support, plink host-key caching) — confirm first.
- Always keep an English column next to Russian plant names in SQL/config (`plants.name_en`).
