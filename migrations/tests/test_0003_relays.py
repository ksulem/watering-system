import importlib
import re
from pathlib import Path
from unittest.mock import MagicMock

import sqlparse

relays_migration = importlib.import_module("0003_relays")

SQL_TEXT = Path(relays_migration.SQL_FILE).read_text(encoding="utf-8")
# Same reason as test_0002_seed: the header comment documents the wiring, and
# sqlparse.split() would attach it to the first statement. Assertions about what
# the migration *does* run against the comment-free SQL — but the wiring table in
# the comments is checked separately below, since it is the human-facing map.
SQL_CODE = sqlparse.format(SQL_TEXT, strip_comments=True)
REPO_ROOT = Path(relays_migration.SQL_FILE).resolve().parent.parent

# The physical wiring: (ru, en, relay_channel, gpio). relay_channel is the relay
# board's own IN label, so 4 and 7 are absent by design — see WIRING_GAPS.
EXPECTED_WIRING = [
    ("Кофейное дерево", "Coffee tree", 1, 5),
    ("Пеперомия", "Peperomia", 2, 6),
    ("Нефролепис", "Boston fern", 3, 13),
    ("Алоказия", "Alocasia", 5, 19),
    ("Спатифиллум", "Peace lily", 6, 26),
    ("Монстера", "Monstera", 8, 21),
]
WIRING_GAPS = {4, 7}


def _statements():
    return [s.strip() for s in sqlparse.split(SQL_CODE) if s.strip()]


def test_migration_is_one_statement():
    # The CTE must survive as ONE statement: splitting it would orphan the JOIN
    # that resolves each plant's generated id from its name.
    statements = _statements()
    assert len(statements) == 1
    assert "WITH relay_wiring" in statements[0]
    assert "INSERT INTO watering_config" in statements[0]


def test_every_wired_plant_gets_its_channel_in_both_languages():
    sql = _statements()[0]
    for name_ru, name_en, channel, _gpio in EXPECTED_WIRING:
        assert re.search(rf"\('{name_ru}',\s*'{name_en}',\s*{channel}\)", sql), \
            f"{name_ru} / {name_en} -> relay {channel} missing from the wiring table"


def test_unwired_channels_are_not_assigned():
    # 4 and 7 have no GPIO driving them and no solenoid attached. Assigning one
    # would create a config whose water_now command can only ever fail, and
    # would silently steal the channel smoke_live.py relies on being free.
    assigned = {c for _ru, _en, c, _g in EXPECTED_WIRING}
    assert assigned.isdisjoint(WIRING_GAPS)
    sql = _statements()[0]
    for gap in WIRING_GAPS:
        assert not re.search(rf",\s*{gap}\)", sql), \
            f"relay channel {gap} is unwired but appears in the seed"


def test_channels_are_unique_and_within_the_schema_constraint():
    # watering_config has CHECK (relay_channel BETWEEN 1 AND 8) and
    # UNIQUE (device_id, relay_channel). Violating either turns `alembic upgrade`
    # into a failed deploy, so catch it here rather than against a live DB.
    channels = [c for _ru, _en, c, _g in EXPECTED_WIRING]
    assert len(channels) == len(set(channels))
    assert all(1 <= c <= 8 for c in channels)


def test_seeded_mode_is_manual():
    # The schema default is 'auto'. Auto-watering six plants through pumps that
    # have never moved water is how a mis-seated tube floods a windowsill, so the
    # seed overrides it. Flip to auto per plant via the API after a witnessed run.
    sql = _statements()[0]
    assert "'manual'" in sql
    assert "'auto'" not in sql


def test_device_uuid_matches_the_rest_of_the_system():
    # A different device_id here means the Pi is never sent these commands: the
    # dispatcher publishes to a topic nothing is subscribed to, and every command
    # sits pending until it expires.
    seeded = re.findall(r"'([0-9a-f-]{36})'", SQL_CODE)
    assert seeded, "no device UUID found in the seed"
    device_id = seeded[0]

    seed_sql = (REPO_ROOT / "migrations" / "002_seed.sql").read_text(encoding="utf-8")
    assert device_id in seed_sql

    pi_env = (REPO_ROOT / "pi_client" / ".env.example").read_text(encoding="utf-8")
    assert f"DEVICE_ID={device_id}" in pi_env


def _relay_pins_from(text: str) -> dict[int, int]:
    match = re.search(r"RELAY_PINS = \{(.+?)\}", text, re.DOTALL)
    assert match, "RELAY_PINS not found"
    body = re.sub(r"#.*", "", match.group(1))   # per-line comments name pins too
    return {int(c): int(g) for c, g in re.findall(r"(\d+):\s*(\d+)", body)}


def test_relay_pins_on_the_pi_agree_with_the_seed():
    # The check that matters physically, against the module that actually runs.
    # The DB says which channel waters which plant; RELAY_PINS says which GPIO
    # that channel drives. If they drift, every command still succeeds — it just
    # waters the wrong plant, which no log or green test would ever reveal.
    module = (REPO_ROOT / "pi_client" / "relay.py").read_text(encoding="utf-8")
    assert _relay_pins_from(module) == {c: g for _ru, _en, c, g in EXPECTED_WIRING}


def test_relay_board_is_active_low():
    # watering_controller and test_relay both derive their levels from these.
    # If the board is swapped without updating this, every "stop" becomes a
    # "start" and a command leaves its pump running.
    module = (REPO_ROOT / "pi_client" / "relay.py").read_text(encoding="utf-8")
    assert re.search(r"^RELAY_ACTIVE_HIGH = False", module, re.MULTILINE)


def test_relay_pins_in_the_plan_agree_with_the_seed():
    # This is the check that matters physically. The DB says which channel waters
    # which plant; RELAY_PINS says which GPIO that channel drives. If they drift,
    # every command still succeeds — it just waters the wrong plant, which no
    # amount of green tests or clean logs would reveal.
    # Same check against the design doc. It carried a stale map for months
    # (channel 8 -> GPIO4, which is the DHT22 pin) precisely because nothing
    # tested it.
    plan = (REPO_ROOT / "plant-monitor-plan.md").read_text(encoding="utf-8")
    assert _relay_pins_from(plan) == {c: g for _ru, _en, c, g in EXPECTED_WIRING}


def test_smoke_test_does_not_claim_a_wired_channel():
    # smoke_live.py runs against the real device and calls POST /water on the
    # config it creates. If its channel collides with a seeded one, the PUT hits
    # UNIQUE (device_id, relay_channel) and 409s — and if the constraint were ever
    # relaxed, the smoke test would water a real plant on every run.
    smoke = (REPO_ROOT / "api" / "tests" / "smoke_live.py").read_text(encoding="utf-8")
    used = {int(c) for c in re.findall(r'"relay_channel":\s*(\d+)', smoke)}
    assigned = {c for _ru, _en, c, _g in EXPECTED_WIRING}
    assert used.isdisjoint(assigned), \
        f"smoke_live.py uses seeded relay channel(s) {sorted(used & assigned)}"


def test_upgrade_executes_every_statement_individually():
    executed = []
    fake_op = MagicMock()
    fake_op.execute.side_effect = lambda stmt: executed.append(stmt)
    relays_migration.op = fake_op

    relays_migration.upgrade()

    assert len(executed) == len(_statements())


def test_revision_chains_onto_the_seed_migration():
    assert relays_migration.revision == "0003"
    assert relays_migration.down_revision == "0002"
