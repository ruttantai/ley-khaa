import hashlib

from sqlalchemy import select
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

    def get_by_url(self, url_sha256: str) -> ImageExtractionRow | None:
        """The second key space (backlog #19, spec §3.5): a source that never
        produced image bytes has no `image_sha256` to look up by."""
        stmt = select(ImageExtractionRow).where(ImageExtractionRow.url_sha256 == url_sha256)
        return self.session.execute(stmt).scalar_one_or_none()

    def clear_unfetchable(self, url_sha256: str) -> bool:
        """Forget the negative row for a source that has since produced bytes.
        True if there was one.

        Only a `record_unfetchable` row can match: `url_sha256` is NULL on
        every ordinary image-bytes row (nullable, unique, no server_default —
        see ImageExtractionRow), so this can never delete a real extraction.

        Why it must be forgotten: the negative row suppresses the DUPLICATE
        dead letter for an identical, still-unfetchable source (item 19). A
        source that has just been fetched successfully is not still
        unfetchable, so a LATER failure of it is a new incident, not a repeat
        of the old one — and without this it would be silently swallowed for
        ever, in the one table whose job is that a failure is never silent.
        """
        row = self.get_by_url(url_sha256)
        if row is None:
            return False
        if row.content:
            # A source-keyed row carrying a real extraction is not a negative
            # row and must survive. Nothing writes one today (`_store` passes
            # no url_sha256), but backlog item 32's shape of the fix is
            # exactly that — a url -> digest index written on the SUCCESS
            # path — and a blind delete here would quietly gut it, on the
            # cache-hit path especially, where `_store` never runs to write
            # it back.
            return False
        self.session.delete(row)
        self.session.commit()
        return True

    def record_unfetchable(
        self, *, url_sha256: str, extraction: VisionExtraction
    ) -> ImageExtractionRow:
        """Record a source that raised before producing any bytes.

        There is no image to hash, so `image_sha256` is set to the same
        value as `url_sha256` — the source's own hash is this row's only
        available identity. `model` stays "": nothing ran, so nothing can be
        credited or blamed the way an actual extraction failure is.
        """
        return self.record(
            image_sha256=url_sha256,
            extraction=extraction,
            media_type="",
            byte_size=0,
            model="",
            url_sha256=url_sha256,
        )

    def record(
        self,
        *,
        image_sha256: str,
        extraction: VisionExtraction,
        media_type: str,
        byte_size: int,
        model: str,
        url_sha256: str | None = None,
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
        row.url_sha256 = url_sha256
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
            row.url_sha256 = url_sha256
            self.session.commit()
        self.session.refresh(row)
        return row
