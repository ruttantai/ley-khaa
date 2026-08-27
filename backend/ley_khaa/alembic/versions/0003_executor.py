"""phase 3: output bundle path and execution verdict

Revision ID: 0003_executor
Revises: 0002_autonomy
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_executor"
down_revision = "0002_autonomy"
branch_labels = None
depends_on = None

_TASK_COLUMNS = [
    sa.Column("workspace_path", sa.String(), nullable=True),
    sa.Column("execution_verdict", sa.JSON(), nullable=True),
]


def upgrade() -> None:
    for column in _TASK_COLUMNS:
        op.add_column("tasks", column)


def downgrade() -> None:
    for column in reversed(_TASK_COLUMNS):
        op.drop_column("tasks", column.name)
