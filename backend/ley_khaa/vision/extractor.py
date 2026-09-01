"""Read an image once, and remember it (spec §3.2, §3.6).

One rule governs this module: an unreadable image NEVER blocks a task. Every
path either returns a real extraction or returns the unread record, and the
unread record is stored like any other so a re-drive does not retry a failure.
"""
from __future__ import annotations

import base64
import binascii
import logging
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

        try:
            image, media_type = self._bytes_for(attachment)
        except (FetchRefused, ValueError, binascii.Error) as exc:
            reason = f"could not read {name}: {exc}"
            self._record_drop(reason, name)
            return self._unread(b"", name, reason=reason, media_type="")

        digest = sha256_of(image)
        cached = self.extractions.get(digest)
        if cached is not None:
            return cached

        if not self.enabled:
            return self._store(digest, image, media_type, self._unread_extraction(name, "vision is disabled"))

        try:
            extraction = self.llm.extract_image(
                choice=model_for(Stage.VISION_EXTRACTION),
                system=_SYSTEM,
                user=name,
                image=image,
                media_type=media_type,
                output_format=VisionExtraction,
            )
        except Exception as exc:  # noqa: BLE001 - an unread image must not fail a task
            logger.exception("extracting %s failed", name)
            reason = f"could not read {name}: {type(exc).__name__}: {exc}"
            self._record_drop(reason, name)
            # STORED, not just returned: otherwise every re-drive retries a
            # failure that will fail again, and a task that repairs three times
            # pays for it three times.
            return self._store(digest, image, media_type, self._unread_extraction(name, reason))

        return self._store(digest, image, media_type, extraction)

    # -- helpers ---------------------------------------------------------

    def _bytes_for(self, attachment: dict) -> tuple[bytes, str]:
        content = str(attachment.get("content") or "")
        if content.startswith("http://") or content.startswith("https://"):
            if self.fetcher is None:
                raise FetchRefused("no image fetcher is configured")
            return self.fetcher.fetch(content)
        if not content:
            raise ValueError("the attachment carries no content")
        # b64decode (not standard_b64decode, which has no validate kwarg),
        # validate=True so a text blob raises here rather than decoding into
        # garbage bytes that get billed to a vision call.
        return base64.b64decode(content, validate=True), "image/png"

    def _unread_extraction(self, name: str, reason: str) -> VisionExtraction:
        return VisionExtraction(kind="text", content="", summary=f"{name} was not read: {reason}")

    def _unread(self, image: bytes, name: str, *, reason: str, media_type: str) -> ImageExtractionRow:
        """An unread record for something that never got as far as a cache key."""
        return ImageExtractionRow(
            image_sha256=sha256_of(image),
            kind="text",
            content="",
            summary=f"{name} was not read: {reason}",
            media_type=media_type,
            byte_size=len(image),
            model=getattr(self.llm, "name", ""),
        )

    def _store(
        self, digest: str, image: bytes, media_type: str, extraction: VisionExtraction
    ) -> ImageExtractionRow:
        return self.extractions.record(
            image_sha256=digest,
            extraction=extraction,
            media_type=media_type,
            byte_size=len(image),
            # LLMClient.name, so the manifest credits who ACTUALLY produced it.
            model=getattr(self.llm, "name", ""),
        )

    def _record_drop(self, reason: str, name: str) -> None:
        if self.dead_letter is None:
            return
        try:
            self.dead_letter(source="vision", kind="inbound", reason=reason, payload={"name": name})
        except Exception:  # noqa: BLE001
            logger.exception("could not dead-letter a failed extraction")
