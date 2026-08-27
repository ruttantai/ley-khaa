"""Turn `TaskSpec.inputs` names into actual bytes (spec §5.10, decision 2).

Attachments win over the catalog: a human who pasted data meant that data. A
name that matches neither is NOT guessed at — it is raised, and the caller
turns it into a clarification before a single token is spent on synthesis.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..domain.models import AttachmentKind
from ..interpreter.spec import TaskSpec
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import TaskRow
from . import catalog

# Only these carry literal content the executor can compute on. An IMAGE
# attachment needs vision extraction, which is not built in this phase.
_TEXTUAL = {AttachmentKind.TABLE.value, AttachmentKind.TEXT.value}


def _safe_basename(name: str) -> str:
    r"""Extract safe basename from a potentially-malicious path.

    Handles path traversal attempts (../../evil, /etc/passwd, \..\..\evil) by taking only
    the last component. Returns empty string if the result is empty, ".", "..", or contains
    path separators (/ or \), allowing the caller to fall back to a default.
    """
    if not name:
        return ""
    # Take the basename (last component after any / separator)
    basename = Path(name).name
    # Reject current/parent directory references and anything still containing separators
    # (handles \..\..\evil on POSIX where \ is literal, not a path separator)
    if basename in (".", "..") or "/" in basename or "\\" in basename:
        return ""
    return basename


@dataclass(frozen=True)
class ResolvedInput:
    name: str       # the spec input name this satisfies
    filename: str   # what it is called inside inputs/
    content: str
    source: str     # "attachment" | "catalog"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class UnresolvedInputs(Exception):
    """Raised with EVERY unresolved name, so the human is asked once."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


def _attachments_for(task: TaskRow, messages: MessageRepository) -> list[dict]:
    rows = messages.get_many(list(task.source_message_ids or []))
    found: list[dict] = []
    for row in rows:
        for attachment in row.attachments or []:
            if attachment.get("kind") in _TEXTUAL:
                found.append(attachment)
    return found


def _from_attachments(name: str, attachments: list[dict], used: set[int]) -> dict | None:
    wanted = catalog.tokens(name)
    if not wanted:
        return None
    for index, attachment in enumerate(attachments):
        if index in used:
            continue
        # Strip the extension before matching: "holdings.csv" should answer to
        # the spoken input name "holdings".
        stem = catalog.tokens(attachment.get("name", "").rsplit(".", 1)[0])
        # A name with no tokens at all ("", "---", ".csv") yields the empty set,
        # and the empty set is a subset of everything — so without this it would
        # match the FIRST spec input, beat the catalog, and have the task compute
        # on whatever bytes it carried while the manifest recorded a clean
        # `source: "attachment"`. AttachmentIn.name is public and unconstrained.
        if not stem:
            continue
        if wanted <= stem or stem <= wanted:
            used.add(index)
            return attachment
    return None


def _unique(filename: str, taken: set[str]) -> str:
    if filename not in taken:
        taken.add(filename)
        return filename
    stem, _, suffix = filename.rpartition(".")
    counter = 2
    while True:
        candidate = f"{stem}_{counter}.{suffix}" if stem else f"{filename}_{counter}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        counter += 1


def resolve_inputs(
    spec: TaskSpec, task: TaskRow, messages: MessageRepository
) -> list[ResolvedInput]:
    attachments = _attachments_for(task, messages)
    used_attachments: set[int] = set()
    taken_filenames: set[str] = set()
    resolved: list[ResolvedInput] = []
    missing: list[str] = []

    for name in spec.inputs:
        hit = _from_attachments(name, attachments, used_attachments)
        if hit is not None:
            resolved.append(
                ResolvedInput(
                    name=name,
                    filename=_unique(_safe_basename(hit.get("name", "")) or f"{name}.csv", taken_filenames),
                    content=hit.get("content", ""),
                    source="attachment",
                )
            )
            continue

        dataset = catalog.resolve_name(name)
        if dataset is not None:
            resolved.append(
                ResolvedInput(
                    name=name,
                    filename=_unique(f"{dataset}.csv", taken_filenames),
                    content=catalog.build_dataset(dataset),
                    source="catalog",
                )
            )
            continue

        missing.append(name)

    if missing:
        raise UnresolvedInputs(missing)
    return resolved
