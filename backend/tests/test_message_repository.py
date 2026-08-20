from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy.exc import IntegrityError
import pytest

from ley_khaa.domain.models import Attachment, AttachmentKind, Message
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.orm import MessageRow


def _utc(value):
    # SQLite hands back naive datetimes even for timezone=True columns.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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
    # Asserted against a literal: comparing to list_for_conversation()[-1].timestamp
    # is verbatim the implementation, so it could never fail.
    repo = MessageRepository(session)
    base = datetime(2026, 8, 19, 9, 30, tzinfo=timezone.utc)
    repo.add(_msg("b", ts=base + timedelta(seconds=30)))
    repo.add(_msg("a", ts=base))
    assert _utc(repo.last_timestamp("c1")) == datetime(2026, 8, 19, 9, 30, 30, tzinfo=timezone.utc)


def test_last_timestamp_none_for_empty_conversation(session):
    assert MessageRepository(session).last_timestamp("nope") is None


def test_unique_constraint_prevents_duplicate_external_id(session):
    """Verify the DB-level unique constraint on external_id is real."""
    # Insert a row with external_id directly
    row1 = MessageRow(
        id=str(uuid.uuid4()),
        external_id="unique-123",
        source="test",
        client="test",
        conversation_id="c1",
        author="test",
        text="first",
    )
    session.add(row1)
    session.commit()

    # Attempt to insert another row with the same external_id
    row2 = MessageRow(
        id=str(uuid.uuid4()),
        external_id="unique-123",
        source="test",
        client="test",
        conversation_id="c1",
        author="test",
        text="second",
    )
    session.add(row2)
    with pytest.raises(IntegrityError):
        session.commit()


def test_add_recovers_when_a_concurrent_insert_wins_the_race(session):
    """Genuinely drives the IntegrityError recovery branch in MessageRepository.add.

    The branch is only reachable when the fast-path lookup misses AND the insert
    then collides — i.e. when another request commits the same external_id in
    between. That interleaving is reproduced here with a before_flush hook on this
    session: the hook fires after add()'s lookup has already returned None, and a
    second session commits the winning row at that exact moment. The duplicate
    INSERT, the IntegrityError, the rollback and the recovery lookup are all real.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker

    other_session = sessionmaker(
        bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True
    )
    repo = MessageRepository(session)
    winner: dict[str, MessageRow] = {}
    fired: list[int] = []

    def commit_the_winner(sess, flush_context, instances):
        if fired:  # only race the first flush; the recovery path flushes again
            return
        fired.append(1)
        other = other_session()
        try:
            winner["row"] = MessageRepository(other).add(
                _msg("winner", external_id="race-1")
            )
        finally:
            other.close()

    event.listen(session, "before_flush", commit_the_winner)
    try:
        row = repo.add(_msg("loser", external_id="race-1"))
    finally:
        event.remove(session, "before_flush", commit_the_winner)

    assert fired, "the race was never interposed"
    # The loser must return the row that won, not raise and not duplicate.
    assert row.id == winner["row"].id
    assert row.text == "winner"
    assert len(repo.list_for_conversation("c1")) == 1


def test_add_reraises_an_integrity_error_that_is_not_the_duplicate_external_id(session):
    """The recovery branch re-raises anything that is not the race it handles."""
    repo = MessageRepository(session)
    first = _msg("first", external_id=None)
    repo.add(first)
    # Same primary key, no external_id: nothing for the recovery lookup to find.
    with pytest.raises(IntegrityError):
        repo.add(first)


def test_record_verdict_persists_stage_a_output(session):
    repo = MessageRepository(session)
    row = repo.add(_msg("compare the universes"))
    assert row.relevant is None and row.topic is None and row.confidence is None

    updated = repo.record_verdict(row.id, relevant=True, topic="universe-check", confidence=0.82)
    assert updated.relevant is True
    assert updated.topic == "universe-check"
    assert updated.confidence == 0.82


def test_window_can_exclude_messages_stage_a_judged_noise(session):
    repo = MessageRepository(session)
    base = datetime.now(timezone.utc)
    keep = repo.add(_msg("compare the universes", ts=base))
    noise = repo.add(_msg("haha nice one", ts=base + timedelta(seconds=1)))
    unjudged = repo.add(_msg("month end please", ts=base + timedelta(seconds=2)))
    repo.record_verdict(keep.id, relevant=True, topic="universe-check", confidence=0.9)
    repo.record_verdict(noise.id, relevant=False, topic="chatter", confidence=0.9)

    texts = [m.text for m in repo.window("c1", exclude_noise=True)]
    assert texts == ["compare the universes", "month end please"]
    # Unjudged messages are kept: absence of a verdict is not evidence of noise.
    assert unjudged.text in texts
    # And the default still returns everything.
    assert len(repo.window("c1")) == 3
