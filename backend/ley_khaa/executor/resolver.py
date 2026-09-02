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

    def __init__(self, names: list[str], unread_images: list["UnreadImage"] | None = None) -> None:
        super().__init__(", ".join(names))
        self.names = names
        # Carried on the exception, not just returned on the happy path: the
        # manifest must record an unread image (review B2) even on the round
        # that never produces a single ResolvedInput.
        self.unread_images = unread_images or []


@dataclass(frozen=True)
class UnreadImage:
    """An image that was supplied but could not be read (spec §3.6, §7).

    Kept out of `ResolvedInput` on purpose: an unread image carries no bytes
    a script can compute on, so folding it in would let it slip into
    inputs/ as an empty file -- the exact thing the B1 fix exists to stop.
    This is what lets the manifest say plainly "an image was supplied and
    not read" instead of staying silent about it.
    """

    name: str
    image_sha256: str
    model: str
    summary: str
    # 0 for every path that never got as far as reading actual bytes (an
    # unfetchable/expired URL, a disabled extractor, a non-image attachment):
    # image_sha256 on those is sha256(b""), a constant every such image
    # shares, not a real identity. The manifest reads this to decide whether
    # attesting that hash would mean anything (review B2's follow-up).
    byte_size: int = 0


def _stem_tokens(name: str) -> frozenset[str]:
    return catalog.tokens((name or "").rsplit(".", 1)[0])


def _collides(tokens: frozenset[str], others: list[frozenset[str]]) -> bool:
    if not tokens:
        return False
    # An empty `other` ("---", ".png", a name with no alphanumerics at all)
    # is a subset of everything, same failure mode _from_attachments already
    # guards against with its own `if not stem: continue` -- without this, one
    # oddly-named unread image would collide with (and so block) EVERY spec
    # input, and with LEY_KHAA_VISION=off every image is unread.
    return any(other and (tokens <= other or other <= tokens) for other in others)


def _attachments_for(
    task: TaskRow, messages: MessageRepository, extractor=None
) -> tuple[list[dict], list[UnreadImage]]:
    rows = messages.get_many(list(task.source_message_ids or []))
    all_attachments = [a for row in rows for a in (row.attachments or [])]

    textual = [a for a in all_attachments if a.get("kind") in _TEXTUAL]
    found: list[dict] = list(textual)
    unread_images: list[UnreadImage] = []
    if extractor is None:
        return found, unread_images

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
        name = attachment.get("name") or "image"
        # Empty (or whitespace-only) content is the "was not read" signal.
        # Binding it would hand a generated script an empty file and let it
        # compute a confident wrong answer, which is worse than asking the
        # human. It must also not vanish silently: recorded here so
        # resolve_inputs can both refuse to let the name fall through to the
        # catalog (review B1) and let the manifest say an image was supplied
        # and not read (review B2).
        if not record.content.strip():
            unread_images.append(
                UnreadImage(
                    name=name,
                    image_sha256=record.image_sha256,
                    model=record.model,
                    summary=record.summary,
                    byte_size=record.byte_size,
                )
            )
            continue
        stem = name.rsplit(".", 1)[0]
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
    return found, unread_images


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
) -> tuple[list[ResolvedInput], list[UnreadImage]]:
    attachments, unread_images = _attachments_for(task, messages, extractor)
    # Same token-subset test _from_attachments already uses to decide a
    # collision, reused here rather than invented fresh: an unread image's
    # filename stem "claims" the spec input names it could plausibly have
    # answered, and that claim must block the catalog exactly like a bound
    # attachment would -- see review B1.
    unread_stems = [_stem_tokens(img.name) for img in unread_images]
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

        # An unread image claiming this name must NOT fall through to the
        # catalog: a human who pasted a screenshot meant that screenshot, and
        # answering with the synthetic demo dataset instead -- silently, with
        # a manifest that attests a clean `source: "catalog"` -- is the
        # defect review B1 exists to close. Ask a human instead.
        if _collides(catalog.tokens(name), unread_stems):
            missing.append(name)
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
        raise UnresolvedInputs(missing, unread_images)
    return resolved, unread_images
