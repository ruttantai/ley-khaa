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
    """A re-extraction can happen legitimately — two workers racing on the same
    image. The second write must not blow up on the primary key."""
    repo = ImageExtractionRepository(session)
    repo.record(
        image_sha256=sha256_of(IMAGE), extraction=_extraction(),
        media_type="image/png", byte_size=1, model="heuristic",
    )
    repo.record(
        image_sha256=sha256_of(IMAGE),
        extraction=_extraction(content="x,y\n3,4", summary="different"),
        media_type="image/png", byte_size=1, model="anthropic",
    )

    row = repo.get(sha256_of(IMAGE))
    assert row.content == "x,y\n3,4"
    assert row.model == "anthropic"


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


@pytest.mark.parametrize("kind", ["png", "csv", "", "TABLE"])
def test_a_kind_outside_the_closed_set_is_rejected(kind):
    """kind decides the checkpoint's file extension. A model returning
    something else must fail validation rather than produce a file whose
    extension lies about its contents."""
    with pytest.raises(Exception):
        VisionExtraction(kind=kind, content="", summary="")
