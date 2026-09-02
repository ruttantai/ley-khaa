"""Read an image once, and remember it (spec §3.2, §3.6).

One rule governs this module: an unreadable image NEVER blocks a task. Every
path either returns a real extraction or returns the unread record, and a
failed extraction is stored so a re-drive does not retry a failure made by the
SAME backend again. A stored "was not read" record from a DIFFERENT (or no)
backend is not frozen forever — see the cache-hit check in `extract()`.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Callable

from ..llm.router import Stage, model_for
from ..persistence.image_extraction_repository import ImageExtractionRepository, sha256_of
from ..persistence.orm import ImageExtractionRow
from .contract import VisionExtraction
from .fetcher import FetchRefused, ImageFetcher

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You read one image and return its content as structured data. If the image is a table, "
    "chart or spreadsheet, set kind='table' and put the data in content as CSV with a header "
    "row. Otherwise set kind='text' and put what the image says in content. summary is one "
    "short sentence describing what the image is."
)

# A browser paste arrives as `data:image/png;base64,....`; strip the prefix
# before decoding. `[^,]*` rather than a strict mime-type pattern: the prefix
# only needs to be recognised and discarded, never parsed for meaning.
_DATA_URI_PREFIX = re.compile(r"^data:[^,]*,")


def _sniff_media_type(image: bytes) -> str:
    """Identify the format from its magic bytes rather than assume PNG.

    A pasted JPEG sent to the API mislabelled image/png 400s — and, worse,
    freezes under that digest forever once stored as a failure. Falls back to
    image/png for anything unrecognised, matching the historical default.
    """
    if image.startswith(b"\x89PNG"):
        return "image/png"
    if image.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image.startswith(b"GIF8"):
        return "image/gif"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


class VisionExtractor:
    def __init__(
        self,
        *,
        llm,
        extractions: ImageExtractionRepository,
        fetcher: ImageFetcher | None = None,
        dead_letter: Callable[..., None] | None = None,
        enabled: bool = True,
    ) -> None:
        self.llm = llm
        self.extractions = extractions
        self.fetcher = fetcher
        self.dead_letter = dead_letter
        self.enabled = enabled

    def extract(self, attachment: dict) -> ImageExtractionRow:
        """One image -> its checkpoint row. Always returns a row."""
        name = str(attachment.get("name") or "an image")
        if attachment.get("kind") != "image":
            # The callers filter, but this is the last line before a CSV would
            # be handed to a vision model as if it were a picture.
            return self._unread(b"", name, reason="not an image attachment", media_type="")

        if not self.enabled:
            # Checked BEFORE _bytes_for, not after: _bytes_for is what
            # actually performs the outbound HTTP fetch (with the Slack bot
            # token attached, for a url_private), so a disabled extractor
            # must never reach it at all — "vision off" has to mean zero
            # fetches, not "fetch it anyway and only skip the model call".
            # This does mean a disabled call can no longer consult the cache
            # by the image's real digest (that digest comes from bytes this
            # path now never reads) — an acceptable trade for fetching
            # nothing, and the next ENABLED call still repopulates it. model
            # is still "": nothing ran on this path, so there is no backend
            # to credit, and an empty model is what lets the cache's
            # re-extract rule retry once vision comes back on.
            return self._unread(b"", name, reason="vision is disabled", media_type="")

        try:
            image, media_type = self._bytes_for(attachment)
        except (FetchRefused, ValueError, binascii.Error) as exc:
            reason = f"could not read {name}: {exc}"
            return self._record_unfetchable(attachment, name, reason)

        digest = sha256_of(image)
        cached = self.extractions.get(digest)
        if cached is not None:
            current_model = getattr(self.llm, "name", "")
            # A non-empty content is a real extraction: always short-circuit —
            # the one-model-call-per-image guarantee, untouched. An empty
            # content is a stored "was not read" record. That stays frozen
            # ONLY when the same backend that produced it is the one asking
            # again (a deterministic failure that will fail again). A stored
            # record from a DIFFERENT backend, or from no backend at all
            # (model == ""), is a configuration change, not a repeat of the
            # same failure — fall through and try again.
            if cached.content or (cached.model and cached.model == current_model):
                return cached

        try:
            extraction = self.llm.extract_image(
                choice=model_for(Stage.VISION_EXTRACTION),
                system=_SYSTEM,
                user=name,
                image=image,
                media_type=media_type,
                output_format=VisionExtraction,
            )
            if not isinstance(extraction, VisionExtraction):
                # A model can stop on max_tokens and hand back a None
                # parsed_output with no exception raised (AnthropicLLM.parse
                # has no guard for it) — that must degrade like any other
                # model failure, not raise an AttributeError out of
                # .extract() when _store below reads extraction.kind.
                raise TypeError(
                    f"vision model returned {type(extraction).__name__}, not VisionExtraction"
                )
        except Exception as exc:  # noqa: BLE001 - an unread image must not fail a task
            logger.exception("extracting %s failed", name)
            reason = f"could not read {name}: {type(exc).__name__}: {exc}"
            self._record_drop(reason, name)
            # STORED, not just returned: otherwise every re-drive retries a
            # failure that will fail again, and a task that repairs three
            # times pays for it three times. Credited to the backend that
            # actually attempted and failed, so the cache-hit check above can
            # tell a same-backend failure (stays frozen) from a switch to a
            # different backend (retried).
            return self._store(
                digest, image, media_type, self._unread_extraction(name, reason),
                model=getattr(self.llm, "name", ""),
            )

        return self._store(digest, image, media_type, extraction, model=getattr(self.llm, "name", ""))

    # -- helpers ---------------------------------------------------------

    def _bytes_for(self, attachment: dict) -> tuple[bytes, str]:
        content = str(attachment.get("content") or "")
        if content.startswith("http://") or content.startswith("https://"):
            if self.fetcher is None:
                raise FetchRefused("no image fetcher is configured")
            return self.fetcher.fetch(content)
        if not content:
            raise ValueError("the attachment carries no content")
        # A browser paste arrives as `data:image/*;base64,...`; strip that
        # prefix. Encoders like coreutils `base64` and base64.encodebytes wrap
        # output at 76 columns; strip all whitespace too, or a legitimate
        # payload fails the strict decode below. This phase exists so a user
        # can paste a screenshot — rejecting the browser paste form defeats
        # the point of it.
        payload = _DATA_URI_PREFIX.sub("", content, count=1)
        payload = "".join(payload.split())
        if not payload:
            # An all-whitespace payload, or a bare "data:image/png;base64,"
            # with nothing after the comma, strips down to "". b64decode("")
            # happily returns b"" rather than raising — which would otherwise
            # make a REAL model call billed against zero image bytes, and
            # store the result under the shared sha256(b"") digest. Treat
            # empty-after-stripping the same as empty-before-stripping.
            raise ValueError("the attachment carries no content")
        # b64decode (not standard_b64decode, which has no validate kwarg),
        # validate=True so a text blob raises here rather than decoding into
        # garbage bytes that get billed to a vision call.
        image = base64.b64decode(payload, validate=True)
        return image, _sniff_media_type(image)

    def _unread_extraction(self, name: str, reason: str) -> VisionExtraction:
        return VisionExtraction(kind="text", content="", summary=f"{name} was not read: {reason}")

    def _unread(self, image: bytes, name: str, *, reason: str, media_type: str) -> ImageExtractionRow:
        """An unread record for something that never got as far as a cache key.

        model is always "": nothing ran on this path, so there is no backend
        to credit or blame.
        """
        return ImageExtractionRow(
            image_sha256=sha256_of(image),
            kind="text",
            content="",
            summary=f"{name} was not read: {reason}",
            media_type=media_type,
            byte_size=len(image),
            model="",
        )

    def _record_unfetchable(self, attachment: dict, name: str, reason: str) -> ImageExtractionRow:
        """A source that raised before producing any bytes (backlog #19,
        spec §3.5): `_bytes_for` never returned an `image`, so there is
        nothing to hash for the image-bytes cache — key on the SOURCE
        string itself instead, so a second drive of the identical
        unfetchable URL (or undecodable payload) dead-letters once, not
        again on every drive.

        This is a SEPARATE, negative cache with its own semantics, not a
        second copy of the image-bytes cache's model-based retry rule:
        nothing here skips calling `_bytes_for` again next time, so a URL
        that becomes fetchable later (a transient host outage clears, a
        deleted Slack file is restored) is picked up for free on the very
        next drive — this only ever suppresses the DUPLICATE dead letter for
        an identical, still-unfetchable source.
        """
        content = str(attachment.get("content") or "")
        if not content:
            # No source string at all — nothing to key a second cache on.
            # Matches every other malformed-attachment path: unstored.
            self._record_drop(reason, name)
            return self._unread(b"", name, reason=reason, media_type="")

        url_key = sha256_of(content.encode())
        cached = self.extractions.get_by_url(url_key)
        if cached is not None:
            # Already recorded by an earlier drive of this identical
            # source: return it without dead-lettering again.
            return cached

        self._record_drop(reason, name)
        return self.extractions.record_unfetchable(
            url_sha256=url_key, extraction=self._unread_extraction(name, reason)
        )

    def _store(
        self, digest: str, image: bytes, media_type: str, extraction: VisionExtraction, *, model: str
    ) -> ImageExtractionRow:
        return self.extractions.record(
            image_sha256=digest,
            extraction=extraction,
            media_type=media_type,
            byte_size=len(image),
            # LLMClient.name when a client actually ran, "" when nothing did
            # (see call sites) — the manifest attests who ACTUALLY produced
            # this, and the cache-hit check above keys retry-worthiness off
            # exactly this column.
            model=model,
        )

    def _record_drop(self, reason: str, name: str) -> None:
        if self.dead_letter is None:
            return
        try:
            self.dead_letter(source="vision", kind="inbound", reason=reason, payload={"name": name})
        except Exception:  # noqa: BLE001
            logger.exception("could not dead-letter a failed extraction")
