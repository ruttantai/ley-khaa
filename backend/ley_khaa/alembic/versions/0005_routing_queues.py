"""phase 5: project routing, task leases and amendment proposals

Revision ID: 0005_routing_queues
Revises: 0004_registry_memory

Creates the projects and project_bindings TABLES. It does not seed the default
project row — startup does that (projects/seeds.py::ensure_default_project),
the same division the seed workflows use. Saying otherwise here would be the
false-statement class of defect that commit 8cebd1f cleaned up.
"""
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0005_routing_queues"
down_revision = "0004_registry_memory"
branch_labels = None
depends_on = None

# Annotated because the element types differ (String, DateTime, Integer, ...)
# and mypy would otherwise join them to `object`, which has neither a
# `.name` for downgrade() nor a type add_column() accepts. Annotation only:
# it emits no DDL and cannot change what this shipped revision does.
_TASK_COLUMNS: list[sa.Column[Any]] = [
    sa.Column("lease_owner", sa.String(), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("lease_attempts", sa.Integer(), nullable=False, server_default="0"),
]

_CANDIDATE_COLUMNS: list[sa.Column[Any]] = [
    sa.Column("amends_task_id", sa.String(), nullable=True),
    sa.Column("amendment_reason", sa.String(), nullable=True),
    sa.Column("amendment_confidence", sa.Float(), nullable=True),
]


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("display_name", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("description", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "project_bindings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("project", sa.String(), nullable=False),
        sa.Column("created_by_stage", sa.String(), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "client", "conversation_id", name="uq_binding_scope"),
    )
    op.create_index("ix_project_bindings_source", "project_bindings", ["source"])
    op.create_index("ix_project_bindings_client", "project_bindings", ["client"])
    op.create_index("ix_project_bindings_project", "project_bindings", ["project"])

    for column in _TASK_COLUMNS:
        op.add_column("tasks", column)
    for column in _CANDIDATE_COLUMNS:
        op.add_column("task_candidates", column)


def downgrade() -> None:
    # LIFO, matching 0002-0004: drop what this revision added, newest first.
    for column in reversed(_CANDIDATE_COLUMNS):
        op.drop_column("task_candidates", column.name)
    for column in reversed(_TASK_COLUMNS):
        op.drop_column("tasks", column.name)
    op.drop_index("ix_project_bindings_project", table_name="project_bindings")
    op.drop_index("ix_project_bindings_client", table_name="project_bindings")
    op.drop_index("ix_project_bindings_source", table_name="project_bindings")
    op.drop_table("project_bindings")
    op.drop_table("projects")
