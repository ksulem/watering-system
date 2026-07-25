import json
import subprocess
from unittest.mock import AsyncMock

import pytest
import sensor_reader
from sensors.adc import ADCSensor

# --- build_sensors --------------------------------------------------------

def test_build_sensors_parses_adc_entries():
    sensors = sensor_reader.build_sensors("adc:0:moisture,adc:6:water_level")

    assert len(sensors) == 2
    assert all(isinstance(s, ADCSensor) for s in sensors)
    assert [s.channel for s in sensors] == [0, 6]
    assert [s.sensor_type for s in sensors] == ["moisture", "water_level"]
    assert [s.address for s in sensors] == ["adc:0", "adc:6"]


def test_build_sensors_ignores_unrecognized_entries():
    # No 'else' branch in build_sensors — unknown prefixes are silently
    # dropped rather than raising, matching "not all channels are wired".
    sensors = sensor_reader.build_sensors("adc:0:moisture,i2c:0x77:pressure")

    assert len(sensors) == 1
    assert sensors[0].channel == 0


# --- clock_synchronized ----------------------------------------------------

def test_clock_synchronized_when_leap_status_normal(monkeypatch):
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=(
        "Reference ID    : A29FC87B (time.cloudflare.com)\n"
        "Leap status     : Normal\n"
    ))
    monkeypatch.setattr(sensor_reader.subprocess, "run", lambda *a, **k: fake)
    assert sensor_reader.clock_synchronized() is True


def test_clock_synchronized_when_leap_status_not_normal(monkeypatch):
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="Leap status     : Not synchronised\n")
    monkeypatch.setattr(sensor_reader.subprocess, "run", lambda *a, **k: fake)
    assert sensor_reader.clock_synchronized() is False


def test_clock_synchronized_when_chronyc_missing(monkeypatch):
    def raise_missing(*a, **k):
        raise FileNotFoundError("chronyc not found")
    monkeypatch.setattr(sensor_reader.subprocess, "run", raise_missing)
    assert sensor_reader.clock_synchronized() is False


def test_clock_synchronized_when_chronyc_times_out(monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="chronyc", timeout=5)
    monkeypatch.setattr(sensor_reader.subprocess, "run", raise_timeout)
    assert sensor_reader.clock_synchronized() is False


# --- outbox / stage ---------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "outbox.db"
    original = sensor_reader.OUTBOX_DB
    sensor_reader.OUTBOX_DB = str(db_path)
    try:
        yield sensor_reader.outbox()
    finally:
        sensor_reader.OUTBOX_DB = original


def test_stage_deduplicates_by_reading_id(conn):
    msg = {"reading_id": "abc-123", "address": "adc:0"}
    sensor_reader.stage(conn, msg)
    sensor_reader.stage(conn, msg)  # simulate a redelivered/duplicate stage

    rows = conn.execute("SELECT reading_id FROM reading_outbox").fetchall()
    assert len(rows) == 1


def test_stage_trims_to_max_rows_keeping_newest(conn, monkeypatch):
    monkeypatch.setattr(sensor_reader, "OUTBOX_MAX_ROWS", 3)

    # Insert with explicit, strictly increasing created_at so "newest" is
    # unambiguous — stage()'s default datetime('now') has 1s resolution and
    # rapid inserts in a test could tie.
    for i in range(5):
        conn.execute(
            "INSERT INTO reading_outbox(reading_id, payload, created_at) VALUES (?, ?, ?)",
            (f"reading-{i}", json.dumps({"reading_id": f"reading-{i}"}), f"2026-01-01T00:00:{i:02d}"),
        )
    # Trigger the trim logic via stage() with one more (newest) row.
    sensor_reader.stage(conn, {"reading_id": "reading-5"})

    remaining = {r[0] for r in conn.execute("SELECT reading_id FROM reading_outbox").fetchall()}
    assert remaining == {"reading-3", "reading-4", "reading-5"}


# --- publish_drain -----------------------------------------------------------

def _seed_readings(conn, ids):
    for reading_id in ids:
        conn.execute(
            "INSERT INTO reading_outbox(reading_id, payload) VALUES (?, ?)",
            (reading_id, json.dumps({"reading_id": reading_id})),
        )


async def test_publish_drain_publishes_all_and_empties_outbox(conn):
    _seed_readings(conn, ["r1", "r2", "r3"])
    client = AsyncMock()

    await sensor_reader.publish_drain(client, conn)

    assert client.publish.await_count == 3
    remaining = conn.execute("SELECT reading_id FROM reading_outbox").fetchall()
    assert remaining == []


async def test_publish_drain_stops_on_failure_without_losing_already_acked(conn):
    _seed_readings(conn, ["r1", "r2", "r3"])
    client = AsyncMock()
    client.publish.side_effect = [None, ConnectionError("broker dropped"), None]

    with pytest.raises(ConnectionError):
        await sensor_reader.publish_drain(client, conn)

    # r1 was acked before the failure -> deleted. r2 failed mid-publish -> its
    # delete never ran, so it stays queued (correctly retried next run). r3
    # was never attempted.
    remaining = {r[0] for r in conn.execute("SELECT reading_id FROM reading_outbox").fetchall()}
    assert remaining == {"r2", "r3"}
