import importlib
import re
from pathlib import Path
from unittest.mock import MagicMock

import sqlparse

init_migration = importlib.import_module("0001_init")

SQL_TEXT = Path(init_migration.SQL_FILE).read_text(encoding="utf-8")

# Regression anchor: manually verified against a live TimescaleDB instance on
# 2026-07-25 (`\dt` showed 14 tables total = these 13 from 001_init.sql plus
# Alembic's own `alembic_version` bookkeeping table). If this changes, it
# should be a deliberate schema change, not an accidental SQL-splitting bug.
EXPECTED_TABLE_COUNT = 13


def _statements():
    return [s.strip() for s in sqlparse.split(SQL_TEXT) if s.strip()]


def test_sql_file_is_nonempty():
    assert len(SQL_TEXT) > 0


def test_statement_splitting_matches_known_table_count():
    # This is the exact bug hit on 2026-07-25: asyncpg's SQLAlchemy dialect
    # rejects multi-statement strings, so 001_init.sql must be split into
    # individual statements before op.execute(). sqlparse.split() is
    # comment/string-aware, unlike a naive split(";"). Cross-check its output
    # against a plain regex count of CREATE TABLE in the raw source as a
    # sanity check that splitting isn't silently merging or shredding
    # statements.
    statements = _statements()

    create_table_via_split = sum(1 for s in statements if re.search(r"CREATE TABLE", s, re.IGNORECASE))
    create_table_via_regex = len(re.findall(r"CREATE TABLE", SQL_TEXT, re.IGNORECASE))

    assert create_table_via_split == create_table_via_regex == EXPECTED_TABLE_COUNT


def test_statement_splitting_keeps_seed_insert_intact():
    statements = _statements()
    seed_statements = [s for s in statements if "INSERT INTO sensor_types" in s]

    assert len(seed_statements) == 1
    # All 5 seeded rows must stay part of the single INSERT, not get shredded
    # into separate statements by a naive semicolon split.
    assert seed_statements[0].count("(") >= 5


def test_upgrade_executes_every_statement_individually():
    executed = []
    fake_op = MagicMock()
    fake_op.execute.side_effect = lambda stmt: executed.append(stmt)
    init_migration.op = fake_op

    init_migration.upgrade()

    assert len(executed) == len(_statements())
    assert "CREATE EXTENSION" in executed[0].upper()
    assert "INSERT INTO sensor_types" in executed[-1]


def test_downgrade_raises_not_implemented():
    import pytest
    with pytest.raises(NotImplementedError):
        init_migration.downgrade()
