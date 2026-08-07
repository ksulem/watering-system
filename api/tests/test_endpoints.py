from datetime import UTC, datetime
from uuid import UUID

import pytest

from api.tests.conftest import OWNER_ID, TEST_SECRET

NOW = datetime(2026, 8, 7, 10, 0, tzinfo=UTC)
DEVICE_ID = UUID("260922ba-03f3-4c20-a7e4-1d7dbf8d9f42")


def plant_row(**over):
    row = {"id": 1, "name": "Монстера", "name_en": "Monstera", "location": "Kitchen",
           "created_at": NOW, "mode": "manual", "auto_threshold": 30.0,
           "last_watered_at": None}
    row.update(over)
    return row


def sensor_row(**over):
    row = {"sensor_id": 6, "plant_id": 1, "address": "adc:5",
           "label": "Монстера moisture", "sensor_type": "moisture", "unit": "%",
           "value": 36.7, "raw_adc": 566, "recorded_at": NOW}
    row.update(over)
    return row


def config_row(**over):
    row = {"plant_id": 1, "device_id": DEVICE_ID, "mode": "manual",
           "auto_threshold": 30.0, "duration_sec": 10, "relay_channel": 2,
           "min_interval_min": 240, "updated_at": NOW}
    row.update(over)
    return row


def command_row(**over):
    row = {"command_id": UUID("cccccccc-0000-4000-8000-000000000001"), "plant_id": 1,
           "device_id": DEVICE_ID, "triggered_by": "manual", "duration_sec": 10,
           "status": "pending", "requested_at": NOW, "expires_at": NOW,
           "started_at": None, "completed_at": None}
    row.update(over)
    return row


# --- Health and auth gating --------------------------------------------------

def test_health_is_open_and_reports_db_state(client, fake_db):
    assert client.get("/health").json() == {"status": "ok", "db": True}

    fake_db.healthy.return_value = False
    resp = client.get("/health")
    # Unhealthy, not just informational — Docker should restart an API that
    # can't reach TimescaleDB.
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


def test_endpoints_require_a_token(client):
    assert client.get("/plants").status_code == 401


def test_garbage_token_is_rejected(client):
    resp = client.get("/plants", headers={"Authorization": "Bearer nonsense"})
    assert resp.status_code == 401


def test_token_for_another_user_is_rejected(client):
    from api.auth import create_session_token
    stranger = create_session_token(OWNER_ID + 1, TEST_SECRET)
    resp = client.get("/plants", headers={"Authorization": f"Bearer {stranger}"})
    assert resp.status_code == 401


def test_unconfigured_auth_fails_closed(fake_db, unconfigured_auth):
    # The state the API is deployed in today. It must lock everything rather
    # than fall open, and say so distinctly from "your token is bad".
    from fastapi.testclient import TestClient

    from api.main import app
    client = TestClient(app)

    assert client.post("/auth/session").status_code == 503
    assert client.get("/plants").status_code == 503
    # Health still works, so the container can pass its healthcheck.
    assert client.get("/health").status_code == 200


def test_session_exchange_issues_a_token(client, configured_auth):
    from api.auth import verify_session_token
    from api.tests.conftest import make_init_data

    resp = client.post("/auth/session",
                       headers={"X-Telegram-Init-Data": make_init_data()})

    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_in"] == 86400
    assert verify_session_token(body["token"], TEST_SECRET, OWNER_ID)


def test_session_exchange_rejects_forged_init_data(client, configured_auth):
    from api.tests.conftest import make_init_data
    resp = client.post("/auth/session",
                       headers={"X-Telegram-Init-Data": make_init_data(tamper=True)})
    assert resp.status_code == 401


def test_session_exchange_needs_the_header(client, configured_auth):
    assert client.post("/auth/session").status_code == 400


# --- Plants ------------------------------------------------------------------

def test_list_plants(authed, fake_db):
    fake_db.list_plants.return_value = [plant_row(), plant_row(id=2, name="Алоказия")]

    body = authed.get("/plants").json()

    assert [p["id"] for p in body] == [1, 2]
    assert body[0]["name_en"] == "Monstera"


def test_plant_without_watering_config_reports_null_mode(authed, fake_db):
    # Every plant is in this state until a relay is plumbed to it.
    fake_db.list_plants.return_value = [plant_row(mode=None, auto_threshold=None)]

    body = authed.get("/plants").json()

    assert body[0]["mode"] is None


