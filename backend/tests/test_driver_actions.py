import pytest
from pydantic import ValidationError

from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.domain.states import InvalidTransition, TaskState
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.domain.models import Message
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two universes", inputs=["bloomberg", "factset"],
        operation="set_difference", output_format="xlsx", certainty=0.95,
    )
    return TaskSpec(**{**base, **overrides})


def _parked(session, responses):
    """A task sitting at awaiting_approval, which is where humans meet it."""
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(Message(source="s", client="c", conversation_id="conv-1",
                               author="boss", text="compare bloomberg against factset"))
    task = repo.create(project="default", title="t", source_message_ids=[row.id])
    driver = TaskDriver(repo, llm=FakeLLM(responses), messages=messages,
                        candidates=CandidateRepository(session))
    driver.advance(task.id)
    return repo, driver, task


def test_approve_releases_a_parked_task(session, stub_execution):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    assert repo.get(task.id).state == TaskState.AWAITING_APPROVAL.value
    assert driver.approve(task.id).state == TaskState.DONE.value


def test_reject_fails_the_task_with_a_reason(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    result = driver.reject(task.id, "not what I asked for")
    assert result.state == TaskState.FAILED.value
    assert result.failure_reason == "not what I asked for"


def test_approving_twice_is_a_conflict_not_a_crash(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    driver.approve(task.id)
    with pytest.raises(InvalidTransition):
        driver.approve(task.id)


def test_rejecting_a_finished_task_does_not_corrupt_its_record(session, stub_execution):
    """A refused rejection must be a no-op, not a partial write.

    reject() used to call record_failure() before claim() validated the
    transition, so a caller that rejected an already-DONE task would get
    InvalidTransition (reasonably read as "nothing happened") while
    failure_reason was permanently stamped onto a task that actually
    succeeded. claim() must run first.
    """
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    driver.approve(task.id)
    assert repo.get(task.id).state == TaskState.DONE.value

    with pytest.raises(InvalidTransition):
        driver.reject(task.id, "actually this was wrong")

    result = repo.get(task.id)
    assert result.state == TaskState.DONE.value
    assert result.failure_reason is None


def test_reject_from_needs_clarification_fails_the_task(session):
    """M3: a task stuck asking a question must still be killable — NEEDS_
    CLARIFICATION -> FAILED is already legal in the state table (states.py);
    reject() just never tried it."""
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(Message(source="s", client="c", conversation_id="conv-1",
                               author="boss", text="compare bloomberg against factset"))
    task = repo.create(project="default", title="t", source_message_ids=[row.id])
    driver = TaskDriver(
        repo,
        llm=FakeLLM([_spec(recipient=None, missing_fields=["recipient"])]),
        messages=messages,
        candidates=CandidateRepository(session),
    )
    driver.advance(task.id)
    assert repo.get(task.id).state == TaskState.NEEDS_CLARIFICATION.value

    result = driver.reject(task.id, "cannot answer this")

    assert result.state == TaskState.FAILED.value
    assert result.failure_reason == "cannot answer this"


def test_editing_a_finished_task_is_a_conflict(session, stub_execution):
    """I3: PATCH .../spec must not rewrite the spec of completed work — the same
    class of bug c043c46 fixed for reject()."""
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    driver.approve(task.id)
    assert repo.get(task.id).state == TaskState.DONE.value

    with pytest.raises(InvalidTransition):
        driver.edit_spec(task.id, {"output_format": "csv"})

    assert TaskSpec.model_validate(repo.get(task.id).spec).output_format == "xlsx"


def test_overriding_the_mode_of_a_finished_task_is_a_conflict(session, stub_execution):
    """I3: POST .../mode must not stamp mode_override on a finished task."""
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    driver.approve(task.id)
    assert repo.get(task.id).state == TaskState.DONE.value

    with pytest.raises(InvalidTransition):
        driver.override(task.id, AutonomyMode.AUTO)

    assert repo.get(task.id).mode_override is None


def test_overriding_to_auto_releases_the_task_on_the_spot(session, stub_execution):
    """This is the dial having teeth: one click moves a parked task."""
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    result = driver.override(task.id, AutonomyMode.AUTO)
    assert result.state == TaskState.DONE.value
    assert result.mode_override == AutonomyMode.AUTO.value


def test_overriding_to_suggest_keeps_the_task_parked(session):
    """A task that would score Auto stays parked when a human has pinned Suggest.

    The spec here has no recipient, so on its own it scores low risk / high
    confidence and would recommend Auto (see test_overriding_to_auto_releases_
    the_task_on_the_spot's counterpart with a recipient, which scores Co-pilot).
    Pinning the override to Suggest *before* the task is scored must still win:
    the recommendation is computed as Auto, but the effective mode — and where
    the task actually lands — is Suggest.
    """
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(Message(source="s", client="c", conversation_id="conv-1",
                               author="boss", text="compare bloomberg against factset"))
    task = repo.create(project="default", title="t", source_message_ids=[row.id])
    repo.set_override(task.id, AutonomyMode.SUGGEST.value, expected=TaskState.RECEIVED)
    driver = TaskDriver(repo, llm=FakeLLM([_spec()]), messages=messages,
                        candidates=CandidateRepository(session))

    result = driver.advance(task.id)

    assert result.state == TaskState.AWAITING_APPROVAL.value
    assert result.recommended_mode == AutonomyMode.AUTO.value
    assert result.effective_mode == AutonomyMode.SUGGEST.value


def test_clearing_the_override_falls_back_to_the_recommendation(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    repo.set_override(
        task.id, AutonomyMode.SUGGEST.value, expected=TaskState.AWAITING_APPROVAL
    )
    driver.override(task.id, None)
    assert repo.get(task.id).effective_mode == AutonomyMode.COPILOT.value


def test_editing_the_spec_rescores_and_can_change_the_recommendation(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    assert repo.get(task.id).recommended_mode == AutonomyMode.COPILOT.value
    # Removing the recipient removes the delivery risk that held it back.
    result = driver.edit_spec(task.id, {"recipient": None})
    assert result.recommended_mode == AutonomyMode.AUTO.value


def test_editing_does_not_re_run_the_interpreter(session):
    """The human's correction is authoritative; re-interpreting would undo it.

    This covers the awaiting_approval path. What actually guards it here is
    the state machine, not the FakeLLM: awaiting_approval -> classified is not
    a legal transition (see domain/states.py), so edit_spec's claim() to
    INTERPRETED is the only way forward and _interpret() is never reached.
    The empty FakeLLM([]) is a belt-and-braces trip wire, not the mechanism
    that proves this — if _interpret() ever did run, any of its .parse()
    calls would raise AssertionError. The needs_clarification path, where
    re-entering CLASSIFIED *is* legal, is covered separately by
    test_editing_from_needs_clarification_moves_to_awaiting_approval, which
    asserts the resulting state directly rather than relying on a call
    failing loudly.
    """
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    driver.interpreter.llm = FakeLLM([])  # trip wire: any call would assert-fail
    driver.edit_spec(task.id, {"output_format": "csv"})
    assert TaskSpec.model_validate(repo.get(task.id).spec).output_format == "csv"


def test_editing_from_needs_clarification_moves_to_awaiting_approval(session):
    """edit_spec on a needs_clarification task also re-enters scoring, not
    interpretation.

    Unlike the awaiting_approval case, classified is a legal target from
    needs_clarification (see domain/states.py), so an accidental
    re-interpretation wouldn't be caught by the state machine here — it has
    to be caught by asserting where the task actually lands. A fill-in edit
    must carry it through INTERPRETED to AWAITING_APPROVAL, not leave it at
    (or send it back to) a state that would imply the interpreter ran again.
    """
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(Message(source="s", client="c", conversation_id="conv-1",
                               author="boss", text="compare bloomberg against factset"))
    task = repo.create(project="default", title="t", source_message_ids=[row.id])
    driver = TaskDriver(
        repo,
        llm=FakeLLM([_spec(recipient=None, missing_fields=["recipient"])]),
        messages=messages,
        candidates=CandidateRepository(session),
    )
    driver.advance(task.id)
    assert repo.get(task.id).state == TaskState.NEEDS_CLARIFICATION.value

    result = driver.edit_spec(task.id, {"recipient": "boss"})

    assert result.state == TaskState.AWAITING_APPROVAL.value


def test_a_misspelled_patch_key_is_rejected(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    with pytest.raises(ValidationError):
        driver.edit_spec(task.id, {"outupt_format": "csv"})


def test_editing_a_task_with_no_spec_yet_is_a_conflict(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    fresh = repo.create(project="default", title="t3", source_message_ids=[])
    with pytest.raises(InvalidTransition):
        driver.edit_spec(fresh.id, {"output_format": "csv"})


def test_an_override_that_loses_the_race_is_reported_not_silently_applied(
    session, stub_execution, monkeypatch
):
    """override() checks the state and then writes: two statements, one window.

    A dispatcher worker can finish the task in between. Unconditional, the write
    landed on the finished row — so `mode_override` claimed the human's choice
    was in force on work that had already run under a different mode. That is
    worse than losing the instruction, because the record then says it was
    honoured. The loss must reach the caller (a 409 at the route), not the row.
    """
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    real = TaskRepository.set_override

    def racing(self, task_id, mode, *, expected):
        # Injected here because the window is exactly one statement wide.
        for source, target in (
            (TaskState.AWAITING_APPROVAL, TaskState.EXECUTING),
            (TaskState.EXECUTING, TaskState.VALIDATING),
            (TaskState.VALIDATING, TaskState.DONE),
        ):
            self.claim(task_id, expected=source, target=target)
        return real(self, task_id, mode, expected=expected)

    monkeypatch.setattr(TaskRepository, "set_override", racing)

    with pytest.raises(InvalidTransition):
        driver.override(task.id, AutonomyMode.AUTO)

    finished = repo.get(task.id)
    assert TaskState(finished.state) is TaskState.DONE
    assert finished.mode_override is None, "the mode was stamped on finished work"
