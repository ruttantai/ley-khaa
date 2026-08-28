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

from ..executor.workspace import MANIFEST_NAME
from ..persistence.orm import WorkflowRow
from ..persistence.workflow_repository import WorkflowRepository
from .fingerprint import normalize_operation

# fullmatch, not match+$: in Python's re module `$` matches just before a
# trailing newline, so `.match()` with this same pattern let
# "universe_check\n" through. fullmatch requires the pattern to consume the
# entire string, newline included, with no such leniency.
#
# Exported as NAME_PATTERN too: api.schemas.PromoteIn uses the identical
# string as a pydantic Field(pattern=...), so a malformed name is a native 422
# from the schema before promote() is ever called (pydantic-core's regex
# engine anchors `$` at the true end of the string, unlike Python's re.match,
# so the same pattern text is safe to reuse there). promote()'s own check
# below stays as the guard for callers that construct a workflow directly,
# bypassing the API schema.
NAME_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"
NAME = re.compile(NAME_PATTERN)


class NotPromotable(Exception):
    """This bundle cannot become a workflow, and why."""


def _load_json(path: Path, what: str) -> dict:
    """Parse untrusted bundle JSON, turning malformed content into a
    NotPromotable (409) instead of letting it propagate into a 500.

    This bundle was written inside a sandbox by LLM-synthesized code, on the
    one route whose entire premise is that its input is untrusted. Malformed
    content here is a state conflict the caller can act on — not a crash.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise NotPromotable(f"{what} is not valid utf-8: {exc}") from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NotPromotable(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise NotPromotable(f"{what} must be a JSON object")
    return parsed


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
    it, including manifest.json itself: the workspace is written by untrusted
    generator code, and a symlink planted anywhere under the bundle — the
    manifest, an attempt script, params.json — would otherwise be promoted (or
    used to forge the verdict that authorizes promotion) and then run on every
    future match.
    """
    if not NAME.fullmatch(name or ""):
        raise NotPromotable(
            "a workflow name must be lowercase letters, digits and underscores, 3-64 characters"
        )

    manifest_path = contained(root, root / MANIFEST_NAME)
    if manifest_path is None or not manifest_path.is_file():
        raise NotPromotable("this bundle has no manifest, so it cannot be verified")
    manifest = _load_json(manifest_path, "manifest.json")

    if not (manifest.get("verdict") or {}).get("ok"):
        raise NotPromotable("only a run that passed validation can be promoted")

    winning = [a for a in manifest.get("attempts") or [] if isinstance(a, dict) and a.get("ok")]
    if not winning:
        raise NotPromotable("this bundle has no passing attempt to promote")
    attempt_number = winning[-1].get("attempt")
    # bool subclasses int in Python, so isinstance(True, int) is True — excluded
    # explicitly so a JSON `true`/`false` attempt number is rejected here rather
    # than passing this guard and failing later for an unrelated reason.
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool):
        raise NotPromotable("the winning attempt has no valid attempt number")
    attempt_path = contained(root, root / "generator" / f"attempt_{attempt_number}.py")
    if attempt_path is None or not attempt_path.is_file():
        raise NotPromotable("the winning attempt is not a readable file inside this bundle")

    params_path = contained(root, root / "inputs" / "params.json")
    if params_path is None or not params_path.is_file():
        raise NotPromotable("this bundle has no params.json, so its roles are unknown")
    params = _load_json(params_path, "inputs/params.json")
    binding = params.get("inputs") or {}
    if not isinstance(binding, dict) or not binding:
        raise NotPromotable("this bundle bound no inputs")

    # Read as bytes and decode explicitly rather than read_text(): read_text()
    # applies universal-newline translation, which would silently turn a CRLF
    # source into LF before it is hashed and stored — source_sha256 would then
    # attest bytes that never existed on disk.
    try:
        source = attempt_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotPromotable(f"the winning attempt is not valid utf-8: {exc}") from exc

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
        source=source,
        origin="promoted",
        promoted_from_task_id=task_id,
    )
