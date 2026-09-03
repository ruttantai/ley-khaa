import warnings

import pytest

from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import ForeignReplyTarget, Orchestrator
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
    """The original candidate is terminal; stage B would happily start a new one.

    The reply text is deliberately chosen to contain request words the
    heuristic's stage A/B would genuinely claim on the normal path ("export",
    "report", "csv") — unlike plain "as a csv please", which stage A would call
    irrelevant on its own and so would never form a candidate from regardless
    of whether routing worked. A test that can't fail on the regression it
    exists to catch is not a guard.
    """
    orchestrator, task = _blocked_task(session)
    before = CandidateRepository(session).list_all()
    before_keys = {c.candidate_key for c in before}
    orchestrator.ingest(
        {
            "conversation_id": "conv-1",
            "text": "export it as a csv report please",
            "reply_to_task_id": task.id,
        }
    )
    after = CandidateRepository(session).list_all()
    after_keys = {c.candidate_key for c in after}
    assert len(after) == len(before)
    assert after_keys == before_keys


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


def test_a_reply_to_an_unknown_task_does_not_pollute_the_window(session):
    """I1: gateway.accept() already committed this message before the KeyError
    is raised. Leaving its `relevant` column NULL would make
    window(exclude_noise=True) — what the crystallizer actually reads — keep it
    forever, since NULL means "not yet judged" there.
    """
    orchestrator = _orchestrator(session)
    with pytest.raises(KeyError):
        orchestrator.ingest(
            {"conversation_id": "conv-1", "text": "hello", "reply_to_task_id": "nope"}
        )
    messages = MessageRepository(session)
    [orphan] = messages.list_for_conversation("conv-1")
    assert orphan.relevant is False
    assert orphan not in messages.window("conv-1", exclude_noise=True)


def test_a_reply_naming_a_task_from_another_conversation_is_rejected(session):
    """I2: a reply must only ever extend the task it names with a message from
    that task's own conversation, or a foreign conversation's message id could
    join the task's source_message_ids — and from there, the spec the
    interpreter reads on its next pass.
    """
    orchestrator, task = _blocked_task(session)  # task's messages live in conv-1
    before = TaskRepository(session).get(task.id).source_message_ids
    with pytest.raises(ForeignReplyTarget):
        orchestrator.ingest(
            {
                "conversation_id": "conv-2",
                "text": "as a csv please",
                "reply_to_task_id": task.id,
            }
        )
    after = TaskRepository(session).get(task.id).source_message_ids
    assert after == before

    messages = MessageRepository(session)
    [foreign] = messages.list_for_conversation("conv-2")
    assert foreign.relevant is False
    assert foreign not in messages.window("conv-2", exclude_noise=True)


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


def test_a_reply_naming_no_task_at_all_is_orphaned_not_looked_up(session):
    """Both callers of _route_reply set reply_to_task_id before routing, so this
    is the branch that exists for when one stops doing so.

    A None id must take the same orphan path as an unknown one rather than
    reaching the repository: `Session.get(TaskRow, None)` answers None but warns
    "fully NULL primary key identity cannot load any object", which SQLAlchemy
    documents as a candidate for becoming an error. A warning is not a lookup,
    and this project's bar is zero warnings.
    """
    orchestrator = _orchestrator(session)
    messages = MessageRepository(session)
    row = messages.add(
        Message(
            id="m-orphan",
            source="dashboard",
            client="test",
            conversation_id="conv-1",
            author="human",
            text="hello",
        )
    )
    assert row.reply_to_task_id is None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(KeyError):
            orchestrator._route_reply(row)
    assert not [w for w in caught if "NULL primary key" in str(w.message)]
    assert messages.list_for_conversation("conv-1")[0].relevant is False
