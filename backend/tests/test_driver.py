import pytest

from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.crystallizer.candidate import CandidateState
from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two universes",
        inputs=["bloomberg", "factset"],
        operation="set_difference",
        output_format="xlsx",
        certainty=0.95,
    )
    return TaskSpec(**{**base, **overrides})


def _setup(session, responses, *, candidate_id=None):
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(
        Message(source="s", client="c", conversation_id="conv-1", author="boss",
                text="compare bloomberg against factset")
    )
    task = repo.create(
        project="default", title="compare universes",
        source_message_ids=[row.id], candidate_id=candidate_id,
    )
    driver = TaskDriver(
        repo, llm=FakeLLM(responses), messages=messages,
        candidates=CandidateRepository(session),
    )
    return repo, driver, task


def test_a_low_risk_confident_task_runs_straight_through(session):
    """Auto skips the gate — this is the dial actually changing behaviour."""
    repo, driver, task = _setup(session, [_spec()])
    result = driver.advance(task.id)
    assert result.state == TaskState.DONE.value
    assert result.recommended_mode == AutonomyMode.AUTO.value


def test_a_risky_task_parks_for_a_human(session):
    repo, driver, task = _setup(session, [_spec(recipient="boss")])
    result = driver.advance(task.id)
    assert result.state == TaskState.AWAITING_APPROVAL.value
    assert result.recommended_mode == AutonomyMode.COPILOT.value
    assert "delivers" in result.autonomy_reason


def test_a_human_pinned_mode_beats_the_recommendation(session):
    repo, driver, task = _setup(session, [_spec(recipient="boss")])
    repo.set_override(task.id, AutonomyMode.AUTO.value)
    assert driver.advance(task.id).state == TaskState.DONE.value


def test_missing_fields_send_the_task_to_clarification(session):
    repo, driver, task = _setup(session, [_spec(missing_fields=["output_format"])])
    result = driver.advance(task.id)
    assert result.state == TaskState.NEEDS_CLARIFICATION.value
    assert "output_format" in result.open_question


def test_the_spec_is_persisted_before_the_gate(session):
    repo, driver, task = _setup(session, [_spec(recipient="boss")])
    driver.advance(task.id)
    assert TaskSpec.model_validate(repo.get(task.id).spec).operation == "set_difference"


def test_a_malformed_spec_asks_the_human_rather_than_failing(session):
    """A task a human could rescue must not be marked failed."""
    from pydantic import ValidationError

    bad = ValidationError.from_exception_data("TaskSpec", [])
    repo, driver, task = _setup(session, [bad, bad])
    result = driver.advance(task.id)
    assert result.state == TaskState.NEEDS_CLARIFICATION.value


def test_a_transport_failure_leaves_the_task_retryable(session):
    repo, driver, task = _setup(session, [ConnectionError("boom")])
    result = driver.advance(task.id)
    assert result.state == TaskState.CLASSIFIED.value
    assert result.interpret_attempts == 1


def test_repeated_transport_failures_eventually_fail_the_task(session):
    repo, driver, task = _setup(session, [ConnectionError("boom")] * 3)
    for _ in range(3):
        driver.advance(task.id)
    result = repo.get(task.id)
    assert result.state == TaskState.FAILED.value
    assert "unavailable" in result.failure_reason


def test_a_lost_race_on_the_final_transport_failure_does_not_stamp_the_task(session):
    """M4: _interpret used to record_failure() before claim() validated the
    transition — the same inversion c043c46 fixed in reject(). Simulate the
    race: another caller already moved the task to FAILED for an unrelated
    reason (e.g. rejected it) between when this driver read its stale row and
    when its own retry-exhausted interpretation attempt lands. The stale
    caller's final claim must lose, and losing must mean it never touches
    failure_reason.
    """
    repo, driver, task = _setup(session, [ConnectionError("boom")])
    assert repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.increment_interpret_attempts(task.id)
    repo.increment_interpret_attempts(task.id)
    stale_row = repo.get(task.id)  # attempts == 2, state == CLASSIFIED (this driver's view)

    # A concurrent caller wins the race first.
    assert repo.claim(task.id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED)
    repo.record_failure(task.id, "failed for an unrelated reason")

    # This driver's in-flight interpret attempt (3rd) now fails too, working
    # off its stale snapshot of the row.
    driver._interpret(stale_row)

    result = repo.get(task.id)
    assert result.state == TaskState.FAILED.value
    assert result.failure_reason == "failed for an unrelated reason"


def test_the_candidates_unsettled_details_lower_confidence(session):
    candidates = CandidateRepository(session)
    candidate = candidates.upsert(
        conversation_id="conv-1", candidate_key="k", title="t", summary="s",
        state=CandidateState.READY, message_ids=[], missing_fields=["deadline"],
        open_question=None,
    )
    repo, driver, task = _setup(session, [_spec()], candidate_id=candidate.id)
    result = driver.advance(task.id)
    assert result.confidence < 0.95


def test_advance_on_a_finished_task_is_a_no_op(session):
    repo, driver, task = _setup(session, [_spec()])
    driver.advance(task.id)
    # No responses left: a second advance must not call the LLM again.
    assert driver.advance(task.id).state == TaskState.DONE.value


def test_advance_on_an_unknown_task_raises(session):
    _, driver, _ = _setup(session, [])
    with pytest.raises(KeyError):
        driver.advance("nope")
