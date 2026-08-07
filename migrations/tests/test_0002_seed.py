import importlib
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import sqlparse

seed_migration = importlib.import_module("0002_seed")

SQL_TEXT = Path(seed_migration.SQL_FILE).read_text(encoding="utf-8")
# Assertions about what the migration *does* run against the comment-free SQL:
# sqlparse.split() attaches a preceding comment block to its statement, and this
# file's header comment names the hardware that is deliberately NOT seeded.
SQL_CODE = sqlparse.format(SQL_TEXT, strip_comments=True)
REPO_ROOT = Path(seed_migration.SQL_FILE).resolve().parent.parent

# The six moisture sensors that are actually wired, in MCP3008 channel order.
EXPECTED_PLANTS = [
    ("Кофейное дерево", "Coffee tree"),
    ("Пеперомия", "Peperomia"),
    ("Нефролепис", "Boston fern"),
    ("Алоказия", "Alocasia"),
    ("Спатифиллум", "Peace lily"),
    ("Монстера", "Monstera"),
]


def _statements():
    return [s.strip() for s in sqlparse.split(SQL_CODE) if s.strip()]


def test_migration_is_three_statements():
    # The plants+sensors CTE must survive as ONE statement: splitting it would
    # orphan the RETURNING that gives each sensor its plant_id.
    statements = _statements()
    assert len(statements) == 3
    assert statements[0].startswith("ALTER TABLE plants")
    assert "INSERT INTO devices" in statements[1]
    assert "WITH wiring" in statements[2]
    assert "INSERT INTO sensors" in statements[2]


def test_every_wired_plant_is_seeded_in_both_languages():
    wiring = _statements()[2]
    for channel, (name_ru, name_en) in enumerate(EXPECTED_PLANTS):
        assert re.search(rf"\({channel}, '{name_ru}',\s*'{name_en}'\)", wiring), \
            f"channel {channel} ({name_ru} / {name_en}) missing from the wiring table"


def test_only_wired_hardware_is_seeded():
    # adc:6 (tank), adc:7 (light), and the DHT22 GPIO sensors don't physically
    # exist yet — seeding them would make the Consumer expect readings that can
    # never arrive and put fictional sensors in the API's responses.
    for absent in ("adc:6", "adc:7", "gpio:4"):
        assert absent not in SQL_CODE
    # No relay hardware either, so no watering_config -> auto-water stays inert.
    assert "INSERT INTO watering_config" not in SQL_CODE


def test_device_uuid_matches_the_broker_acl_and_pi_config():
    # If these three ever diverge, the Pi authenticates and publishes fine but
    # every reading is dead-lettered as unknown_sensor — a silent data outage
    # that looks like healthy services.
    seeded = re.findall(r"'([0-9a-f-]{36})'", SQL_CODE)
    assert seeded, "no device UUID found in the seed"
    device_id = seeded[0]

    acl = (REPO_ROOT / "mosquitto" / "acl").read_text(encoding="utf-8")
    assert device_id in acl

    pi_env = (REPO_ROOT / "pi_client" / ".env.example").read_text(encoding="utf-8")
    assert f"DEVICE_ID={device_id}" in pi_env


def test_sensor_addresses_match_the_pi_sensor_config():
    # The Pi builds addresses as f"adc:{channel}"; the seed builds them as
    # 'adc:' || channel. Same six channels, or readings won't resolve.
    pi_env = (REPO_ROOT / "pi_client" / ".env.example").read_text(encoding="utf-8")
    configured = re.findall(r"adc:(\d+):moisture", pi_env)
    seeded_channels = re.findall(r"\((\d+), '", SQL_CODE)
    assert configured == seeded_channels == [str(n) for n in range(6)]


def test_upgrade_executes_every_statement_individually():
    executed = []
    fake_op = MagicMock()
    fake_op.execute.side_effect = lambda stmt: executed.append(stmt)
    seed_migration.op = fake_op

    seed_migration.upgrade()

    assert len(executed) == len(_statements())


def test_revision_chains_onto_the_init_migration():
    assert seed_migration.revision == "0002"
    assert seed_migration.down_revision == "0001"


def test_downgrade_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        seed_migration.downgrade()
