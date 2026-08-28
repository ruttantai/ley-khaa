"""The registry ships near-empty but not empty (spec §5.6).

Two proven workflows, so a fresh clone can demonstrate the fast path before any
human has promoted anything. Installed at startup rather than by a migration:
migrations that import application code rot when the code moves, and app.py
already seeds the demo conversation this same way.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..fingerprint import normalize_operation
from ...persistence.workflow_repository import DuplicateWorkflow, WorkflowRepository
from . import set_difference, summary_stats

SEEDS: list[dict] = [set_difference.WORKFLOW, summary_stats.WORKFLOW]


def ensure_seed_workflows(session: Session) -> int:
    """Install any seed the registry does not already have. Returns how many.

    Idempotent: startup runs on every boot, and a human may have deleted a seed
    on purpose — so this fills gaps rather than resetting the registry. It never
    overwrites an existing row, because that row may be one a human edited.
    """
    repo = WorkflowRepository(session)
    installed = 0
    for seed in SEEDS:
        if repo.get(seed["name"]) is not None:
            continue
        try:
            repo.create(
                name=seed["name"],
                description=seed["description"],
                operation_aliases=[normalize_operation(a) for a in seed["operation_aliases"]],
                output_format=seed["output_format"],
                inputs=seed["inputs"],
                source=seed["source"],
                origin="seed",
            )
        except DuplicateWorkflow:
            # Another process seeded it between the check and the insert.
            continue
        installed += 1
    return installed
