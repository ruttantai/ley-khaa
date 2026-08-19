from enum import Enum


class CandidateState(str, Enum):
    FORMING = "forming"
    CRYSTALLIZING = "crystallizing"
    READY = "ready"
    PROMOTED = "promoted"
    ABANDONED = "abandoned"


# A candidate can slide backwards (a follow-up message reopens a settled request)
# but PROMOTED and ABANDONED are terminal.
_ALLOWED: dict[CandidateState, set[CandidateState]] = {
    CandidateState.FORMING: {
        CandidateState.FORMING,
        CandidateState.CRYSTALLIZING,
        CandidateState.READY,
        CandidateState.ABANDONED,
    },
    CandidateState.CRYSTALLIZING: {
        CandidateState.CRYSTALLIZING,
        CandidateState.FORMING,
        CandidateState.READY,
        CandidateState.ABANDONED,
    },
    CandidateState.READY: {
        CandidateState.READY,
        CandidateState.CRYSTALLIZING,
        CandidateState.PROMOTED,
        CandidateState.ABANDONED,
    },
    CandidateState.PROMOTED: set(),
    CandidateState.ABANDONED: set(),
}


class InvalidCandidateTransition(Exception):
    pass


def can_transition(current: CandidateState, target: CandidateState) -> bool:
    return target in _ALLOWED[current]


def ensure_transition(current: CandidateState, target: CandidateState) -> None:
    if not can_transition(current, target):
        raise InvalidCandidateTransition(f"{current.value} -> {target.value} not allowed")
