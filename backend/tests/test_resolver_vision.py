import pytest

from ley_khaa.executor.resolver import UnresolvedInputs, resolve_inputs
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.orm import ImageExtractionRow
from ley_khaa.persistence.repository import TaskRepository
from ley_khaa.domain.models import Attachment, Message


class _Extractor:
    def __init__(self, content="ticker,qty\nAAA,10", kind="table"):
        self.row = ImageExtractionRow(
            image_sha256="a" * 64, kind=kind, content=content,
            summary="a holdings table", media_type="image/png",
            byte_size=99, model="anthropic",
        )

    def extract(self, attachment):
        return self.row


def _task_with_image(session, *, name="holdings.png"):
    messages = MessageRepository(session)
    row = messages.add(
        Message(
            source="discord", client="g", conversation_id="c1", author="ana",
            text="compare these",
            attachments=[Attachment(kind="image", name=name, content="https://cdn.discordapp.com/a.png")],
        )
    )
    task = TaskRepository(session).create(
        project="default", title="compare", source_message_ids=[row.id]
    )
    return task, messages


def _spec(**over) -> TaskSpec:
    return TaskSpec(
        intent="compare", inputs=over.pop("inputs", ["holdings"]),
        operation="set_difference", output_format="csv",
        recipient="me", urgency="normal", missing_fields=[],
        certainty=0.9, **over,
    )


def test_an_image_with_an_extraction_becomes_an_input(session):
    task, messages = _task_with_image(session)

    resolved = resolve_inputs(_spec(), task, messages, extractor=_Extractor())

    assert len(resolved) == 1
    assert resolved[0].content == "ticker,qty\nAAA,10"
    assert resolved[0].source == "vision"


def test_the_provenance_reaches_the_resolved_input(session):
    """The spec's DoD: the manifest must attest the IMAGE's hash and the model
    that read it, neither of which ResolvedInput.sha256 carries."""
    task, messages = _task_with_image(session)

    resolved = resolve_inputs(_spec(), task, messages, extractor=_Extractor())

    assert resolved[0].extracted_from == "a" * 64
    assert resolved[0].extracted_by == "anthropic"
    assert resolved[0].sha256 != resolved[0].extracted_from, (
        "sha256 hashes the extracted CONTENT; extracted_from is the IMAGE"
    )


def test_the_checkpoint_is_named_for_the_image_and_its_kind(session):
    task, messages = _task_with_image(session, name="holdings.png")

    resolved = resolve_inputs(_spec(), task, messages, extractor=_Extractor())

    assert resolved[0].filename == "extracted_holdings.csv"


def test_a_text_extraction_lands_as_txt_not_csv(session):
    task, messages = _task_with_image(session, name="note.png")

    resolved = resolve_inputs(
        _spec(inputs=["note"]), task, messages,
        extractor=_Extractor(kind="text", content="just some words"),
    )

    assert resolved[0].filename == "extracted_note.txt"


def test_an_unread_image_is_NOT_bindable(session):
    """Empty content is the 'was not read' signal. Binding it would hand a
    script an empty file and let it compute a confident wrong answer."""
    # "holdings" (the default _spec() input / _task_with_image() name) is also
    # a real catalog dataset name (see catalog.DATASET_NAMES) AND would token
    # -match the synthetic "extracted_holdings.csv" attachment name, so it
    # cannot discriminate a broken guard from a working one: it succeeds via
    # the catalog either way. "notebook" collides with neither the catalog nor
    # the image's own filename, but DOES token-match the synthetic attachment
    # name the (would-be) extraction produces, so removing the "empty content"
    # guard actually flips this test from raising to succeeding.
    task, messages = _task_with_image(session, name="notebook.png")

    with pytest.raises(UnresolvedInputs):
        resolve_inputs(
            _spec(inputs=["notebook"]), task, messages,
            extractor=_Extractor(content=""),
        )


@pytest.mark.parametrize(
    "attachments",
    [
        pytest.param(
            [
                Attachment(kind="image", name="holdings.png", content="https://cdn/a.png"),
                Attachment(kind="table", name="holdings.csv", content="ticker\nREAL\n"),
            ],
            id="image_first",
        ),
        pytest.param(
            [
                Attachment(kind="table", name="holdings.csv", content="ticker\nREAL\n"),
                Attachment(kind="image", name="holdings.png", content="https://cdn/a.png"),
            ],
            id="csv_first",
        ),
    ],
)
def test_a_pasted_csv_beats_a_screenshot_of_the_same_data_either_order(session, attachments):
    """Coordinator ruling: resolver.py's own principle -- "a human who pasted
    data meant that data" -- ranks literal bytes above a model's READING of a
    picture. A user who drags both the screenshot AND the exact CSV into one
    message must always get the real bytes, never the OCR'd guess, and this
    must not depend on which one they happened to attach first."""
    messages = MessageRepository(session)
    row = messages.add(
        Message(
            source="discord", client="g", conversation_id="c1", author="ana",
            text="compare these", attachments=attachments,
        )
    )
    task = TaskRepository(session).create(
        project="default", title="compare", source_message_ids=[row.id]
    )

    resolved = resolve_inputs(_spec(), task, messages, extractor=_Extractor())

    assert len(resolved) == 1
    assert resolved[0].content == "ticker\nREAL\n"
    assert resolved[0].source == "attachment"
    assert resolved[0].extracted_from is None
    assert resolved[0].extracted_by is None


def test_a_whitespace_only_extraction_is_NOT_bindable(session):
    """Defence in depth: whitespace-only content is the same "was not read"
    case as empty content, and must not sneak past on a bare truthiness
    check ("\\n  \\n" is truthy)."""
    task, messages = _task_with_image(session, name="notebook.png")

    with pytest.raises(UnresolvedInputs):
        resolve_inputs(
            _spec(inputs=["notebook"]), task, messages,
            extractor=_Extractor(content="\n  \n"),
        )


def test_without_an_extractor_an_image_is_still_ignored(session):
    """The offline path is byte-identical to pre-phase-7 behaviour."""
    task, messages = _task_with_image(session)

    # Same catalog-collision reason as above: avoid "holdings" resolving via
    # the dataset catalog instead of exercising the (absent) image path.
    with pytest.raises(UnresolvedInputs):
        resolve_inputs(_spec(inputs=["holdings screenshot"]), task, messages, extractor=None)
