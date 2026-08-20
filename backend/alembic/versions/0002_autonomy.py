"""phase 2: task spec, autonomy scoring, and task replies

Revision ID: 0002_autonomy
Revises: 0001_baseline
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_autonomy"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

_TASK_COLUMNS = [
    sa.Column("candidate_id", sa.String(), nullable=True),
    sa.Column("spec", sa.JSON(), nullable=True),
    sa.Column("recommended_mode", sa.String(), nullable=True),
    sa.Column("mode_override", sa.String(), nullable=True),
    sa.Column("confidence", sa.Float(), nullable=True),
    sa.Column("risk", sa.Float(), nullable=True),
    sa.Column("autonomy_reason", sa.String(), nullable=True),
    sa.Column("open_question", sa.String(), nullable=True),
    sa.Column("failure_reason", sa.String(), nullable=True),
    sa.Column("interpret_attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("clarification_rounds", sa.Integer(), nullable=False, server_default="0"),
]


def upgrade() -> None:
    for column in _TASK_COLUMNS:
        op.add_column("tasks", column)
    op.create_index("ix_tasks_candidate_id", "tasks", ["candidate_id"])
    op.add_column("messages", sa.Column("reply_to_task_id", sa.String(), nullable=True))
    op.create_index("ix_messages_reply_to_task_id", "messages", ["reply_to_task_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_reply_to_task_id", table_name="messages")
    op.drop_column("messages", "reply_to_task_id")
    op.drop_index("ix_tasks_candidate_id", table_name="tasks")
    for column in reversed(_TASK_COLUMNS):
        op.drop_column("tasks", column.name)
