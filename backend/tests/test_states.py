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