def test_get_plant_includes_its_sensors(authed, fake_db):
    fake_db.get_plant.return_value = plant_row()
    fake_db.latest_readings.return_value = [sensor_row()]

    body = authed.get("/plants/1").json()

    assert body["id"] == 1
    assert body["sensors"][0]["value"] == 36.7
    assert body["sensors"][0]["unit"] == "%"


def test_get_missing_plant_is_404(authed, fake_db):
    fake_db.get_plant.return_value = None
    assert authed.get("/plants/999").status_code == 404


def test_sensor_that_never_reported_returns_nulls(authed, fake_db):
    fake_db.get_plant.return_value = plant_row()
    fake_db.latest_readings.return_value = [
        sensor_row(value=None, raw_adc=None, recorded_at=None)]

    body = authed.get("/plants/1").json()

    assert body["sensors"][0]["value"] is None


def test_create_plant(authed, fake_db):
    fake_db.create_plant.return_value = plant_row(mode=None, auto_threshold=None)

    resp = authed.post("/plants", json={"name": "Фикус", "name_en": "Ficus"})

    assert resp.status_code == 201
    fake_db.create_plant.assert_awaited_once_with("Фикус", "Ficus", None)


def test_create_plant_rejects_an_empty_name(authed, fake_db):
    assert authed.post("/plants", json={"name": ""}).status_code == 422


def test_patch_missing_plant_is_404(authed, fake_db):
    fake_db.update_plant.return_value = None
    assert authed.patch("/plants/999", json={"name": "x"}).status_code == 404


# --- Readings ----------------------------------------------------------------

def test_latest_readings_for_a_plant(authed, fake_db):
    fake_db.latest_readings.return_value = [sensor_row()]
    assert authed.get("/plants/1/readings/latest").json()[0]["sensor_id"] == 6


def test_readings_for_missing_plant_are_404(authed, fake_db):
    fake_db.plant_exists.return_value = False
    assert authed.get("/plants/999/readings/latest").status_code == 404


def test_history_defaults_to_the_last_24h(authed, fake_db):
    fake_db.reading_history.return_value = []

    authed.get("/plants/1/readings/history")

    _, _, frm, to, limit = fake_db.reading_history.await_args.args
    assert (to - frm).total_seconds() == pytest.approx(86400, abs=5)
    assert limit == 500


def test_history_rejects_an_inverted_range(authed, fake_db):
    resp = authed.get("/plants/1/readings/history",
                      params={"from": "2026-08-07T10:00:00Z", "to": "2026-08-06T10:00:00Z"})
    assert resp.status_code == 400


def test_history_limit_is_capped(authed, fake_db):
    # A year of one sensor at 60s is ~500k rows; the cap is what stops a single
    # request trying to serialize that.
    assert authed.get("/plants/1/readings/history",
                      params={"limit": 100_000}).status_code == 422


def test_history_passes_the_sensor_type_filter_through(authed, fake_db):
    fake_db.reading_history.return_value = []
    authed.get("/plants/1/readings/history", params={"sensor_type": "moisture"})
    assert fake_db.reading_history.await_args.args[1] == "moisture"


@pytest.mark.parametrize("bucket", ["1h", "1d"])
def test_aggregated_accepts_both_buckets(authed, fake_db, bucket):
    fake_db.reading_aggregated.return_value = []
    assert authed.get("/plants/1/readings/aggregated",
                      params={"bucket": bucket}).status_code == 200


def test_aggregated_rejects_an_unknown_bucket(authed, fake_db):
    # Only two continuous aggregates exist; the pattern is what keeps an
    # arbitrary string away from the view-name lookup.
    assert authed.get("/plants/1/readings/aggregated",
                      params={"bucket": "1w"}).status_code == 422


def test_daily_aggregate_has_no_sample_count(authed, fake_db):
    fake_db.reading_aggregated.return_value = [{
        "bucket": NOW, "avg_value": 40.0, "min_value": 38.0, "max_value": 42.0,
        "sample_count": None, "sensor_id": 6, "address": "adc:5",
        "sensor_type": "moisture"}]

    body = authed.get("/plants/1/readings/aggregated", params={"bucket": "1d"}).json()

    assert body[0]["sample_count"] is None


# --- Watering ----------------------------------------------------------------

