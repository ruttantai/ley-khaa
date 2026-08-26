import pytest

from ley_khaa.domain.models import Attachment, AttachmentKind, Message
from ley_khaa.executor.resolver import UnresolvedInputs, resolve_inputs
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _spec(inputs: list[str]) -> TaskSpec:
    return TaskSpec(
        intent="compare the two lists",
        inputs=inputs,
        operation="set_difference",
        output_format="xlsx",
        certainty=0.9,
    )


def _task_with(session, attachments: list[Attachment]):
    messages = MessageRepository(session)
    row = messages.add(
        Message(
            source="slack",
            client="demo",
            conversation_id="conv-1",
            author="boss",
            text="compare these",
            attachments=attachments,
        )
    )
    task = TaskRepository(session).create(
        project="demo", title="compare", source_message_ids=[row.id]
    )
    return task, messages


def test_an_attachment_satisfies_a_spec_input(session):
    task, messages = _task_with(
        session,
        [Attachment(kind=AttachmentKind.TABLE, name="holdings.csv", content="ticker\nAAA\n")],
    )
    resolved = resolve_inputs(_spec(["holdings"]), task, messages)
    assert [r.source for r in resolved] == ["attachment"]
    assert resolved[0].content == "ticker\nAAA\n"


def test_attachments_win_over_the_catalog(session):
    """A human who pasted data meant that data, not our synthetic stand-in."""
    task, messages = _task_with(
        session,
        [Attachment(kind=AttachmentKind.TABLE, name="holdings.csv", content="ticker\nAAA\n")],
    )
    resolved = resolve_inputs(_spec(["holdings"]), task, messages)
    assert resolved[0].source == "attachment"
    assert "SYN" not in resolved[0].content


def test_the_catalog_covers_a_name_no_attachment_provides(session):
    task, messages = _task_with(session, [])
    resolved = resolve_inputs(_spec(["Bloomberg universe", "FactSet"]), task, messages)
    assert [r.source for r in resolved] == ["catalog", "catalog"]
    assert [r.filename for r in resolved] == ["bloomberg_universe.csv", "factset_universe.csv"]


def test_an_unresolvable_input_raises_with_every_missing_name(session):
    task, messages = _task_with(session, [])
    with pytest.raises(UnresolvedInputs) as excinfo:
        resolve_inputs(_spec(["Bloomberg universe", "trade blotter", "universe"]), task, messages)
    # All of them, not just the first: asking the human one question about one
    # gap, then another question about the next, is the ping-pong the
    # clarification cap exists to prevent.
    assert excinfo.value.names == ["trade blotter", "universe"]


def test_image_attachments_are_not_treated_as_data(session):
    """Vision extraction is not built in this phase; an image is not a table."""
    task, messages = _task_with(
        session,
        [Attachment(kind=AttachmentKind.IMAGE, name="holdings.png", content="base64...")],
    )
    with pytest.raises(UnresolvedInputs):
        resolve_inputs(_spec(["holdings screenshot"]), task, messages)


def test_colliding_filenames_stay_distinct(session):
    task, messages = _task_with(
        session,
        [
            Attachment(kind=AttachmentKind.TABLE, name="data.csv", content="a\n1\n"),
            Attachment(kind=AttachmentKind.TABLE, name="data.csv", content="b\n2\n"),
        ],
    )
    resolved = resolve_inputs(_spec(["data", "data"]), task, messages)
    assert len({r.filename for r in resolved}) == 2


def test_sha256_is_content_addressed(session):
    task, messages = _task_with(session, [])
    resolved = resolve_inputs(_spec(["holdings"]), task, messages)
    assert len(resolved[0].sha256) == 64
