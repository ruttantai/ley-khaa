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


def test_offline_extraction_credits_the_heuristic_stand_in(session):
    """Offline, the extraction row must say heuristic -- never claude-opus-5.
    (Renamed from test_the_manifest_credits_who_actually_read_the_image: this
    is the unit-level check on the extraction row. The manifest-level claim
    that name promised is now pinned by
    test_an_unread_image_reaches_the_manifest_as_a_carried_not_read_image
    below, which actually opens a manifest.json.)"""
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


def test_an_unread_image_reaches_the_manifest_as_a_carried_not_read_image(tmp_path, session):
    """Review B2: on the default (no-account) path an unread image never
    becomes a ResolvedInput, so without this fix the manifest would say
    NOTHING about it -- a confident audit trail for a run whose screenshot
    was ignored. Drives a REAL ExecutionRunner to a REAL tmp_path workspace
    and opens manifest.json, the way the read-successfully test above does.
    This is the fix for the test that used to be named for the manifest
    (test_the_manifest_credits_who_actually_read_the_image) but never opened
    one -- see test_offline_extraction_credits_the_heuristic_stand_in for the
    unit-level half of that claim."""
    message = _image_message(session)  # attaches holdings.png
    created = TaskRepository(session).create(
        project="demo", title="compare", source_message_ids=[message.id]
    )
    extraction_llm = _CountingLLM()  # no result -> the real offline stand-in, unread
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
    # "portfolio" is a real catalog dataset whose name does not collide with
    # the image's own filename stem ("holdings"), so this spec resolves
    # normally -- the B1 refusal path is exercised elsewhere
    # (test_an_unread_image_is_NOT_bindable); this test is only about
    # whether the manifest RECORDS the unread image once the round completes.
    spec = TaskSpec(
        intent="compare the holdings against the portfolio",
        inputs=["portfolio"],
        operation="set_difference",
        output_format="csv",
        certainty=0.9,
    )

    outcome = runner.run(created, spec)

    assert outcome.verdict.ok, outcome.verdict.reason
    bundle_root = tmp_path / f"task-{created.id}"
    manifest = json.loads((bundle_root / MANIFEST_NAME).read_text())

    assert manifest["images"], "an unread image must not vanish from the manifest"
    image_entry = manifest["images"][0]
    assert image_entry["name"] == "holdings.png"
    assert image_entry["sha256"] == sha256_of(PNG)
    assert image_entry["model"] == "heuristic", "must credit the offline stand-in, never an Anthropic model"
    assert "not read" in image_entry["summary"]


def test_an_unread_image_from_a_fetch_failure_does_not_attest_a_fake_hash(tmp_path, session):
    """Whole-branch re-review, item 2: on the fetch-failure path (an
    unfetchable or expired URL -- the same shape as an expired Discord CDN
    link, or LEY_KHAA_VISION=off) no image bytes are ever read, so
    record.image_sha256 is sha256(b"") -- a constant every such image
    shares, not a real identity for THIS image. The manifest must not
    attest that as though it meant something. This is the production-likely
    path the manifest test above does NOT cover: that one decodes real
    base64 bytes locally and only the MODEL declines, so its hash is
    genuine."""
    row = MessageRepository(session).add(
        Message(
            source="dashboard", client="me", conversation_id="c1", author="ana",
            text="compare the holdings against the portfolio",
            # A url with no fetcher configured is refused before any bytes
            # are ever read -- FetchRefused out of _bytes_for, same as an
            # expired CDN link.
            attachments=[
                Attachment(kind="image", name="holdings.png", content="https://cdn.discordapp.com/a.png")
            ],
        )
    )
    created = TaskRepository(session).create(
        project="demo", title="compare", source_message_ids=[row.id]
    )
    extractor = VisionExtractor(
        llm=_CountingLLM(), extractions=ImageExtractionRepository(session), fetcher=None,
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
        inputs=["portfolio"],
        operation="set_difference",
        output_format="csv",
        certainty=0.9,
    )

    outcome = runner.run(created, spec)

    assert outcome.verdict.ok, outcome.verdict.reason
    bundle_root = tmp_path / f"task-{created.id}"
    manifest = json.loads((bundle_root / MANIFEST_NAME).read_text())

    assert manifest["images"], "an unread image must not vanish from the manifest"
    image_entry = manifest["images"][0]
    assert image_entry["name"] == "holdings.png"
    assert image_entry["sha256"] is None, (
        'no bytes were ever read on this path -- attesting sha256(b"") would '
        "look like a real identity every such unread image shares"
    )
    assert "not read" in image_entry["summary"]


def test_the_unresolved_inputs_manifest_still_names_the_unread_image(tmp_path, session):
    """Whole-branch re-review, item 4: the round that raises UnresolvedInputs
    is exactly where an unread image is most likely to BE the reason nothing
    resolved (review B1) -- and it is the one _write_manifest call this
    branch cannot fall through to by accident, since `resolved` is [] there
    and every OTHER field comes from the verdict this except block builds by
    hand. Nothing else in the suite pins unread_images=exc.unread_images at
    runner.py's UnresolvedInputs handler specifically; setting it to None
    there leaves every other test green."""
    message = _image_message(session)  # attaches holdings.png, unread offline
    created = TaskRepository(session).create(
        project="demo", title="compare", source_message_ids=[message.id]
    )
    extractor = VisionExtractor(
        llm=_CountingLLM(), extractions=ImageExtractionRepository(session), fetcher=None,
    )
    runner = ExecutionRunner(
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        sandbox=_WritesDeliverable(),
        workspace_root=tmp_path,
        extractor=extractor,
    )
    # "holdings" collides with the unread image's own filename stem, so B1's
    # guard blocks the catalog and resolve_inputs raises UnresolvedInputs
    # rather than resolving anything at all.
    spec = TaskSpec(
        intent="compare the holdings against the portfolio",
        inputs=["holdings"],
        operation="set_difference",
        output_format="csv",
        certainty=0.9,
    )

    outcome = runner.run(created, spec)

    assert not outcome.verdict.ok
    assert "holdings" in outcome.verdict.reason
    bundle_root = tmp_path / f"task-{created.id}"
    manifest = json.loads((bundle_root / MANIFEST_NAME).read_text())

    assert manifest["inputs"] == [], "nothing resolved on this round"
    assert manifest["images"], (
        "the round B1 and B2 exist to make visible must not go back to silence"
    )
    assert manifest["images"][0]["name"] == "holdings.png"


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
