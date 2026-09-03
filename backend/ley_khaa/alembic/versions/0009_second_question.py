"""phase 9 task 3: deliver a second, different clarifying question (item 17)

Revision ID: 0009_second_question
Revises: 0008_vision

Adds tasks.last_notified_question, the second half of the notification
compare-and-swap key alongside the existing last_notified_state. State alone
under-distinguishes a task that is asked a SECOND, DIFFERENT question without
ever leaving NEEDS_CLARIFICATION -- a reply is answered, the task is
re-interpreted, and a different field is still missing -- so the guard now
compares (state, question) together (TaskRepository.mark_notified).
"""
import sqlalchemy as sa
from alembic import op

revision = "0009_second_question"
down_revision = "0008_vision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column(
            "last_notified_question",
            sa.String(),
            nullable=True,
            server_default=sa.text("''"),
        ),
    )


def downgrade() -> None:
    op.drop_column("tasks", "last_notified_question")
