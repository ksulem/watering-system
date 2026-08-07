"""End-to-end smoke test against a REAL database and a REAL running API.

The unit tests mock `api.db` entirely, so they prove routing, validation and
status codes but say nothing about whether the SQL matches the schema. This
script closes that gap: every statement actually executes against TimescaleDB.

It is excluded from the Docker image (see api/.dockerignore) and is not part of
the pytest run — it needs live infrastructure. Pipe it into a throwaway API
container that has auth configured:

    docker compose run -d --name api_smoke \
        -e BOT_TOKEN=smoke-not-a-real-bot \
        -e OWNER_USER_ID=1 \
        -e SESSION_SECRET=$(openssl rand -hex 32) \
        --entrypoint uvicorn api \
        api.main:app --host 0.0.0.0 --port 8000
    docker exec -i api_smoke python - < api/tests/smoke_live.py

It WRITES to the database: it creates a plant named `__smoke_test__` plus a
watering config, a watering command, one alert config and one calibration row.
Everything it touches is printed at the end under CLEANUP so it can be removed.
Run the cleanup SQL afterwards — nothing here deletes on its own, because a
failed run is more useful with its wreckage left in place.
"""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

BASE = "http://localhost:8000"
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])

passed, failed = 0, 0
created = []


def check(label, got, want):
    global passed, failed
    ok = got == want
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}: expected {want}, got {got}")
    return ok


