"""baseline: the 0.2.0 schema

Revision ID: 0001_baseline
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source_message_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("relevant", sa.Boolean(), nullable=True),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
    )
    op.create_index("ix_messages_external_id", "messages", ["external_id"], unique=True)
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_table(
        "task_candidates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("candidate_key", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("message_ids", sa.JSON(), nullable=False),
        sa.Column("missing_fields", sa.JSON(), nullable=False),
        sa.Column("open_question", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "conversation_id", "candidate_key", name="uq_candidate_per_conversation"
        ),
    )
    op.create_index("ix_task_candidates_conversation_id", "task_candidates", ["conversation_id"])
    op.create_index("ix_task_candidates_candidate_key", "task_candidates", ["candidate_key"])


def downgrade() -> None:
    op.drop_table("task_candidates")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_index("ix_messages_external_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("tasks")
