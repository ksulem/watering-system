"""add plants.name_en; seed real device, plants and wired moisture sensors

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-07

"""
import os
from collections.abc import Sequence

import sqlparse
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_FILE = os.path.join(os.path.dirname(__file__), "..", "002_seed.sql")


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
    # Deleting plants/sensors would cascade into real readings. If this seed
    # needs undoing, do it deliberately by hand.
    raise NotImplementedError(
        "No downgrade for 0002_seed — removing sensors would orphan or delete "
        "recorded sensor_readings. Remove rows manually if truly required."
    )
