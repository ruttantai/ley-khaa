import pytest

from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _orchestrator(session) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
    )


def _blocked_task(session):
    """A task parked in needs_clarification: the request names no output format."""
    orchestrator = _orchestrator(session)
    result = orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "compare the holdings against the portfolio"}
    )
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.NEEDS_CLARIFICATION.value
    return orchestrator, task


def test_an_answer_unblocks_the_task_it_replies_to(session):
    orchestrator, task = _blocked_task(session)
    result = orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    assert result.replied_to_task_id == task.id
    refreshed = TaskRepository(session).get(task.id)
    assert refreshed.state == TaskState.AWAITING_APPROVAL.value
    assert refreshed.spec["output_format"] == "csv"


def test_an_answer_never_spawns_a_second_candidate(session):
    """The original candidate is terminal; stage B would happily start a new one."""
    orchestrator, task = _blocked_task(session)
    before = len(CandidateRepository(session).list_all())
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    assert len(CandidateRepository(session).list_all()) == before


def test_the_answer_is_attached_to_the_task(session):
    orchestrator, task = _blocked_task(session)
    result = orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    assert result.message_id in TaskRepository(session).get(task.id).source_message_ids


def test_the_answer_is_still_a_real_message_in_the_conversation(session):
    """It has to be, or a Slack-sourced answer would be invisible in the thread."""
    orchestrator, task = _blocked_task(session)
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    texts = [m.text for m in MessageRepository(session).list_for_conversation("conv-1")]
    assert "as a csv please" in texts


def test_each_answer_counts_a_clarification_round(session):
    orchestrator, task = _blocked_task(session)
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "still not sure", "reply_to_task_id": task.id}
    )
    assert TaskRepository(session).get(task.id).clarification_rounds == 1


def test_the_loop_gives_up_asking_after_three_rounds(session):
    """A model that keeps reporting the same gap must not ping-pong forever."""
    orchestrator, task = _blocked_task(session)
    for _ in range(4):
        orchestrator.ingest(
            {"conversation_id": "conv-1", "text": "no idea", "reply_to_task_id": task.id}
        )
    refreshed = TaskRepository(session).get(task.id)
    assert refreshed.state == TaskState.AWAITING_APPROVAL.value
    # The gaps stay visible even though we stopped asking about them.
    assert refreshed.spec["missing_fields"]
    assert refreshed.open_question is None


def test_a_reply_to_an_unknown_task_is_rejected(session):
    orchestrator = _orchestrator(session)
    with pytest.raises(KeyError):
        orchestrator.ingest(
            {"conversation_id": "conv-1", "text": "hello", "reply_to_task_id": "nope"}
        )


def test_a_reply_to_a_task_that_is_not_asking_is_attached_but_changes_nothing(session):
    orchestrator, task = _blocked_task(session)
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    parked = TaskRepository(session).get(task.id)
    assert parked.state == TaskState.AWAITING_APPROVAL.value
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "one more thought", "reply_to_task_id": task.id}
    )
    assert TaskRepository(session).get(task.id).state == TaskState.AWAITING_APPROVAL.value
