import pytest

from ley_khaa.domain.states import (
    TaskState,
    can_transition,
    ensure_transition,
    InvalidTransition,
)


def test_valid_transition_allowed():
    assert can_transition(TaskState.RECEIVED, TaskState.CLASSIFIED) is True


def test_invalid_transition_rejected():
    assert can_transition(TaskState.RECEIVED, TaskState.DONE) is False


def test_terminal_states_have_no_transitions():
    assert can_transition(TaskState.DONE, TaskState.EXECUTING) is False
    assert can_transition(TaskState.FAILED, TaskState.RECEIVED) is False


def test_ensure_transition_raises_on_invalid():
    with pytest.raises(InvalidTransition):
        ensure_transition(TaskState.RECEIVED, TaskState.DONE)


def test_full_stub_path_is_valid():
    path = [
        TaskState.RECEIVED,
        TaskState.CLASSIFIED,
        TaskState.INTERPRETED,
        TaskState.EXECUTING,
        TaskState.VALIDATING,
        TaskState.DONE,
    ]
    for current, target in zip(path, path[1:]):
        assert can_transition(current, target) is True


def test_interpreter_can_escalate_to_clarification():
    assert can_transition(TaskState.CLASSIFIED, TaskState.NEEDS_CLARIFICATION)
    assert can_transition(TaskState.INTERPRETED, TaskState.NEEDS_CLARIFICATION)


def test_an_answered_clarification_goes_back_to_be_re_interpreted():
    assert can_transition(TaskState.NEEDS_CLARIFICATION, TaskState.CLASSIFIED)


def test_editing_a_parked_spec_re_enters_scoring():
    assert can_transition(TaskState.AWAITING_APPROVAL, TaskState.INTERPRETED)


def test_terminal_states_stay_terminal():
    for state in TaskState:
        assert not can_transition(TaskState.DONE, state)
        assert not can_transition(TaskState.FAILED, state)
