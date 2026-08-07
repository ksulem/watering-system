"""The API and the Consumer both create watering commands. Keep them identical.

Manual watering (API) and auto watering (Consumer) are two independent writers
competing for one physical relay. They only serialize correctly if both take the
same `SELECT ... FOR UPDATE` lock on the plant row and run the same
active-command check inside the same transaction. The two implementations live
in separate Docker build contexts and can't share a module, so this compares
them structurally instead — if someone fixes a bug in one, this fails until the
other is fixed too.
"""
import ast
import inspect
import textwrap

from api import db as api_db
from consumer import db as consumer_db

SQL_METHODS = {"execute", "fetch", "fetchrow", "fetchval"}


def sql_of(fn) -> list[str]:
    """Every SQL literal the function passes to a connection, whitespace-normalized."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    statements = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in SQL_METHODS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            statements.append(" ".join(node.args[0].value.split()))
    return statements


def payload_keys(fn) -> list[str]:
    """Keys of the command payload dict built inside the function."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == "command_id" for k in node.keys):
            return [k.value for k in node.keys if isinstance(k, ast.Constant)]
    raise AssertionError("no command payload dict found")


def test_command_creation_sql_is_identical():
    api_sql = sql_of(api_db.try_create_pending_watering_command)
    consumer_sql = sql_of(consumer_db.try_create_pending_watering_command)

    assert api_sql == consumer_sql, (
        "api and consumer create watering commands with different SQL — they "
        "race for the same relay and must lock identically"
    )


def test_command_creation_takes_the_plant_row_lock():
    # The guard the whole design rests on. Losing the FOR UPDATE would let two
    # concurrent dry-soil decisions both insert a command and double-water.
    for fn in (api_db.try_create_pending_watering_command,
               consumer_db.try_create_pending_watering_command):
        assert any("FOR UPDATE" in s for s in sql_of(fn)), fn.__module__


def test_command_creation_writes_both_rows():
    for fn in (api_db.try_create_pending_watering_command,
               consumer_db.try_create_pending_watering_command):
        statements = " | ".join(sql_of(fn))
        # No compensation path exists, so both inserts must be in the function.
        assert "INSERT INTO watering_events" in statements, fn.__module__
        assert "INSERT INTO command_outbox" in statements, fn.__module__


def test_active_command_check_is_identical():
    assert (sql_of(api_db.has_active_watering_command)
            == sql_of(consumer_db.has_active_watering_command))


def test_command_payload_matches_the_pi_contract():
    api_keys = payload_keys(api_db.try_create_pending_watering_command)
    consumer_keys = payload_keys(consumer_db.try_create_pending_watering_command)

    assert api_keys == consumer_keys
    # This is what the Pi parses off the commands topic; dropping a field here
    # breaks watering_controller.py, which has no schema fallback.
    assert set(api_keys) == {
        "schema_version", "command_id", "command", "device_id", "plant_id",
        "relay_channel", "duration_sec", "expires_at",
    }


def test_payload_is_cast_to_jsonb_on_insert():
    # asyncpg has no implicit dict->jsonb codec; without the cast the insert
    # fails at runtime. The dispatcher reads this column back as a str.
    for fn in (api_db.try_create_pending_watering_command,
               consumer_db.try_create_pending_watering_command):
        outbox = [s for s in sql_of(fn) if "command_outbox" in s]
        assert outbox and "::jsonb" in outbox[0], fn.__module__
