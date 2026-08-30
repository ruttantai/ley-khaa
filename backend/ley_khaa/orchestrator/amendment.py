"""Is this new request a follow-up to something already running? (spec §5.9)

Two stages, like every other matcher here. Stage 1 is free and answers "does
this project have anything active at all?" — almost always no, so the common
path costs nothing. Stage 2 is one Haiku call, and its answer is untrusted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Iterable

from pydantic import BaseModel

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.orm import TaskRow
from ..persistence.repository import TaskRepository

logger = logging.getLogger(__name__)

# Same floor and same reasoning as the other two matchers.
AMENDMENT_CONFIDENCE_FLOOR = 0.8

SYSTEM = """You decide whether a new request modifies a task that is already underway.

You are given the new request and a list of active tasks with their ids and specifications.
Answer with the id of the task this request MODIFIES — adds to, narrows, corrects — or null
if it is a separate piece of work.

Say null unless you are confident. Folding a separate request into a running task loses the
separate request. A null costs only that a duplicate-looking task appears, which a human can
see and reject."""


class AmendmentChoice(BaseModel):
    """Stage 2's answer. `task_id` is an id from the list shown, or null."""

    task_id: str | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class AmendmentProposal:
    task_id: str
    confidence: float
    # The model's own sentence, shown to whoever decides.
    reason: str


class AmendmentDetector:
    def __init__(self, repo: TaskRepository, llm: LLMClient) -> None:
        self.repo = repo
        self.llm = llm

    def detect(
        self,
        *,
        project: str,
        title: str,
        summary: str,
        exclude_task_ids: Iterable[str] = (),
    ) -> AmendmentProposal | None:
        try:
            return self._detect(project, title, summary, set(exclude_task_ids))
        except Exception:
            # A detector that raises takes intake down. A detector that misses
            # costs one duplicate task a human can reject.
            logger.exception("amendment detection failed; treating this as a new request")
            return None

    def _detect(
        self, project: str, title: str, summary: str, exclude: set[str]
    ) -> AmendmentProposal | None:
        active = [t for t in self.repo.active_in_project(project) if t.id not in exclude]
        if not active:
            return None

        choice = self.llm.parse(
            choice=model_for(Stage.AMENDMENT_MATCH),
            system=SYSTEM,
            user=_prompt(title, summary, active),
            output_format=AmendmentChoice,
        )
        if not choice.task_id or choice.confidence < AMENDMENT_CONFIDENCE_FLOOR:
            return None

        # Untrusted output: the id must be one we actually showed it, which is
        # also what keeps another project's task from ever being named.
        target = next((t for t in active if t.id == choice.task_id), None)
        if target is None:
            logger.info("amendment detector named an unknown task %r", choice.task_id)
            return None
        return AmendmentProposal(
            task_id=target.id, confidence=choice.confidence, reason=choice.reason
        )


def _prompt(title: str, summary: str, active: list[TaskRow]) -> str:
    lines = ["## New request", f"title: {title}", f"summary: {summary}", "", "## Active tasks"]
    for task in active:
        spec = task.spec or {}
        lines.append(
            f"- [{task.id}] {task.title} (state: {task.state}; "
            f"intent: {spec.get('intent', 'not yet interpreted')})"
        )
    return "\n".join(lines)
