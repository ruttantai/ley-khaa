from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.orm import TaskRow


def test_message_autofills_id_and_timestamp():
    m = Message(source="simulator", client="demo", conversation_id="c1", author="u", text="hi")
    assert m.id
    assert m.timestamp is not None


def test_task_row_persists_and_reads_back(session):
    row = TaskRow(id="t1", project="default", state=TaskState.RECEIVED.value, title="hi", source_message_ids=["m1"])
    session.add(row)
    session.commit()
    fetched = session.get(TaskRow, "t1")
    assert fetched.state == "received"
    assert fetched.source_message_ids == ["m1"]
    assert fetched.created_at is not None
