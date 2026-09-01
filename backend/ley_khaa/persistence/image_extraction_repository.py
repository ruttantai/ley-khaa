import hashlib

from sqlalchemy.orm import Session

from ..vision.contract import VisionExtraction
from .orm import ImageExtractionRow


def sha256_of(image: bytes) -> str:
    """The identity of an image, and the cache key (spec §3.2)."""
    return hashlib.sha256(image).hexdigest()


class ImageExtractionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, image_sha256: str) -> ImageExtractionRow | None:
        return self.session.get(ImageExtractionRow, image_sha256)

    def record(
        self,
        *,
        image_sha256: str,
        extraction: VisionExtraction,
        media_type: str,
        byte_size: int,
        model: str,
    ) -> ImageExtractionRow:
        """Upsert, not insert.

        Two workers can legitimately reach the same unread image at once — one
        per project, by design since Phase 5 — and the loser of that race must
        not raise on the primary key. Last write wins: both wrote the same
        image, so neither result is more correct than the other.
        """
        row = self.session.get(ImageExtractionRow, image_sha256)
        if row is None:
            row = ImageExtractionRow(image_sha256=image_sha256)
            self.session.add(row)
        row.kind = extraction.kind
        row.content = extraction.content
        row.summary = extraction.summary
        row.media_type = media_type
        row.byte_size = byte_size
        row.model = model
        self.session.commit()
        self.session.refresh(row)
        return row
