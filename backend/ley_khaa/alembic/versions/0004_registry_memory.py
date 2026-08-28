"""phase 4: workflow registry and task memory

Revision ID: 0004_registry_memory
Revises: 0003_executor
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_registry_memory"
down_revision = "0003_executor"
branch_labels = None
depends_on = None

_TASK_COLUMNS = [
    sa.Column("remembered_from_task_id", sa.String(), nullable=True),
    sa.Column("familiarity", sa.Integer(), nullable=False, server_default="0"),
]


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("operation_aliases", sa.JSON(), nullable=False),
        sa.Column("output_format", sa.String(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_sha256", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False, server_default="promoted"),
        sa.Column("promoted_from_task_id", sa.String(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runs_ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("runs_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_index("ix_workflows_name", "workflows", ["name"], unique=True)

    op.create_table(
        "task_memory",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project", sa.String(), nullable=False, server_default="default"),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("intent", sa.String(), nullable=False, server_default=""),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("source_task_id", sa.String(), nullable=False),
        sa.Column("times_seen", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "project", "fingerprint", name="uq_memory_per_project_fingerprint"
        ),
    )
    op.create_index("ix_task_memory_project", "task_memory", ["project"])
    op.create_index("ix_task_memory_fingerprint", "task_memory", ["fingerprint"])

    for column in _TASK_COLUMNS:
        op.add_column("tasks", column)


def downgrade() -> None:
    for column in reversed(_TASK_COLUMNS):
        op.drop_column("tasks", column.name)
    op.drop_index("ix_task_memory_fingerprint", table_name="task_memory")
    op.drop_index("ix_task_memory_project", table_name="task_memory")
    op.drop_table("task_memory")
    op.drop_index("ix_workflows_name", table_name="workflows")
    op.drop_table("workflows")