def call(method, path, *, token=None, body=None, headers=None):
    """Return (status, parsed_json_or_text)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


def make_init_data(user_id):
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAF_smoke",
        "user": json.dumps({"id": user_id, "first_name": "Smoke"},
                           separators=(",", ":")),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


print("\n=== health ===")
status, body = call("GET", "/health")
check("GET /health", status, 200)
check("db reachable", body.get("db"), True)

print("\n=== auth ===")
status, _ = call("GET", "/plants")
check("no token is rejected", status, 401)

status, _ = call("POST", "/auth/session",
                 headers={"X-Telegram-Init-Data": make_init_data(OWNER_USER_ID + 1)})
check("initData for another user is rejected", status, 401)

status, body = call("POST", "/auth/session",
                    headers={"X-Telegram-Init-Data": make_init_data(OWNER_USER_ID)})
check("POST /auth/session", status, 200)
TOKEN = body["token"] if status == 200 else ""
check("token has a 24h lifetime", body.get("expires_in"), 86400)

print("\n=== reads against seeded data ===")
status, plants = call("GET", "/plants", token=TOKEN)
check("GET /plants", status, 200)
check("six seeded plants", len(plants), 6)
if plants:
    names = {p["name_en"] for p in plants}
    check("English names present", "Monstera" in names, True)
    check("no relay assigned yet", all(p["mode"] is None for p in plants), True)

first_id = plants[0]["id"] if plants else 1
status, detail = call("GET", f"/plants/{first_id}", token=TOKEN)
check(f"GET /plants/{first_id}", status, 200)
check("plant detail carries its sensors", len(detail.get("sensors", [])) >= 1, True)

status, latest = call("GET", f"/plants/{first_id}/readings/latest", token=TOKEN)
check("GET readings/latest", status, 200)
check("latest reading has a value", latest and latest[0]["value"] is not None, True)

status, hist = call("GET", f"/plants/{first_id}/readings/history?limit=10", token=TOKEN)
check("GET readings/history", status, 200)
check("history returns rows", len(hist) > 0, True)

for bucket in ("1h", "1d"):
    status, agg = call(
        "GET", f"/plants/{first_id}/readings/aggregated?bucket={bucket}", token=TOKEN)
    check(f"GET readings/aggregated bucket={bucket}", status, 200)

status, _ = call("GET", f"/plants/{first_id}/readings/aggregated?bucket=1w", token=TOKEN)
check("unknown bucket is rejected", status, 422)

status, _ = call("GET", "/plants/999999", token=TOKEN)
check("missing plant is 404", status, 404)

print("\n=== sensors ===")
status, types = call("GET", "/sensors/types", token=TOKEN)
check("GET /sensors/types", status, 200)
check("five seeded sensor types", len(types), 5)

status, sensors = call("GET", "/sensors", token=TOKEN)
check("GET /sensors", status, 200)
check("six wired sensors", len(sensors), 6)
sensor_id = sensors[0]["sensor_id"] if sensors else 1

status, _ = call("GET", f"/sensors/{sensor_id}/calibration", token=TOKEN)
check("no calibration row yet", status, 404)

# Written with the Consumer's own moisture defaults so behaviour is unchanged.
status, cal = call("PUT", f"/sensors/{sensor_id}/calibration", token=TOKEN,
                   body={"raw_dry": 720, "raw_wet": 300, "notes": "smoke test"})
check("PUT calibration", status, 200)
if status == 200:
    created.append(f"sensor_calibration sensor_id={sensor_id}")
check("calibration round-trips", cal.get("raw_dry"), 720)

status, _ = call("PUT", f"/sensors/{sensor_id}/calibration", token=TOKEN,
                 body={"raw_dry": 500, "raw_wet": 500})
check("equal calibration endpoints rejected", status, 422)

# threshold -1 can never trigger (value < -1 is impossible for a clamped 0-100%).
status, ac = call("PATCH", f"/sensors/{sensor_id}/alert-config", token=TOKEN,
                  body={"alert_type": "too_low", "threshold": -1, "cooldown_min": 360})
check("PATCH alert-config", status, 200)
if status == 200:
    created.append(f"sensor_alert_config sensor_id={sensor_id}")

status, configs = call("GET", f"/sensors/{sensor_id}/alert-config", token=TOKEN)
check("GET alert-config", status, 200)
check("alert config persisted", len(configs), 1)

print("\n=== writes on a throwaway plant ===")
status, plant = call("POST", "/plants", token=TOKEN,
                     body={"name": "__smoke_test__", "name_en": "Smoke Test",
                           "location": "nowhere"})
check("POST /plants", status, 201)
tmp_id = plant.get("id")
if tmp_id:
    created.append(f"plants id={tmp_id}")

status, patched = call("PATCH", f"/plants/{tmp_id}", token=TOKEN,
                       body={"location": "still nowhere"})
check("PATCH /plants", status, 200)
check("patch applied", patched.get("location"), "still nowhere")
check("unpatched field preserved", patched.get("name"), "__smoke_test__")

device_id = os.environ.get("SMOKE_DEVICE_ID", "260922ba-03f3-4c20-a7e4-1d7dbf8d9f42")
status, _ = call("GET", f"/plants/{tmp_id}/watering/config", token=TOKEN)
check("no watering config yet", status, 404)

status, _ = call("PATCH", f"/plants/{tmp_id}/watering/config", token=TOKEN,
                 body={"mode": "auto"})
check("PATCH cannot create a config", status, 404)

# relay_channel 8 — nothing else claims it, and no relay is physically wired.
status, cfg = call("PUT", f"/plants/{tmp_id}/watering/config", token=TOKEN,
                   body={"device_id": device_id, "mode": "manual",
                         "relay_channel": 8, "duration_sec": 5})
check("PUT watering config", status, 200)
if status == 200:
    created.append(f"watering_config plant_id={tmp_id}")

status, _ = call("PUT", f"/plants/{tmp_id}/watering/config", token=TOKEN,
                 body={"device_id": "11111111-1111-1111-1111-111111111111",
                       "relay_channel": 7})
check("unknown device_id is rejected", status, 400)

print("\n=== watering command lifecycle ===")
status, cmd = call("POST", f"/plants/{tmp_id}/water", token=TOKEN)
check("POST water", status, 202)
command_id = cmd.get("command_id")
if command_id:
    created.append(f"watering_events + command_outbox command_id={command_id}")

status, _ = call("POST", f"/plants/{tmp_id}/water", token=TOKEN)
check("second command while one is in flight is 409", status, 409)

status, cmd_status = call("GET", f"/commands/{command_id}/status", token=TOKEN)
check("GET command status", status, 200)
check("command is live", cmd_status.get("status") in ("pending", "dispatched"), True)
check("triggered_by recorded", cmd_status.get("triggered_by"), "manual")

status, _ = call("PATCH", f"/plants/{tmp_id}/watering/config", token=TOKEN,
                 body={"mode": "auto"})
check("PATCH to auto mode", status, 200)

status, _ = call("POST", f"/plants/{tmp_id}/water", token=TOKEN)
check("manual watering refused in auto mode", status, 400)

status, _ = call("GET", "/commands/00000000-0000-4000-8000-000000000000/status",
                 token=TOKEN)
check("unknown command is 404", status, 404)

print("\n=== alerts ===")
status, alerts = call("GET", "/alerts", token=TOKEN)
check("GET /alerts", status, 200)
status, _ = call("POST", "/alerts/999999/ack", token=TOKEN)
check("acking a missing alert is 404", status, 404)

print(f"\n{'=' * 60}")
print(f"  {passed} passed, {failed} failed")
print(f"{'=' * 60}")
print("\nCLEANUP — rows this run created:")
for item in created:
    print(f"  - {item}")
print(f"""
  DELETE FROM command_outbox  WHERE command_id IN
      (SELECT command_id FROM watering_events WHERE plant_id = {tmp_id});
  DELETE FROM watering_events WHERE plant_id = {tmp_id};
  DELETE FROM watering_config WHERE plant_id = {tmp_id};
  DELETE FROM plants          WHERE id = {tmp_id};
  DELETE FROM sensor_alert_config WHERE sensor_id = {sensor_id} AND threshold = -1;
  DELETE FROM sensor_calibration  WHERE sensor_id = {sensor_id} AND notes = 'smoke test';
""")
raise SystemExit(1 if failed else 0)
