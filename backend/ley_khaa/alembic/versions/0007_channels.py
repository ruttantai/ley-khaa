"""phase 6: channel adapters — dead letters and the notification guard

Revision ID: 0007_channels
Revises: 0006_alias_jsonb

Creates the dead_letters TABLE and adds tasks.last_notified_state. It seeds
nothing: there is nothing to seed, and a migration docstring that claims a seed
it does not perform is the false-statement class of defect commit 8cebd1f
cleaned up.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_channels"
down_revision = "0006_alias_jsonb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dead_letters",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dead_letters_source", "dead_letters", ["source"])
    op.create_index("ix_dead_letters_kind", "dead_letters", ["kind"])
    op.create_index("ix_dead_letters_created_at", "dead_letters", ["created_at"])

    op.add_column("tasks", sa.Column("last_notified_state", sa.String(), nullable=True))


def downgrade() -> None:
    # LIFO, matching 0002-0006: drop what this revision added, newest first.
    op.drop_column("tasks", "last_notified_state")
    op.drop_index("ix_dead_letters_created_at", table_name="dead_letters")
    op.drop_index("ix_dead_letters_kind", table_name="dead_letters")
    op.drop_index("ix_dead_letters_source", table_name="dead_letters")
    op.drop_table("dead_letters")
