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
    message so it has a source, a conversation and an external id to answer to."""
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


def test_mark_notified_wins_once_per_state(session):
    repo = TaskRepository(session)
    row = repo.create(project="default", title="t", source_message_ids=[])

    assert repo.mark_notified(row.id, "done") is True
    assert repo.mark_notified(row.id, "done") is False, "a re-drive must not re-announce"
    assert repo.mark_notified(row.id, "failed") is True, "a NEW state may be announced"


def test_mark_notified_on_an_unknown_task_is_false_not_an_error(session):
    assert TaskRepository(session).mark_notified("nope", "done") is False


def test_a_parked_task_asks_its_question_in_its_own_conversation(session):
    notifier = RecordingNotifier()
    _orchestrator_, task = _blocked(session, notifier)

    assert len(notifier.sent) == 1
    dest, text = notifier.sent[0]
    assert dest.source == "slack"
    assert dest.conversation_id == "slack:T1:C1:100.1"
    assert dest.external_id == "slack:C1:100.1"
    assert task.open_question in text


def test_re_driving_the_same_state_does_not_repeat_the_question(session):
    """advance() is re-entrant and the sweeper re-drives tasks, so without the
    guard a parked task would repeat its question on every pass."""
    notifier = RecordingNotifier()
    orchestrator, task = _blocked(session, notifier)

    orchestrator.driver.advance(task.id)
    orchestrator.driver.advance(task.id)

    assert len(notifier.sent) == 1


def test_a_new_state_is_announced_even_after_an_earlier_one(session):
    notifier = RecordingNotifier()
    orchestrator, task = _blocked(session, notifier)

    orchestrator.driver.reject(task.id, "not needed after all")

    assert len(notifier.sent) == 2
    assert "not needed after all" in notifier.sent[1][1]


def test_rejection_notifies_even_though_it_never_calls_advance(session):
    """reject() moves a task to FAILED on its own. If _announce were only wired
    into advance(), the human would never be told."""
    notifier = RecordingNotifier()
    orchestrator, task = _blocked(session, notifier)
    notifier.sent.clear()

    orchestrator.driver.reject(task.id, "duplicate")

    assert [t for _d, t in notifier.sent if "duplicate" in t]


def test_a_task_with_no_source_messages_is_not_announced(session, caplog):
    """There is nowhere to answer. It must be a silent skip, not a crash.

    notifier.sent == [] alone is a masked assertion here: _announce's blanket
    except Exception would produce the exact same empty list if _destination's
    empty-sources guard were missing and `sources[0]` raised IndexError instead
    of returning None cleanly. The caplog check tells "skipped on purpose" apart
    from "crashed and got swallowed".
    """
    notifier = RecordingNotifier()
    repo = TaskRepository(session)
    orchestrator = _orchestrator(session, notifier)
    row = repo.create(project="default", title="orphan", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION)

    with caplog.at_level("ERROR"):
        orchestrator.driver.advance(row.id)

    assert notifier.sent == []
    assert orchestrator.driver._destination(repo.get(row.id)) is None
    assert not [r for r in caplog.records if "could not announce" in r.getMessage()]


def test_a_raising_notifier_never_fails_the_task(session):
    """Spec §3.6: outbound work never fails a task. A wedged Slack API must not
    be able to stop work from completing."""

    class Exploding:
        name = "exploding"

        def notify(self, dest, text):
            raise RuntimeError("slack is down")

    orchestrator, task = _blocked(session, Exploding())
    assert TaskRepository(session).get(task.id).state == TaskState.NEEDS_CLARIFICATION.value


def test_the_default_notifier_is_null_so_nothing_existing_changes(session):
    orchestrator = _orchestrator(session)
    assert orchestrator.driver.notifier.name == "null"
