from enum import Enum


class TaskState(str, Enum):
    RECEIVED = "received"
    CLASSIFIED = "classified"
    INTERPRETED = "interpreted"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VALIDATING = "validating"
    NEEDS_CLARIFICATION = "needs_clarification"
    DONE = "done"
    FAILED = "failed"


# A task now pauses for a human, so the table gained three edges Phase 1
# declared but never wired: CLASSIFIED -> NEEDS_CLARIFICATION, so the
# interpreter can escalate a gap it finds; NEEDS_CLARIFICATION -> CLASSIFIED,
# so an answered clarification is re-interpreted over the enlarged message
# set; and AWAITING_APPROVAL -> INTERPRETED, so editing a parked spec
# re-enters scoring rather than re-running the interpreter. (INTERPRETED ->
# NEEDS_CLARIFICATION was declared alongside them but the interpreter only
# ever claims out of CLASSIFIED, so it was never reachable; removed.)
_ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.RECEIVED: {TaskState.CLASSIFIED, TaskState.FAILED},
    TaskState.CLASSIFIED: {
        TaskState.INTERPRETED,
        TaskState.NEEDS_CLARIFICATION,
        TaskState.FAILED,
    },
    TaskState.INTERPRETED: {
        TaskState.AWAITING_APPROVAL,
        TaskState.EXECUTING,
        TaskState.FAILED,
    },
    TaskState.AWAITING_APPROVAL: {
        TaskState.EXECUTING,
        TaskState.INTERPRETED,
        TaskState.NEEDS_CLARIFICATION,
        TaskState.FAILED,
    },
    TaskState.EXECUTING: {TaskState.VALIDATING, TaskState.FAILED},
    TaskState.VALIDATING: {TaskState.DONE, TaskState.NEEDS_CLARIFICATION, TaskState.FAILED},
    TaskState.NEEDS_CLARIFICATION: {
        TaskState.CLASSIFIED,
        TaskState.INTERPRETED,
        TaskState.AWAITING_APPROVAL,
        TaskState.EXECUTING,
        TaskState.FAILED,
    },
    TaskState.DONE: set(),
    TaskState.FAILED: set(),
}


# Where a task comes to rest on its own: finished, or a human owes it something.
# Lives here rather than in the driver because the dispatcher needs the same
# answer, and two copies of this set would drift.
WAITING: frozenset[TaskState] = frozenset(
    {
        TaskState.AWAITING_APPROVAL,
        TaskState.NEEDS_CLARIFICATION,
        TaskState.DONE,
        TaskState.FAILED,
    }
)

# Nothing moves a task out of these.
TERMINAL: frozenset[TaskState] = frozenset({TaskState.DONE, TaskState.FAILED})


class InvalidTransition(Exception):
    pass


def can_transition(current: TaskState, target: TaskState) -> bool:
    return target in _ALLOWED[current]


def ensure_transition(current: TaskState, target: TaskState) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"{current.value} -> {target.value} not allowed")
