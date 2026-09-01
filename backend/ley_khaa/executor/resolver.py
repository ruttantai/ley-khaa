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
# attachment carries a URL or base64, so it is computable only once vision has
# extracted it — which is what `extractor` in resolve_inputs() does, turning it
# into a synthetic textual attachment before matching (phase 7, spec §3.5).
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
    source: str     # "attachment" | "catalog" | "vision"
    # Set only for source == "vision". sha256 below hashes the extracted
    # CONTENT; these say which IMAGE it came from and who read it, which is
    # what makes a vision-sourced run auditable.
    extracted_from: str | None = None
    extracted_by: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class UnresolvedInputs(Exception):
    """Raised with EVERY unresolved name, so the human is asked once."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


def _stem_tokens(name: str) -> frozenset[str]:
    return catalog.tokens((name or "").rsplit(".", 1)[0])


def _collides(tokens: frozenset[str], others: list[frozenset[str]]) -> bool:
    if not tokens:
        return False
    return any(tokens <= other or other <= tokens for other in others)


def _attachments_for(task: TaskRow, messages: MessageRepository, extractor=None) -> list[dict]:
    rows = messages.get_many(list(task.source_message_ids or []))
    all_attachments = [a for row in rows for a in (row.attachments or [])]

    textual = [a for a in all_attachments if a.get("kind") in _TEXTUAL]
    found: list[dict] = list(textual)
    if extractor is None:
        return found

    # A pasted CSV/table beats a screenshot of the same data, regardless of
    # which attachment the human happened to paste first: the module
    # docstring's principle — "a human who pasted data meant that data" —
    # ranks literal bytes above a model's READING of a picture. Vision
    # entries are therefore computed in a SECOND pass, after every textual
    # stem is known, and one is dropped outright if its filename stem
    # collides with a textual attachment already bound, so attachment order
    # within the message can never flip which one wins.
    textual_stems = [_stem_tokens(a.get("name", "")) for a in textual]

    for attachment in all_attachments:
        if attachment.get("kind") != AttachmentKind.IMAGE.value:
            continue
        record = extractor.extract(attachment)
        # Empty (or whitespace-only) content is the "was not read" signal.
        # Binding it would hand a generated script an empty file and let it
        # compute a confident wrong answer, which is worse than asking the
        # human.
        if not record.content.strip():
            continue
        stem = (attachment.get("name") or "image").rsplit(".", 1)[0]
        if _collides(catalog.tokens(stem), textual_stems):
            continue
        suffix = "csv" if record.kind == "table" else "txt"
        found.append(
            {
                "kind": AttachmentKind.TEXT.value,
                "name": f"extracted_{stem}.{suffix}",
                "content": record.content,
                # Carried on the synthetic attachment so resolve_inputs can
                # stamp provenance without a second extractor call.
                "_vision": {"from": record.image_sha256, "by": record.model},
            }
        )
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
    spec: TaskSpec, task: TaskRow, messages: MessageRepository, extractor=None
) -> list[ResolvedInput]:
    attachments = _attachments_for(task, messages, extractor)
    used_attachments: set[int] = set()
    taken_filenames: set[str] = set()
    resolved: list[ResolvedInput] = []
    missing: list[str] = []

    for name in spec.inputs:
        hit = _from_attachments(name, attachments, used_attachments)
        if hit is not None:
            vision = hit.get("_vision")
            resolved.append(
                ResolvedInput(
                    name=name,
                    filename=_unique(
                        _safe_basename(hit.get("name", "")) or f"{name}.csv", taken_filenames
                    ),
                    content=hit.get("content", ""),
                    source="vision" if vision else "attachment",
                    extracted_from=(vision or {}).get("from"),
                    extracted_by=(vision or {}).get("by"),
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
