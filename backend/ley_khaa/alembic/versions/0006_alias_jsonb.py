"""task 15: operation_aliases -> JSONB on Postgres

Revision ID: 0006_alias_jsonb
Revises: 0005_routing_queues

record_success()'s compare-and-swap (WHERE operation_aliases == current) needs
real equality on the column. Postgres's plain `json` type defines none —
every alias-learning call raised UndefinedFunction in production while the
SQLite-only test suite stayed green. `jsonb` has equality and preserves array
order, so the CAS keeps the exact meaning it already has on SQLite; every
other dialect keeps plain JSON, unchanged.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_alias_jsonb"
down_revision = "0005_routing_queues"
branch_labels = None
depends_on = None

_JSONB_ON_POSTGRES = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("workflows") as batch_op:
        batch_op.alter_column(
            "operation_aliases",
            existing_type=sa.JSON(),
            type_=_JSONB_ON_POSTGRES,
            existing_nullable=False,
            postgresql_using="operation_aliases::jsonb",
        )


def downgrade() -> None:
    with op.batch_alter_table("workflows") as batch_op:
        batch_op.alter_column(
            "operation_aliases",
            existing_type=_JSONB_ON_POSTGRES,
            type_=sa.JSON(),
            existing_nullable=False,
            postgresql_using="operation_aliases::json",
        )
