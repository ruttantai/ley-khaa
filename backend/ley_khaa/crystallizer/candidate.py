from enum import Enum


class CandidateState(str, Enum):
    FORMING = "forming"
    CRYSTALLIZING = "crystallizing"
    READY = "ready"
    PROMOTED = "promoted"
    ABANDONED = "abandoned"


# PROMOTED and ABANDONED are terminal: a candidate in one of them is done being
# reported on and must never be resurrected.
TERMINAL_STATES: frozenset[str] = frozenset(
    {CandidateState.PROMOTED.value, CandidateState.ABANDONED.value}
)


# A candidate can slide backwards (a follow-up message reopens a settled request)
# to any other non-terminal state, but PROMOTED and ABANDONED are terminal. The
# three non-terminal states (FORMING, CRYSTALLIZING, READY) are therefore mutually
# reachable and each self-reachable; PROMOTED is reachable only from READY.
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
        CandidateState.FORMING,
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
