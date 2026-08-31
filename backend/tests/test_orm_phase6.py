from datetime import datetime

from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.orm import DeadLetterRow, TaskRow
from ley_khaa.persistence.repository import TaskRepository


def test_a_dead_letter_row_round_trips(session):
    row = DeadLetterRow(
        id="dl-1",
        source="slack",
        kind="inbound",
        reason="no text in the event",
        payload='{"event": {"type": "message"}}',
    )
    session.add(row)
    session.commit()

    stored = session.get(DeadLetterRow, "dl-1")
    assert stored.source == "slack"
    assert stored.kind == "inbound"
    assert stored.reason == "no text in the event"
    assert stored.payload == '{"event": {"type": "message"}}'
    assert stored.created_at.tzinfo is not None or isinstance(stored.created_at, datetime)


def test_a_new_task_has_never_been_notified(session):
    """The column must default to NULL, not to a state — a fresh task has
    announced nothing, and a non-null default would suppress its first
    notification."""
    row = TaskRepository(session).create(project="default", title="t", source_message_ids=[])
    assert row.last_notified_state is None


def test_last_notified_state_persists(session):
    """Asserted through SQL, not through the identity map.

    The `session` fixture uses expire_on_commit=False, so a plain attribute
    assignment on an already-loaded row survives commit()/refresh() even when
    nothing is mapped to a column — which is how the obvious version of this
    test passes with the column deleted. Reading the value back with raw SQL is
    what makes it fail for the right reason.
    """
    from sqlalchemy import text as sql_text

    repo = TaskRepository(session)
    row = repo.create(project="default", title="t", source_message_ids=[])
    stored = session.get(TaskRow, row.id)
    stored.last_notified_state = TaskState.DONE.value
    session.commit()

    persisted = session.execute(
        sql_text("SELECT last_notified_state FROM tasks WHERE id = :id"), {"id": row.id}
    ).scalar_one()
    assert persisted == "done"
