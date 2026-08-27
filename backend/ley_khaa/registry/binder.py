"""Stage 3: bind a workflow's declared roles to this run's resolved inputs.

The rule that governs this whole module: **a bind failure is a cache miss, never
a guess.** Falling through to synthesis costs one Opus call. Binding the wrong
file to a role costs a confident, deterministic, wrong answer that the validator
may well accept — a spreadsheet full of the wrong numbers is still a spreadsheet
full of numbers.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..executor.resolver import ResolvedInput
from ..persistence.orm import WorkflowRow

logger = logging.getLogger(__name__)


def bind(workflow: WorkflowRow, resolved: list[ResolvedInput]) -> dict[str, str] | None:
    """role -> filename in inputs/, or None if this run cannot serve this workflow.

    Positional: roles are declared in the order the script expects them, and
    resolved inputs arrive in spec-input order. Anything else — a different
    count, a suffix the role does not accept, a malformed declaration — is a
    refusal.
    """
    roles = workflow.inputs or []
    if len(roles) != len(resolved) or not roles:
        return None

    binding: dict[str, str] = {}
    for declared, item in zip(roles, resolved):
        if not isinstance(declared, dict):
            return None
        role = declared.get("role")
        if not role or role in binding:
            # A duplicate role silently collapses in params.json, leaving the
            # frozen script reading a file it was never bound.
            return None
        suffixes = declared.get("suffixes") or []
        if suffixes and Path(item.filename).suffix.lower() not in {s.lower() for s in suffixes}:
            return None
        binding[role] = item.filename
    return binding
