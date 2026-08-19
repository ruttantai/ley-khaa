import pytest

from ley_khaa.intake.gateway import IntakeGateway
from ley_khaa.persistence.message_repository import MessageRepository


@pytest.fixture
def gateway(session):
    return IntakeGateway(MessageRepository(session))


def test_accept_normalizes_minimal_payload(gateway):
    row = gateway.accept({"text": "do the universe check"})
    assert row.text == "do the universe check"
    assert row.source == "simulator"
    assert row.client == "demo"
    assert row.conversation_id == "conv-1"
    assert row.author == "user"


def test_thread_id_is_accepted_as_conversation_id(gateway):
    row = gateway.accept({"text": "hi", "thread_id": "slack-thread-9"})
    assert row.conversation_id == "slack-thread-9"


def test_explicit_conversation_id_wins_over_thread_id(gateway):
    row = gateway.accept({"text": "hi", "thread_id": "t1", "conversation_id": "c9"})
    assert row.conversation_id == "c9"


def test_attachments_are_coerced(gateway):
    row = gateway.accept(
        {
            "text": "here it is",
            "attachments": [{"kind": "table", "name": "u.csv", "content": "a,b"}],
        }
    )
    assert row.attachments[0]["kind"] == "table"


def test_image_attachment_is_stored_not_interpreted(gateway):
    row = gateway.accept(
        {"text": "see chart", "attachments": [{"kind": "image", "name": "c.png", "content": "/tmp/c.png"}]}
    )
    assert row.attachments[0]["kind"] == "image"
    assert row.attachments[0]["content"] == "/tmp/c.png"


def test_accept_is_idempotent_per_external_id(gateway):
    first = gateway.accept({"text": "same", "external_id": "slack-1"})
    second = gateway.accept({"text": "same", "external_id": "slack-1"})
    assert first.id == second.id


def test_missing_text_raises(gateway):
    with pytest.raises(ValueError, match="text is required"):
        gateway.accept({"author": "boss"})
