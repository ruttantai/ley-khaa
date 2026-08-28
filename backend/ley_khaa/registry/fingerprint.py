"""Stage 1: free, offline, deterministic matching (spec §3.3).

Everything here is a pure function over a spec and some rows. That matters more
than it looks: this is the half of the matcher that still works with no
ANTHROPIC_API_KEY, so the fast path never depends on a model being reachable.
"""
from __future__ import annotations

import re

from ..executor.formats import expected_suffixes
from ..interpreter.spec import TaskSpec
from ..persistence.orm import WorkflowRow

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_operation(operation: str) -> str:
    """Lowercase, non-alphanumerics to _, collapsed, stripped.

    "Set Difference", "set-difference" and "set__difference" are the same
    operation. The interpreter invents these strings freely (its prompt says so),
    so matching raw text would miss the cache on capitalization alone.
    """
    return _NON_ALNUM.sub("_", (operation or "").lower()).strip("_")


def formats_agree(a: str, b: str) -> bool:
    """True when both formats mean the same file suffix.

    Delegates to formats.py, which already knows excel == xlsx == spreadsheet.
    An unrecognised format yields (), and () never agrees with anything —
    otherwise every unknown word would match every other unknown word.
    """
    left, right = expected_suffixes(a), expected_suffixes(b)
    return bool(left) and left == right


def fingerprint_candidates(spec: TaskSpec, workflows: list[WorkflowRow]) -> list[WorkflowRow]:
    """Workflows this spec could be served by, on deterministic evidence alone.

    Conservative on purpose: a paraphrased operation is a miss here and stage 2's
    problem. Guessing at this layer is how a request ends up run by code that was
    proven for a different job — the one failure mode worse than a cache miss.
    """
    operation = normalize_operation(spec.operation)
    if not operation:
        return []
    return [
        workflow
        for workflow in workflows
        if not workflow.quarantined
        and operation in {normalize_operation(a) for a in workflow.operation_aliases or []}
        and formats_agree(spec.output_format, workflow.output_format)
        and len(workflow.inputs or []) == len(spec.inputs or [])
    ]
