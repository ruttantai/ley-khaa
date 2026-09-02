import hashlib

from sqlalchemy.exc import IntegrityError
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

        Check-then-insert, so it races, same shape as MessageRepository.add,
        CandidateRepository.upsert and MemoryRepository.record: the loser's
        INSERT collides on the primary key and raises IntegrityError, caught
        here and turned into an UPDATE on the row the winner already
        committed, rather than an exception out of a function whose whole
        contract is "always returns a row, never raises".
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
        try:
            self.session.commit()
        except IntegrityError:
            # Race: another worker inserted the same image_sha256 after our
            # get() missed it.
            self.session.rollback()
            row = self.session.get(ImageExtractionRow, image_sha256)
            if row is None:
                # The IntegrityError was not the duplicate key (should not
                # happen in normal operation).
                raise
            row.kind = extraction.kind
            row.content = extraction.content
            row.summary = extraction.summary
            row.media_type = media_type
            row.byte_size = byte_size
            row.model = model
            self.session.commit()
        self.session.refresh(row)
        return row
