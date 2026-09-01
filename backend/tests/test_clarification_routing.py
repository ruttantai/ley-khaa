from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository

CONV = "slack:T1:C1:100.1"


def _orchestrator(session) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
    )


def _blocked(session, conversation_id=CONV):
    orchestrator = _orchestrator(session)
    result = orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": conversation_id,
            "text": "compare the holdings against the portfolio",
        }
    )
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.NEEDS_CLARIFICATION.value
    return orchestrator, task


def test_a_plain_message_in_a_clarifying_conversation_answers_the_question(session):
    """The headline of §3.7: nobody types a task id into Slack."""
    orchestrator, task = _blocked(session)

    result = orchestrator.ingest(
        {"source": "slack", "client": "T1", "conversation_id": CONV, "text": "as a csv please"}
    )

    assert result.replied_to_task_id == task.id
    refreshed = TaskRepository(session).get(task.id)
    assert refreshed.state == TaskState.AWAITING_APPROVAL.value
    assert refreshed.spec["output_format"] == "csv"


def test_the_answer_is_recorded_as_a_reply_in_the_database(session):
    """The same row shape POST /tasks/{id}/answer produces, so the two paths
    cannot drift apart and an audit shows the message for what it is."""
    orchestrator, task = _blocked(session)

    result = orchestrator.ingest(
        {"source": "slack", "client": "T1", "conversation_id": CONV, "text": "as a csv please"}
    )

    row = MessageRepository(session).get_many([result.message_id])[0]
    assert row.reply_to_task_id == task.id

    # Re-read from the DATABASE, not the identity map. Without the expunge this
    # assertion cannot tell a committed write from an attribute set on an
    # attached object — SQLAlchemy's unit of work would flush either one, so the
    # test would pass for a reason weaker than the one it is named for.
    session.expunge_all()
    reloaded = MessageRepository(session).get_many([result.message_id])[0]
    assert reloaded.reply_to_task_id == task.id


def test_the_answer_never_spawns_a_second_candidate(session):
    """The original candidate is PROMOTED, which is terminal, so stage B would
    happily start a new one for the same request. The reply text deliberately
    contains words stage A would otherwise claim."""
    orchestrator, _task = _blocked(session)
    before = {c.candidate_key for c in CandidateRepository(session).list_all()}

    orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": CONV,
            "text": "export it as a csv report please",
        }
    )

    assert {c.candidate_key for c in CandidateRepository(session).list_all()} == before


def test_a_message_in_a_conversation_with_no_clarifying_task_is_unaffected(session):
    """§3.7 explicitly: it flows on to the crystallizer and the amendment
    detector as before."""
    orchestrator = _orchestrator(session)
    result = orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": "slack:T1:C1:900.1",
            "text": "compare the Bloomberg universe against FactSet and send it as an Excel file",
        }
    )
    assert result.replied_to_task_id is None
    assert result.task_ids


def test_a_clarifying_task_in_another_conversation_is_not_answered(session):
    """Two channels, two tasks. A message in one must never answer the other's
    question — that would attach a foreign message to a task's source set."""
    orchestrator, task_a = _blocked(session, conversation_id=CONV)
    other = "slack:T1:C2:200.1"

    result = orchestrator.ingest(
        {"source": "slack", "client": "T1", "conversation_id": other, "text": "as a csv please"}
    )

    assert result.replied_to_task_id is None
    assert TaskRepository(session).get(task_a.id).state == TaskState.NEEDS_CLARIFICATION.value


def test_an_explicit_reply_target_still_wins(session):
    """The dashboard names a task id, and that must keep beating inference."""
    orchestrator, task = _blocked(session)

    result = orchestrator.ingest(
        {
            "source": "dashboard",
            "client": "T1",
            "conversation_id": CONV,
            "text": "as a csv please",
            "reply_to_task_id": task.id,
        }
    )

    assert result.replied_to_task_id == task.id


def test_a_second_message_after_the_question_is_answered_forms_work_again(session):
    """Once the task leaves needs_clarification there is nothing to answer, so
    the conversation goes back to producing candidates. Without this the first
    parked task in a channel would swallow every later message forever."""
    orchestrator, _task = _blocked(session)
    orchestrator.ingest(
        {"source": "slack", "client": "T1", "conversation_id": CONV, "text": "as a csv please"}
    )

    result = orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": CONV,
            "text": "also compare the Bloomberg universe against FactSet as an Excel file",
        }
    )

    assert result.replied_to_task_id is None


def test_the_most_recently_updated_clarifying_task_is_the_one_answered(session):
    """Two parked tasks in one conversation is possible (the simulator's split
    request produces exactly that). The tie-break must be deterministic, or the
    answer lands on an arbitrary one."""
    orchestrator, first = _blocked(session)
    repo = TaskRepository(session)
    second = repo.create(
        project="default", title="second", source_message_ids=list(first.source_message_ids)
    )
    repo.claim(second.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(second.id, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION)

    assert orchestrator._clarifying_task_in(CONV).id == second.id
