from datetime import datetime, timedelta, timezone

from ley_khaa.domain.models import Attachment, AttachmentKind, Message
from ley_khaa.persistence.message_repository import MessageRepository


def _msg(text="hello", conv="c1", external_id=None, ts=None):
    return Message(
        source="simulator",
        client="demo",
        conversation_id=conv,
        author="boss",
        text=text,
        external_id=external_id,
        timestamp=ts or datetime.now(timezone.utc),
    )


def test_add_persists_message(session):
    repo = MessageRepository(session)
    row = repo.add(_msg("first"))
    assert row.text == "first"
    assert repo.list_for_conversation("c1")[0].id == row.id


def test_add_is_idempotent_per_external_id(session):
    repo = MessageRepository(session)
    first = repo.add(_msg("dup", external_id="slack-123"))
    second = repo.add(_msg("dup", external_id="slack-123"))
    assert first.id == second.id
    assert len(repo.list_for_conversation("c1")) == 1


def test_messages_without_external_id_are_not_deduped(session):
    repo = MessageRepository(session)
    repo.add(_msg("same text"))
    repo.add(_msg("same text"))
    assert len(repo.list_for_conversation("c1")) == 2


def test_list_is_ordered_by_timestamp(session):
    repo = MessageRepository(session)
    base = datetime.now(timezone.utc)
    repo.add(_msg("second", ts=base + timedelta(seconds=10)))
    repo.add(_msg("first", ts=base))
    assert [m.text for m in repo.list_for_conversation("c1")] == ["first", "second"]


def test_window_returns_most_recent_n_in_order(session):
    repo = MessageRepository(session)
    base = datetime.now(timezone.utc)
    for i in range(5):
        repo.add(_msg(f"m{i}", ts=base + timedelta(seconds=i)))
    assert [m.text for m in repo.window("c1", limit=3)] == ["m2", "m3", "m4"]


def test_conversations_are_isolated(session):
    repo = MessageRepository(session)
    repo.add(_msg("in c1", conv="c1"))
    repo.add(_msg("in c2", conv="c2"))
    assert len(repo.list_for_conversation("c1")) == 1


def test_attachments_round_trip(session):
    repo = MessageRepository(session)
    m = _msg("see table")
    m.attachments = [Attachment(kind=AttachmentKind.TABLE, name="holdings.csv", content="a,b\n1,2")]
    row = repo.add(m)
    assert row.attachments[0]["kind"] == "table"
    assert row.attachments[0]["name"] == "holdings.csv"


def test_last_timestamp_returns_latest(session):
    repo = MessageRepository(session)
    base = datetime.now(timezone.utc)
    repo.add(_msg("a", ts=base))
    repo.add(_msg("b", ts=base + timedelta(seconds=30)))
    assert repo.last_timestamp("c1") == repo.list_for_conversation("c1")[-1].timestamp


def test_last_timestamp_none_for_empty_conversation(session):
    assert MessageRepository(session).last_timestamp("nope") is None
