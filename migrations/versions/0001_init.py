"""init schema

Revision ID: 0001
Revises:
Create Date: 2026-07-25

"""
import os
from collections.abc import Sequence

import sqlparse
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_FILE = os.path.join(os.path.dirname(__file__), "..", "001_init.sql")


def upgrade() -> None:
    # asyncpg's SQLAlchemy dialect always uses prepared statements, which
    # reject multi-statement strings ("cannot insert multiple commands into
    # a prepared statement") - unlike psql or raw asyncpg.execute() with the
    # simple query protocol. Split into individual statements instead.
    # sqlparse.split() is comment/string-aware, unlike a naive split(";").
    with open(SQL_FILE, encoding="utf-8") as f:
        sql = f.read()
    for statement in sqlparse.split(sql):
        statement = statement.strip()
        if statement:
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError(
        "No downgrade for 0001_init — TimescaleDB hypertables/continuous "
        "aggregates don't downgrade cleanly. Restore from backup instead."
    )
