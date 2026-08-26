"""Turn `TaskSpec.inputs` names into actual bytes (spec §5.10, decision 2).

Attachments win over the catalog: a human who pasted data meant that data. A
name that matches neither is NOT guessed at — it is raised, and the caller
turns it into a clarification before a single token is spent on synthesis.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..domain.models import AttachmentKind
from ..interpreter.spec import TaskSpec
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import TaskRow
from . import catalog

_TOKEN = re.compile(r"[a-z0-9]+")

# Only these carry literal content the executor can compute on. An IMAGE
# attachment needs vision extraction, which is not built in this phase.
_TEXTUAL = {AttachmentKind.TABLE.value, AttachmentKind.TEXT.value}


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


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(value.lower()))


def _attachments_for(task: TaskRow, messages: MessageRepository) -> list[dict]:
    rows = messages.get_many(list(task.source_message_ids or []))
    found: list[dict] = []
    for row in rows:
        for attachment in row.attachments or []:
            if attachment.get("kind") in _TEXTUAL:
                found.append(attachment)
    return found


def _from_attachments(name: str, attachments: list[dict], used: set[int]) -> dict | None:
    wanted = _tokens(name)
    if not wanted:
        return None
    for index, attachment in enumerate(attachments):
        if index in used:
            continue
        # Strip the extension before matching: "holdings.csv" should answer to
        # the spoken input name "holdings".
        stem = _tokens(attachment.get("name", "").rsplit(".", 1)[0])
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
                    filename=_unique(hit.get("name") or f"{name}.csv", taken_filenames),
                    content=hit.get("content", ""),
                    source="attachment",
                )
            )
            continue

        dataset = catalog.resolve_name(name)
        if dataset is not None:
            # Verify that all tokens in the input are present in the dataset name.
            # catalog.resolve_name might match on dataset_tokens <= wanted, but we
            # only use matches where wanted <= dataset_tokens (i.e., "FactSet"
            # matches "factset_universe", but "holdings screenshot" must not match
            # "holdings" because "screenshot" is not a valid dataset token).
            wanted = _tokens(name)
            dataset_tokens = _tokens(dataset)
            if wanted <= dataset_tokens:
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
