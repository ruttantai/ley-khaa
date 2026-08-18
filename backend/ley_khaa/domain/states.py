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


_ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.RECEIVED: {TaskState.CLASSIFIED, TaskState.FAILED},
    TaskState.CLASSIFIED: {TaskState.INTERPRETED, TaskState.FAILED},
    TaskState.INTERPRETED: {TaskState.AWAITING_APPROVAL, TaskState.EXECUTING, TaskState.FAILED},
    TaskState.AWAITING_APPROVAL: {TaskState.EXECUTING, TaskState.NEEDS_CLARIFICATION, TaskState.FAILED},
    TaskState.EXECUTING: {TaskState.VALIDATING, TaskState.FAILED},
    TaskState.VALIDATING: {TaskState.DONE, TaskState.NEEDS_CLARIFICATION, TaskState.FAILED},
    TaskState.NEEDS_CLARIFICATION: {
        TaskState.INTERPRETED,
        TaskState.AWAITING_APPROVAL,
        TaskState.EXECUTING,
        TaskState.FAILED,
    },
    TaskState.DONE: set(),
    TaskState.FAILED: set(),
}


class InvalidTransition(Exception):
    pass


def can_transition(current: TaskState, target: TaskState) -> bool:
    return target in _ALLOWED[current]


def ensure_transition(current: TaskState, target: TaskState) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"{current.value} -> {target.value} not allowed")
