"""Types the registry passes around (spec §5.6).

The ORM rows live in persistence/orm.py with every other row; these are the
pydantic and dataclass types that never touch the database.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from ..persistence.orm import WorkflowRow


class InputRole(BaseModel):
    """One declared input of a workflow. `role` is the key the frozen script
    reads out of params.json, so it is fixed at promotion and never renamed."""

    role: str
    suffixes: list[str]


class RegistryDecision(BaseModel):
    """What the stage-2 model call returns. `workflow` is a name or null —
    null is a first-class answer, not a failure."""

    workflow: str | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class Match:
    workflow: WorkflowRow
    # role -> the filename in inputs/ that this run bound to it.
    binding: dict[str, str]
    # "fingerprint" or "model". Recorded in the manifest, and the difference
    # decides whether an alias is learned on success.
    matched_by: str
