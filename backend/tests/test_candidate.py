import pytest

from ley_khaa.crystallizer.candidate import (
    CandidateState,
    InvalidCandidateTransition,
    can_transition,
    ensure_transition,
)


@pytest.mark.parametrize(
    "current,target,allowed",
    [
        (CandidateState.FORMING, CandidateState.CRYSTALLIZING, True),
        (CandidateState.FORMING, CandidateState.ABANDONED, True),
        (CandidateState.FORMING, CandidateState.READY, True),
        (CandidateState.CRYSTALLIZING, CandidateState.READY, True),
        (CandidateState.CRYSTALLIZING, CandidateState.FORMING, True),
        (CandidateState.READY, CandidateState.PROMOTED, True),
        (CandidateState.READY, CandidateState.CRYSTALLIZING, True),
        (CandidateState.READY, CandidateState.FORMING, True),
        (CandidateState.PROMOTED, CandidateState.READY, False),
        (CandidateState.PROMOTED, CandidateState.FORMING, False),
        (CandidateState.ABANDONED, CandidateState.FORMING, False),
    ],
)
def test_transition_rules(current, target, allowed):
    assert can_transition(current, target) is allowed


def test_ensure_transition_raises_on_illegal_move():
    with pytest.raises(InvalidCandidateTransition, match="promoted -> forming"):
        ensure_transition(CandidateState.PROMOTED, CandidateState.FORMING)


def test_ensure_transition_allows_legal_move():
    ensure_transition(CandidateState.FORMING, CandidateState.CRYSTALLIZING)


def test_same_state_is_allowed():
    # The LLM re-reports an unchanged candidate on most turns.
    assert can_transition(CandidateState.FORMING, CandidateState.FORMING) is True
