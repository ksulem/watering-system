"""seed watering_config for the six wired relay channels

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-09

"""
import os
from collections.abc import Sequence

import sqlparse
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_FILE = os.path.join(os.path.dirname(__file__), "..", "003_relays.sql")


def upgrade() -> None:
    # Same statement-splitting rationale as 0001: asyncpg's SQLAlchemy dialect
    # uses prepared statements, which reject multi-statement strings.
    with open(SQL_FILE, encoding="utf-8") as f:
        sql = f.read()
    for statement in sqlparse.split(sql):
        statement = statement.strip()
        if statement:
            op.execute(statement)


def downgrade() -> None:
    # Unlike 0002, this is safely reversible: watering_config holds no history,
    # and watering_events references plants rather than this table. Dropping the
    # rows disables watering (auto_water short-circuits with no config) without
    # destroying anything recorded.
    op.execute(
        "DELETE FROM watering_config WHERE device_id = "
        "'260922ba-03f3-4c20-a7e4-1d7dbf8d9f42'::uuid "
        "AND relay_channel IN (1, 2, 3, 5, 6, 8)"
    )
