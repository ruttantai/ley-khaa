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


def test_approve_releases_a_parked_task(session):
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


def test_overriding_to_auto_releases_the_task_on_the_spot(session):
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
    repo.set_override(task.id, AutonomyMode.SUGGEST.value)
    driver = TaskDriver(repo, llm=FakeLLM([_spec()]), messages=messages,
                        candidates=CandidateRepository(session))

    result = driver.advance(task.id)

    assert result.state == TaskState.AWAITING_APPROVAL.value
    assert result.recommended_mode == AutonomyMode.AUTO.value
    assert result.effective_mode == AutonomyMode.SUGGEST.value


def test_clearing_the_override_falls_back_to_the_recommendation(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    repo.set_override(task.id, AutonomyMode.SUGGEST.value)
    driver.override(task.id, None)
    assert repo.get(task.id).effective_mode == AutonomyMode.COPILOT.value


def test_editing_the_spec_rescores_and_can_change_the_recommendation(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    assert repo.get(task.id).recommended_mode == AutonomyMode.COPILOT.value
    # Removing the recipient removes the delivery risk that held it back.
    result = driver.edit_spec(task.id, {"recipient": None})
    assert result.recommended_mode == AutonomyMode.AUTO.value


def test_editing_does_not_re_run_the_interpreter(session):
    """The human's correction is authoritative; re-interpreting would undo it."""
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    driver.interpreter.llm = FakeLLM([])  # any call would assert-fail
    driver.edit_spec(task.id, {"output_format": "csv"})
    assert TaskSpec.model_validate(repo.get(task.id).spec).output_format == "csv"


def test_a_misspelled_patch_key_is_rejected(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    with pytest.raises(ValidationError):
        driver.edit_spec(task.id, {"outupt_format": "csv"})


def test_editing_a_task_with_no_spec_yet_is_a_conflict(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    fresh = repo.create(project="default", title="t3", source_message_ids=[])
    with pytest.raises(InvalidTransition):
        driver.edit_spec(fresh.id, {"output_format": "csv"})
