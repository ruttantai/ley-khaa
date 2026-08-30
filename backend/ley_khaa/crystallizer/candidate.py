from enum import Enum


class CandidateState(str, Enum):
    FORMING = "forming"
    CRYSTALLIZING = "crystallizing"
    READY = "ready"
    AWAITING_TRIAGE = "awaiting_triage"
    PROMOTED = "promoted"
    ABANDONED = "abandoned"


# "Already handled" for the crystallizer: a candidate in one of these is done
# being reported on and must never be resurrected. This is a wider set than the
# two states with no outgoing transitions, and deliberately so. AWAITING_TRIAGE
# still has somewhere to go (a human folds it or runs it separately), but stage B
# has nothing left to say about it: the request is captured and a decision is
# parked. Leaving it out left the parked candidate rendered as ACTIVE in the
# stage-B prompt, so the model re-reported the same candidate_key, and upsert hit
# ensure_transition(AWAITING_TRIAGE -> READY) — which _ALLOWED forbids. One
# parked amendment then made every later message in that conversation raise.
TERMINAL_STATES: frozenset[str] = frozenset(
    {
        CandidateState.PROMOTED.value,
        CandidateState.ABANDONED.value,
        CandidateState.AWAITING_TRIAGE.value,
    }
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
        CandidateState.AWAITING_TRIAGE,
    },
    # A candidate whose amendment proposal is waiting on a human. Reachable only
    # from READY, and it ends the same two ways any candidate ends: PROMOTED (the
    # human folded it or ran it separately) or ABANDONED.
    CandidateState.AWAITING_TRIAGE: {
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
