"""Backlog item 17: a second clarifying question is delivered (spec §3.3/§8).

`TaskRepository.mark_notified` gates announcements on a STATE change
(repository.py:334-335). A task can be asked a SECOND, DIFFERENT question
without ever leaving NEEDS_CLARIFICATION in between -- the human answers, the
task is re-interpreted, and it comes right back to NEEDS_CLARIFICATION with a
different open_question. Because the announced *state* never changed, the old
guard silently ate the second question; the human saw it only by looking at
the dashboard, which is exactly what spec §8's "every human-facing state is
announced" claim says cannot happen.

This file has two halves, matching the two directions the fix must not get
wrong:

  * the repository-level guard itself (mark_notified), tested directly, and
  * the real end-to-end path through TaskDriver.advance(), mirroring
    test_notifier_wiring.py's `_blocked` construction so this exercises the
    actual guard wired into the driver, not a reimplementation of it.

Mirrors backend/tests/test_notifier_wiring.py for the orchestrator/driver
construction and backend/tests/test_notification_policy.py for the states
under test.
"""

from ley_khaa.adapters.notifier import RecordingNotifier
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _orchestrator(session, notifier=None) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
        notifier=notifier,
    )


def _blocked(session, notifier):
    """A task parked in needs_clarification, arriving from a channel-shaped
    message so it has a source, a conversation and an external id to answer
    to. Copied verbatim from test_notifier_wiring.py so this test exercises
    the same real construction, not a hand-rolled stand-in."""
    orchestrator = _orchestrator(session, notifier)
    result = orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": "slack:T1:C1:100.1",
            "external_id": "slack:C1:100.1",
            "author": "U1",
            "text": "compare the holdings against the portfolio",
        }
    )
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.NEEDS_CLARIFICATION.value
    return orchestrator, task


# --- repository guard, directly -------------------------------------------


def test_mark_notified_wins_for_a_second_different_question_in_the_same_state(session):
    """The fix: a question change within the SAME state must win the CAS,
    not just a state change."""
    repo = TaskRepository(session)
    row = repo.create(project="default", title="t", source_message_ids=[])

    assert repo.mark_notified(row.id, "needs_clarification", "which format?") is True
    assert (
        repo.mark_notified(row.id, "needs_clarification", "which output format?") is True
    ), "a different question in the same state must be announced"


def test_mark_notified_still_suppresses_the_same_question_repeated(session):
    """The property the guard exists for: a re-entrant advance() driving the
    same state with the SAME question must not re-announce it."""
    repo = TaskRepository(session)
    row = repo.create(project="default", title="t", source_message_ids=[])

    assert repo.mark_notified(row.id, "needs_clarification", "which format?") is True
    assert (
        repo.mark_notified(row.id, "needs_clarification", "which format?") is False
    ), "the identical question repeated must not re-notify"


def test_mark_notified_treats_two_missing_questions_as_the_same_question(session):
    """Both NULL and "" mean 'no question text' -- the fallback prompt in
    message_for() covers either. This must not be misread as "changed"."""
    repo = TaskRepository(session)
    row = repo.create(project="default", title="t", source_message_ids=[])

    assert repo.mark_notified(row.id, "needs_clarification", None) is True
    assert repo.mark_notified(row.id, "needs_clarification", None) is False


# --- the real path: TaskDriver.advance() ----------------------------------


def test_a_second_different_question_in_the_same_state_is_delivered(session):
    notifier = RecordingNotifier()
    orchestrator, task = _blocked(session, notifier)
    assert len(notifier.sent) == 1
    first_question = task.open_question

    # A different question is asked while the task is STILL in
    # needs_clarification -- no intervening state change, exactly the gap
    # backlog item 17 describes. (edit_spec/reply flows are one real way this
    # happens; set_open_question directly isolates the guard from the
    # interpreter's behaviour, same spirit as test_notifier_wiring.py driving
    # the guard through repeated advance() calls.)
    repo = TaskRepository(session)
    second_question = "actually -- which output format do you want?"
    assert second_question != first_question
    repo.set_open_question(task.id, second_question)

    orchestrator.driver.advance(task.id)

    assert len(notifier.sent) == 2, "the second, different question must be delivered"
    assert second_question in notifier.sent[1][1]


def test_re_driving_with_the_same_question_still_does_not_repeat_it(session):
    """The property the fix must not break, exercised through the driver
    rather than the repository directly: re-entrant advance() calls with an
    UNCHANGED question must still be silent after the first announcement."""
    notifier = RecordingNotifier()
    orchestrator, task = _blocked(session, notifier)
    assert len(notifier.sent) == 1

    orchestrator.driver.advance(task.id)
    orchestrator.driver.advance(task.id)

    assert len(notifier.sent) == 1, "an unchanged question must not be re-announced"
