from dataclasses import dataclass

from pydantic import BaseModel


class ProjectChoice(BaseModel):
    """Stage 2's answer. `project` is a project name or null — null is a
    first-class answer meaning "route this to the default project"."""

    project: str | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class RoutingDecision:
    project: str
    # "binding" (free), "model" (stage 2 won), or "default" (miss//fallback).
    # Recorded so a routing decision can be audited after the fact.
    stage: str
    confidence: float
    reason: str
