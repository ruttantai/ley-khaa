"""A proven bundle becomes a permanent capability (spec §5.6).

Promotion is a pure copy. Nothing here rewrites, reformats or re-synthesizes the
source: the code that becomes a workflow is byte-for-byte the code that passed
validation, which is the only reason source_sha256 and the bundle's audit trail
mean anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..executor.workspace import Workspace
from ..persistence.orm import WorkflowRow
from ..persistence.workflow_repository import WorkflowRepository
from .fingerprint import normalize_operation

NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class NotPromotable(Exception):
    """This bundle cannot become a workflow, and why."""


def promote(
    session: Session,
    *,
    task_id: str,
    name: str,
    description: str,
    root: Path,
    contained,
) -> WorkflowRow:
    """Freeze the winning attempt of `root` as a workflow named `name`.

    `contained` is api.app._contained, passed in rather than imported so this
    module does not depend on the API layer. Every path read below goes through
    it: the workspace is written by untrusted generator code, and a symlink
    planted in generator/ would otherwise be promoted into the registry and run
    on every future match.
    """
    if not NAME.match(name or ""):
        raise NotPromotable(
            "a workflow name must be lowercase letters, digits and underscores, 3-64 characters"
        )

    manifest = Workspace(root).read_manifest()
    if not (manifest.get("verdict") or {}).get("ok"):
        raise NotPromotable("only a run that passed validation can be promoted")

    winning = [a for a in manifest.get("attempts") or [] if a.get("ok")]
    if not winning:
        raise NotPromotable("this bundle has no passing attempt to promote")
    attempt_path = contained(root, root / "generator" / f"attempt_{winning[-1]['attempt']}.py")
    if attempt_path is None or not attempt_path.is_file():
        raise NotPromotable("the winning attempt is not a readable file inside this bundle")

    params_path = contained(root, root / "inputs" / "params.json")
    if params_path is None or not params_path.is_file():
        raise NotPromotable("this bundle has no params.json, so its roles are unknown")
    binding = json.loads(params_path.read_text(encoding="utf-8")).get("inputs") or {}
    if not binding:
        raise NotPromotable("this bundle bound no inputs")

    spec = manifest.get("spec") or {}
    return WorkflowRepository(session).create(
        name=name,
        description=description,
        operation_aliases=[normalize_operation(spec.get("operation", ""))],
        output_format=spec.get("output_format", ""),
        # Roles are the binding this run actually used, in its order — the names
        # the frozen script reads out of params.json.
        inputs=[
            {"role": role, "suffixes": [Path(filename).suffix.lower()]}
            for role, filename in binding.items()
        ],
        source=attempt_path.read_text(encoding="utf-8"),
        origin="promoted",
        promoted_from_task_id=task_id,
    )
