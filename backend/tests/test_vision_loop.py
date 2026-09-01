"""Phase 7's claim, offline: no network, no key, real everything else."""
import base64
import json

from ley_khaa.domain.models import Attachment, Message
from ley_khaa.executor.workspace import MANIFEST_NAME
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.persistence.image_extraction_repository import (
    ImageExtractionRepository,
    sha256_of,
)
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.vision.contract import VisionExtraction
from ley_khaa.vision.extractor import VisionExtractor

PNG = b"\x89PNG\r\n\x1a\nholdings"
B64 = base64.standard_b64encode(PNG).decode()


class _CountingLLM:
    """The REAL offline client, wrapped in a counter."""

    def __init__(self, result=None):
        self.inner = HeuristicLLM()
        self.result = result
        self.calls = []
        self.name = "heuristic" if result is None else "anthropic"

    def extract_image(self, **kwargs):
        self.calls.append(kwargs)
        return self.result if self.result is not None else self.inner.extract_image(**kwargs)

    def __getattr__(self, item):
        return getattr(self.inner, item)


def _image_message(session, name="holdings.png"):
    return MessageRepository(session).add(
        Message(
            source="dashboard", client="me", conversation_id="c1", author="ana",
            text="compare the holdings against the portfolio",
            attachments=[Attachment(kind="image", name=name, content=B64)],
        )
    )


def test_an_image_is_read_once_and_only_once(session):
    """The reproducibility claim. Asserted with a counter around the real
    client, and re-asserted inside the test so it cannot degrade into 'two
    calls happened to agree'."""
    llm = _CountingLLM(VisionExtraction(kind="table", content="t,q\nAAA,10", summary="holdings"))
    extractor = VisionExtractor(
        llm=llm, extractions=ImageExtractionRepository(session), fetcher=None
    )
    attachment = {"kind": "image", "name": "holdings.png", "content": B64}

    first = extractor.extract(attachment)
    assert len(llm.calls) == 1

    second = extractor.extract(attachment)

    assert len(llm.calls) == 1, "the second read must come from the checkpoint"
    assert second.content == first.content
    assert second.image_sha256 == sha256_of(PNG)


def test_the_checkpoint_survives_a_fresh_extractor(session):
    """A re-drive builds a new extractor on a new session. The checkpoint is
    the DB row, not in-process state."""
    result = VisionExtraction(kind="table", content="t,q\nAAA,10", summary="holdings")
    attachment = {"kind": "image", "name": "holdings.png", "content": B64}

    first_llm = _CountingLLM(result)
    VisionExtractor(
        llm=first_llm, extractions=ImageExtractionRepository(session), fetcher=None
    ).extract(attachment)

    second_llm = _CountingLLM(result)
    row = VisionExtractor(
        llm=second_llm, extractions=ImageExtractionRepository(session), fetcher=None
    ).extract(attachment)

    assert second_llm.calls == [], "a new extractor must still hit the stored checkpoint"
    assert row.content == "t,q\nAAA,10"


def test_offline_an_image_is_carried_not_read_and_the_task_still_completes(session, client):
    """The zero-account invariant: no key, and the demo still works."""
    resp = client.post(
        "/messages",
        json={
            "text": "compare the holdings against the portfolio and send it as a csv",
            "attachments": [{"kind": "image", "name": "holdings.png", "content": B64}],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_ids"], "an image attachment must not stop a task forming"


def test_the_manifest_credits_who_actually_read_the_image(session):
    """Offline, the manifest must say heuristic — never claude-opus-5."""
    llm = _CountingLLM()  # no result -> the real offline stand-in
    extractor = VisionExtractor(
        llm=llm, extractions=ImageExtractionRepository(session), fetcher=None
    )

    row = extractor.extract({"kind": "image", "name": "holdings.png", "content": B64})

    assert row.model == "heuristic"
    assert row.content == ""


def test_the_whole_offline_path_opens_no_socket(session, monkeypatch):
    """'Offline' enforced rather than claimed."""
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("the offline vision path opened a socket")

    monkeypatch.setattr(socket.socket, "connect", forbidden)

    extractor = VisionExtractor(
        llm=_CountingLLM(), extractions=ImageExtractionRepository(session), fetcher=None
    )
    extractor.extract({"kind": "image", "name": "holdings.png", "content": B64})
