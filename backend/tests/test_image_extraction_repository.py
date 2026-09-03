import pytest

from ley_khaa.persistence.image_extraction_repository import (
    ImageExtractionRepository,
    sha256_of,
)
from ley_khaa.vision.contract import VisionExtraction

IMAGE = b"\x89PNG\r\n\x1a\n-not-really-a-png"


def _extraction(**over) -> VisionExtraction:
    return VisionExtraction(
        kind=over.pop("kind", "table"),
        content=over.pop("content", "a,b\n1,2"),
        summary=over.pop("summary", "a two-column table"),
    )


def test_a_hash_is_stable_and_content_addressed():
    assert sha256_of(IMAGE) == sha256_of(bytes(IMAGE))
    assert sha256_of(IMAGE) != sha256_of(IMAGE + b"x")


def test_an_unknown_image_has_no_row(session):
    assert ImageExtractionRepository(session).get(sha256_of(IMAGE)) is None


def test_a_recorded_extraction_comes_back(session):
    repo = ImageExtractionRepository(session)
    repo.record(
        image_sha256=sha256_of(IMAGE),
        extraction=_extraction(),
        media_type="image/png",
        byte_size=len(IMAGE),
        model="heuristic",
    )

    row = repo.get(sha256_of(IMAGE))
    assert row is not None
    assert row.kind == "table"
    assert row.content == "a,b\n1,2"
    assert row.summary == "a two-column table"
    assert row.media_type == "image/png"
    assert row.byte_size == len(IMAGE)
    assert row.model == "heuristic"


def test_recording_the_same_image_twice_updates_rather_than_raising(session):
    """Genuinely drives the IntegrityError recovery branch in record().

    Same technique as CandidateRepository's
    test_upsert_recovers_when_a_concurrent_insert_wins_the_race: a
    before_flush hook on the primary session opens a SECOND session on the
    same connection and commits a competing row under the SAME image_sha256
    right after this session's own get() has already missed it. The primary
    session's INSERT then collides for real, and the IntegrityError, the
    rollback and the recovery UPDATE are all real — not simulated.

    The previous shape of this test called repo.record() twice, sequentially,
    on ONE session: the second call's own session.get() would already see
    the first row and take the plain UPDATE branch, never touching the
    except IntegrityError branch at all. That passed whether or not the
    guard existed, and pinned nothing.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker

    other_session = sessionmaker(
        bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True
    )
    repo = ImageExtractionRepository(session)
    digest = sha256_of(IMAGE)
    fired: list[int] = []

    def commit_the_winner(sess, flush_context, instances):
        if fired:  # only race the first flush; the recovery path flushes again
            return
        fired.append(1)
        other = other_session()
        try:
            ImageExtractionRepository(other).record(
                image_sha256=digest,
                extraction=_extraction(content="x,y\n3,4", summary="different"),
                media_type="image/png", byte_size=1, model="anthropic",
            )
        finally:
            other.close()

    event.listen(session, "before_flush", commit_the_winner)
    try:
        result = repo.record(
            image_sha256=digest, extraction=_extraction(),
            media_type="image/png", byte_size=1, model="heuristic",
        )
    finally:
        event.remove(session, "before_flush", commit_the_winner)

    assert fired, "the race was never interposed"
    # Last write wins per record()'s own docstring: the row that wins the
    # INSERT race (the "other" session, above) is not necessarily the row
    # that survives — the recovering caller's own values are applied ON TOP
    # of it, so THIS call's fields (not the other session's) are final.
    row = repo.get(digest)
    assert row.content == "a,b\n1,2"
    assert row.model == "heuristic"
    assert result.content == "a,b\n1,2"
    assert result.model == "heuristic"


def test_an_unread_record_is_storable(session):
    """The degradation record (spec §3.6): empty content is how "was not read"
    is expressed, and it must round-trip like any other."""
    repo = ImageExtractionRepository(session)
    repo.record(
        image_sha256=sha256_of(IMAGE),
        extraction=_extraction(kind="text", content="", summary="chart.png was not read"),
        media_type="image/png", byte_size=0, model="heuristic",
    )

    row = repo.get(sha256_of(IMAGE))
    assert row.content == ""
    assert "was not read" in row.summary


def test_an_unfetchable_source_is_findable_by_its_url_hash(session):
    """The second key space (backlog #19): a source with no image bytes is
    looked up by url_sha256, not image_sha256."""
    repo = ImageExtractionRepository(session)
    url_key = sha256_of(b"https://evil.example.com/a.png")

    assert repo.get_by_url(url_key) is None

    row = repo.record_unfetchable(
        url_sha256=url_key,
        extraction=_extraction(kind="text", content="", summary="a.png was not read"),
    )

    assert row.content == ""
    assert row.url_sha256 == url_key
    assert row.image_sha256 == url_key, "no image bytes exist, so the source hash is the identity"
    assert repo.get_by_url(url_key) is row or repo.get_by_url(url_key).image_sha256 == row.image_sha256


def test_an_ordinary_image_row_has_no_url_sha256(session):
    """A normal image-bytes row must not accidentally acquire a url key."""
    repo = ImageExtractionRepository(session)
    row = repo.record(
        image_sha256=sha256_of(IMAGE),
        extraction=_extraction(),
        media_type="image/png",
        byte_size=len(IMAGE),
        model="heuristic",
    )

    assert row.url_sha256 is None


@pytest.mark.parametrize("kind", ["png", "csv", "", "TABLE"])
def test_a_kind_outside_the_closed_set_is_rejected(kind):
    """kind decides the checkpoint's file extension. A model returning
    something else must fail validation rather than produce a file whose
    extension lies about its contents."""
    with pytest.raises(Exception):
        VisionExtraction(kind=kind, content="", summary="")
