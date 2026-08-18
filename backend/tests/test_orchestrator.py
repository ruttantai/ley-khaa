from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.repository import TaskRepository


def _msg(text="Compare Bloomberg vs FactSet and send what's missing."):
    return Message(source="simulator", client="demo", conversation_id="c1", author="boss", text=text)


def test_ingest_reaches_done(session):
    orch = Orchestrator(TaskRepository(session))
    task = orch.ingest(_msg())
    assert task.state == TaskState.DONE.value


def test_ingest_records_source_message_and_title(session):
    orch = Orchestrator(TaskRepository(session))
    m = _msg("Reconcile the holdings list please")
    task = orch.ingest(m)
    assert task.source_message_ids == [m.id]
    assert task.title == "Reconcile the holdings list please"
