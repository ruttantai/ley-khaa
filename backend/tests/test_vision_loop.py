"""Phase 7's claim, offline: no network, no key, real everything else."""
import base64
import json

from ley_khaa.domain.models import Attachment, Message
from ley_khaa.executor.runner import ExecutionRunner
from ley_khaa.executor.sandbox import SandboxResult
from ley_khaa.executor.workspace import MANIFEST_NAME
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.persistence.image_extraction_repository import (
    ImageExtractionRepository,
    sha256_of,
)
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository
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

    # The invariant this test is named for, actually pinned: this is the only
    # test in the file on the REAL API path (client -> build_vision_extractor
    # -> production wiring), so it must not just take the sibling unit test's
    # word for carried-not-read -- an operator's zero-account run goes through
    # exactly this path, and a silent regression here would bite for real.
    extraction = ImageExtractionRepository(session).get(sha256_of(PNG))
    assert extraction is not None
    assert extraction.content == "", "carried, not read: no content may be fabricated"
    assert extraction.model == "heuristic", "must credit the offline stand-in, never an Anthropic model"


def test_the_manifest_credits_who_actually_read_the_image(session):
    """Offline, the manifest must say heuristic — never claude-opus-5."""
    llm = _CountingLLM()  # no result -> the real offline stand-in
    extractor = VisionExtractor(
        llm=llm, extractions=ImageExtractionRepository(session), fetcher=None
    )

    row = extractor.extract({"kind": "image", "name": "holdings.png", "content": B64})

    assert row.model == "heuristic"
    assert row.content == ""


class _WritesDeliverable:
    """Fake sandbox: ignores the generated script, writes a passing CSV.

    Same shape as `FakeSandbox` in test_runner.py — runner tests are about the
    bundle, not about a real interpreter, and this test is no exception.
    """

    name = "fake"

    def run(self, *, script, workspace, timeout_s):
        (workspace / "deliverable" / "output.csv").write_text("ticker\nSYN0000\n")
        return SandboxResult(exit_code=0, stdout="ok", stderr="", duration_ms=1, timed_out=False)


def test_the_bundle_holds_the_extracted_text_the_manifest_points_at(tmp_path, session):
    """The bundle-file half of DoD claim 3, which nothing else proves: the
    manifest's provenance (extracted_from/extracted_by/sha256) is proven by
    test_runner.py, and ResolvedInput.filename is proven by
    test_resolver_vision.py, but neither opens the actual file a generated
    script would read. Drives a REAL ExecutionRunner to a REAL tmp_path
    workspace and reads inputs/extracted_holdings.csv off disk."""
    message = _image_message(session)
    created = TaskRepository(session).create(
        project="demo", title="compare", source_message_ids=[message.id]
    )
    extraction_llm = _CountingLLM(
        VisionExtraction(kind="table", content="ticker,qty\nAAA,10\n", summary="holdings")
    )
    extractor = VisionExtractor(
        llm=extraction_llm, extractions=ImageExtractionRepository(session), fetcher=None
    )
    runner = ExecutionRunner(
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        sandbox=_WritesDeliverable(),
        workspace_root=tmp_path,
        extractor=extractor,
    )
    spec = TaskSpec(
        intent="compare the holdings against the portfolio",
        inputs=["holdings"],
        operation="set_difference",
        output_format="csv",
        certainty=0.9,
    )

    outcome = runner.run(created, spec)

    assert outcome.verdict.ok, outcome.verdict.reason
    bundle_root = tmp_path / f"task-{created.id}"
    manifest = json.loads((bundle_root / MANIFEST_NAME).read_text())
    vision_entry = next(i for i in manifest["inputs"] if i["source"] == "vision")
    assert vision_entry["file"] == "extracted_holdings.csv"

    # The manifest half and the bundle-file half, tied together: the manifest
    # names the file, and that exact name is the file that actually exists.
    bundle_file = bundle_root / "inputs" / vision_entry["file"]
    assert bundle_file.is_file(), "inputs/extracted_holdings.csv must exist in the written bundle"
    content = bundle_file.read_text(encoding="utf-8")
    assert content == "ticker,qty\nAAA,10\n", "must hold the extracted TEXT, not be empty or wrong"
    assert PNG not in bundle_file.read_bytes(), "must hold extracted text, not the image bytes"


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
