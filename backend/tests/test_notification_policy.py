import pytest

from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.notifier import (
    NOTIFY_STATES,
    NullNotifier,
    RecordingNotifier,
    current_notifier,
    message_for,
    set_notifier,
)
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.orm import TaskRow


def _row(state: TaskState, **fields) -> TaskRow:
    return TaskRow(id="t1", project="default", state=state.value, title="universe check", **fields)


# Exactly the four states in §3.6's table, and no others. Table-driven so a
# fifth state added later has to be added here deliberately.
_SILENT = [
    TaskState.RECEIVED,
    TaskState.CLASSIFIED,
    TaskState.INTERPRETED,
    TaskState.EXECUTING,
    TaskState.VALIDATING,
]


@pytest.mark.parametrize("state", sorted(_SILENT, key=lambda s: s.value))
def test_an_in_flight_state_says_nothing(state):
    assert message_for(_row(state)) is None


def test_the_notify_states_are_exactly_the_four_in_the_policy():
    assert NOTIFY_STATES == frozenset(
        {
            TaskState.NEEDS_CLARIFICATION,
            TaskState.AWAITING_APPROVAL,
            TaskState.DONE,
            TaskState.FAILED,
        }
    )


def test_every_notify_state_produces_a_message():
    """The set and the renderer must agree: a state in NOTIFY_STATES with no
    branch in message_for would silently notify nothing."""
    for state in NOTIFY_STATES:
        assert message_for(_row(state)), f"{state} is in NOTIFY_STATES but renders nothing"


def test_needs_clarification_asks_the_open_question():
    text = message_for(
        _row(TaskState.NEEDS_CLARIFICATION, open_question="Which output format?")
    )
    assert "Which output format?" in text


def test_needs_clarification_with_no_question_still_asks_something():
    """A task can reach this state with open_question NULL (the validate path
    clears it). Posting an empty string into a channel is worse than a generic
    prompt.

    text.strip() alone cannot fail here: the title makes the message non-empty
    even if the generic-prompt fallback is deleted outright (message_for would
    then render "“universe check” — ", which still .strip()s
    truthy). Assert the actual fallback content so deleting it is caught.
    """
    text = message_for(_row(TaskState.NEEDS_CLARIFICATION, open_question=None))
    assert text.strip()
    assert "What should I do?" in text


def test_awaiting_approval_names_the_mode_and_the_reason():
    text = message_for(
        _row(
            TaskState.AWAITING_APPROVAL,
            recommended_mode="suggest",
            autonomy_reason="low certainty → suggest",
        )
    )
    assert "suggest" in text
    assert "low certainty" in text


def test_awaiting_approval_reports_the_effective_mode_not_the_recommendation():
    """A human who pinned a mode must be told what is actually in force, not
    what the engine wanted — effective_mode is the field the dashboard shows
    and the driver acts on."""
    text = message_for(
        _row(
            TaskState.AWAITING_APPROVAL,
            recommended_mode="suggest",
            mode_override="copilot",
            autonomy_reason="r",
        )
    )
    assert "copilot" in text
    assert "suggest" not in text


def test_awaiting_approval_with_no_reason_still_reports_readiness():
    """autonomy_reason is nullable (row-level, not just this test's brief
    coverage — the brief's own suite never exercised this). No test previously
    checked it: message_for must not render an empty "()" tail when the reason
    is absent."""
    text = message_for(
        _row(TaskState.AWAITING_APPROVAL, recommended_mode="suggest", autonomy_reason=None)
    )
    assert text.strip()
    assert "None" not in text
    assert "()" not in text


def test_done_says_where_the_bundle_is():
    text = message_for(_row(TaskState.DONE, workspace_path="/work/task-workspaces/task-t1"))
    assert "/work/task-workspaces/task-t1" in text


def test_done_with_no_bundle_still_reports_completion():
    """text.strip() and "None" not in text alone cannot fail here: deleting the
    emptiness guard on the bundle tail renders "…is done. The bundle is at .",
    which is still non-empty and contains no literal "None". Assert the
    malformed tail itself is absent so removing the guard is caught."""
    text = message_for(_row(TaskState.DONE, workspace_path=None))
    assert text.strip()
    assert "None" not in text
    assert "at ." not in text


def test_failed_gives_the_reason():
    text = message_for(_row(TaskState.FAILED, failure_reason="the sandbox was unavailable"))
    assert "the sandbox was unavailable" in text


def test_failed_with_no_reason_still_reports_the_failure():
    """text.strip() and "None" not in text alone cannot fail here: deleting the
    "no reason was recorded" fallback renders "…failed: ", which is still
    non-empty (the title carries it) and contains no literal "None". Assert
    the fallback content itself so removing it is caught."""
    text = message_for(_row(TaskState.FAILED, failure_reason=None))
    assert text.strip()
    assert "None" not in text
    assert "no reason was recorded" in text


def test_the_null_notifier_does_nothing_and_says_so():
    notifier = NullNotifier()
    assert notifier.name == "null"
    assert notifier.notify(Destination(source="slack", conversation_id="c"), "hi") is None


def test_the_recording_notifier_keeps_what_it_was_asked_to_send():
    notifier = RecordingNotifier()
    dest = Destination(source="slack", conversation_id="c", external_id="e")
    notifier.notify(dest, "hello")
    assert notifier.sent == [(dest, "hello")]


def test_the_default_notifier_is_null():
    assert isinstance(current_notifier(), NullNotifier)


def test_the_holder_can_be_set_and_reset():
    recording = RecordingNotifier()
    previous = current_notifier()
    set_notifier(recording)
    try:
        assert current_notifier() is recording
    finally:
        set_notifier(previous)
    assert current_notifier() is previous