def test_get_watering_config_404_when_no_relay_assigned(authed, fake_db):
    fake_db.get_watering_config.return_value = None
    assert authed.get("/plants/1/watering/config").status_code == 404


def test_put_creates_the_first_watering_config(authed, fake_db):
    # PATCH can't create, and nothing seeds watering_config — PUT is the only
    # way a plant ever gets a relay.
    fake_db.put_watering_config.return_value = config_row()

    resp = authed.put("/plants/1/watering/config",
                      json={"device_id": str(DEVICE_ID), "relay_channel": 2})

    assert resp.status_code == 200
    assert resp.json()["relay_channel"] == 2


def test_put_reports_a_relay_already_taken(authed, fake_db):
    fake_db.put_watering_config.side_effect = Exception(
        'duplicate key value violates unique constraint "watering_relay_unique_per_device"')

    resp = authed.put("/plants/1/watering/config",
                      json={"device_id": str(DEVICE_ID), "relay_channel": 2})

    assert resp.status_code == 409
    assert "already assigned" in resp.json()["detail"]


@pytest.mark.parametrize("body", [
    {"device_id": str(DEVICE_ID), "relay_channel": 0},
    {"device_id": str(DEVICE_ID), "relay_channel": 9},
    {"device_id": str(DEVICE_ID), "relay_channel": 2, "duration_sec": 0},
    {"device_id": str(DEVICE_ID), "relay_channel": 2, "duration_sec": 999},
    {"device_id": str(DEVICE_ID), "relay_channel": 2, "auto_threshold": 150},
    {"device_id": "not-a-uuid", "relay_channel": 2},
    {"relay_channel": 2},
])
def test_put_config_validation_mirrors_the_db_constraints(authed, fake_db, body):
    # These would otherwise surface as an opaque 500 from a CHECK violation.
    assert authed.put("/plants/1/watering/config", json=body).status_code == 422


def test_patch_config_needs_at_least_one_field(authed, fake_db):
    assert authed.patch("/plants/1/watering/config", json={}).status_code == 422


def test_patch_config_404_points_at_put(authed, fake_db):
    fake_db.patch_watering_config.return_value = None
    resp = authed.patch("/plants/1/watering/config", json={"mode": "auto"})
    assert resp.status_code == 404
    assert "PUT" in resp.json()["detail"]


def test_manual_watering_queues_a_command(authed, fake_db):
    fake_db.get_watering_config.return_value = config_row(mode="manual")

    resp = authed.post("/plants/1/water")

    assert resp.status_code == 202
    assert resp.json()["status"] == "pending"
    kwargs = fake_db.try_create_pending_watering_command.await_args.kwargs
    assert kwargs["triggered_by"] == "manual"
    assert kwargs["relay_channel"] == 2
    assert kwargs["duration_sec"] == 10


def test_watering_in_auto_mode_is_refused(authed, fake_db):
    # Auto mode means the Consumer owns the decision; a manual command would
    # race its threshold logic.
    fake_db.get_watering_config.return_value = config_row(mode="auto")

    resp = authed.post("/plants/1/water")

    assert resp.status_code == 400
    fake_db.try_create_pending_watering_command.assert_not_awaited()


def test_watering_without_a_relay_is_404(authed, fake_db):
    fake_db.get_watering_config.return_value = None
    assert authed.post("/plants/1/water").status_code == 404


def test_second_watering_while_one_is_in_flight_is_409(authed, fake_db):
    fake_db.get_watering_config.return_value = config_row(mode="manual")
    fake_db.try_create_pending_watering_command.return_value = False

    assert authed.post("/plants/1/water").status_code == 409


def test_command_status(authed, fake_db):
    fake_db.get_command.return_value = command_row(status="completed")
    body = authed.get("/commands/cccccccc-0000-4000-8000-000000000001/status").json()
    assert body["status"] == "completed"


def test_missing_command_is_404(authed, fake_db):
    fake_db.get_command.return_value = None
    assert authed.get(
        "/commands/cccccccc-0000-4000-8000-000000000009/status").status_code == 404


def test_non_uuid_command_id_is_422(authed, fake_db):
    assert authed.get("/commands/banana/status").status_code == 422


# --- Sensors -----------------------------------------------------------------

def test_list_sensor_types(authed, fake_db):
    fake_db.list_sensor_types.return_value = [
        {"id": 1, "code": "moisture", "unit": "%", "label": "Soil Moisture"}]
    assert authed.get("/sensors/types").json()[0]["code"] == "moisture"


def test_sensor_types_path_is_not_shadowed_by_the_id_routes(authed, fake_db):
    # /sensors/types must not be parsed as /sensors/{sensor_id}/...
    fake_db.list_sensor_types.return_value = []
    assert authed.get("/sensors/types").status_code == 200


def test_list_sensors(authed, fake_db):
    fake_db.latest_readings.return_value = [sensor_row()]
    assert authed.get("/sensors").json()[0]["address"] == "adc:5"


def test_alert_config_upsert(authed, fake_db):
    fake_db.upsert_alert_config.return_value = {
        "id": 1, "sensor_id": 6, "alert_type": "too_low", "threshold": 25.0,
        "cooldown_min": 360}

    resp = authed.patch("/sensors/6/alert-config",
                        json={"alert_type": "too_low", "threshold": 25.0,
                              "cooldown_min": 360})

    assert resp.status_code == 200
    fake_db.upsert_alert_config.assert_awaited_once_with(6, "too_low", 25.0, 360)


def test_alert_config_rejects_an_unknown_direction(authed, fake_db):
    assert authed.patch("/sensors/6/alert-config",
                        json={"alert_type": "too_damp", "threshold": 1}).status_code == 422


def test_alert_config_rejects_a_zero_cooldown(authed, fake_db):
    # cooldown_min 0 would re-alert on every reading — 6 pushes a minute.
    assert authed.patch("/sensors/6/alert-config",
                        json={"alert_type": "too_low", "threshold": 25.0,
                              "cooldown_min": 0}).status_code == 422


def test_alert_config_for_missing_sensor_is_404(authed, fake_db):
    fake_db.sensor_exists.return_value = False
    assert authed.get("/sensors/99/alert-config").status_code == 404


def test_calibration_absent_means_defaults_are_in_use(authed, fake_db):
    fake_db.get_calibration.return_value = None
    resp = authed.get("/sensors/6/calibration")
    assert resp.status_code == 404
    assert "defaults" in resp.json()["detail"]


def test_put_calibration(authed, fake_db):
    fake_db.put_calibration.return_value = {
        "sensor_id": 6, "raw_dry": 810, "raw_wet": 300, "calibrated_at": NOW,
        "notes": "Peperomia reads dry"}

    resp = authed.put("/sensors/6/calibration",
                      json={"raw_dry": 810, "raw_wet": 300,
                            "notes": "Peperomia reads dry"})

    assert resp.status_code == 200
    assert resp.json()["raw_dry"] == 810


def test_calibration_endpoints_must_differ(authed, fake_db):
    # Equal endpoints are a divide-by-zero in the Consumer's linear map, and the
    # DB has a CHECK for it — reject before either gets involved.
    assert authed.put("/sensors/6/calibration",
                      json={"raw_dry": 500, "raw_wet": 500}).status_code == 422


@pytest.mark.parametrize("body", [
    {"raw_dry": -1, "raw_wet": 300},
    {"raw_dry": 1024, "raw_wet": 300},
])
def test_calibration_stays_inside_the_adc_range(authed, fake_db, body):
    assert authed.put("/sensors/6/calibration", json=body).status_code == 422


# --- Alerts ------------------------------------------------------------------

def test_list_alerts(authed, fake_db):
    fake_db.list_alerts.return_value = [{
        "id": 1, "plant_id": 1, "sensor_id": 6, "alert_type": "too_low",
        "value": 18.0, "sent_at": NOW, "acknowledged": False,
        "sensor_label": "Монстера moisture"}]

    body = authed.get("/alerts", params={"unacked": "true"}).json()

    assert body[0]["sensor_label"] == "Монстера moisture"
    assert fake_db.list_alerts.await_args.kwargs["unacked"] is True


def test_ack_alert(authed, fake_db):
    fake_db.ack_alert.return_value = {
        "id": 1, "plant_id": 1, "sensor_id": 6, "alert_type": "too_low",
        "value": 18.0, "sent_at": NOW, "acknowledged": True}

    assert authed.post("/alerts/1/ack").json()["acknowledged"] is True


def test_ack_missing_alert_is_404(authed, fake_db):
    fake_db.ack_alert.return_value = None
    assert authed.post("/alerts/99/ack").status_code == 404
