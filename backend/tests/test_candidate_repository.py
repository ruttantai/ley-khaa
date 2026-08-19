import pytest
from sqlalchemy.exc import IntegrityError

from ley_khaa.crystallizer.candidate import CandidateState, InvalidCandidateTransition
from ley_khaa.persistence.candidate_repository import CandidateRepository


def _upsert(repo, key="cand-a", state=CandidateState.FORMING, message_ids=None, conv="c1"):
    return repo.upsert(
        conversation_id=conv,
        candidate_key=key,
        title="Universe check",
        summary="Compare Bloomberg vs FactSet",
        state=state,
        message_ids=message_ids if message_ids is not None else ["m1"],
        missing_fields=[],
        open_question=None,
    )


def test_upsert_creates_then_updates_same_row(session):
    repo = CandidateRepository(session)
    first = _upsert(repo)
    second = _upsert(repo, state=CandidateState.CRYSTALLIZING, message_ids=["m1", "m2"])
    assert first.id == second.id
    assert second.state == "crystallizing"
    assert second.message_ids == ["m1", "m2"]
    assert len(repo.list_for_conversation("c1")) == 1


def test_candidate_keys_are_scoped_per_conversation(session):
    repo = CandidateRepository(session)
    _upsert(repo, conv="c1")
    _upsert(repo, conv="c2")
    assert len(repo.list_for_conversation("c1")) == 1
    assert len(repo.list_for_conversation("c2")) == 1


def test_upsert_rejects_illegal_transition(session):
    repo = CandidateRepository(session)
    row = _upsert(repo, state=CandidateState.READY)
    assert repo.claim_for_promotion(row.id)
    repo.attach_task(row.id, task_id="t1")
    with pytest.raises(InvalidCandidateTransition):
        _upsert(repo, state=CandidateState.FORMING)


def test_claim_then_attach_records_task_id(session):
    repo = CandidateRepository(session)
    row = _upsert(repo, state=CandidateState.READY)
    assert repo.claim_for_promotion(row.id) is True
    promoted = repo.attach_task(row.id, task_id="task-99")
    assert promoted.state == "promoted"
    assert promoted.task_id == "task-99"


def test_only_one_caller_can_claim_a_ready_candidate(session):
    """The conditional update is what makes promotion safe under concurrency."""
    repo = CandidateRepository(session)
    row = _upsert(repo, state=CandidateState.READY)
    assert repo.claim_for_promotion(row.id) is True
    # Second caller read the same row as READY and now tries to claim it.
    assert repo.claim_for_promotion(row.id) is False


def test_claiming_a_candidate_that_was_never_ready_fails(session):
    repo = CandidateRepository(session)
    row = _upsert(repo, state=CandidateState.CRYSTALLIZING)
    assert repo.claim_for_promotion(row.id) is False
    assert repo.get_by_key("c1", "cand-a").state == "crystallizing"


def test_list_by_state_filters(session):
    repo = CandidateRepository(session)
    _upsert(repo, key="a", state=CandidateState.READY)
    _upsert(repo, key="b", state=CandidateState.FORMING)
    ready = repo.list_by_state(CandidateState.READY)
    assert [r.candidate_key for r in ready] == ["a"]


def test_get_by_key_returns_none_when_absent(session):
    assert CandidateRepository(session).get_by_key("c1", "nope") is None


def test_open_question_round_trips(session):
    repo = CandidateRepository(session)
    row = repo.upsert(
        conversation_id="c1",
        candidate_key="k",
        title="t",
        summary="s",
        state=CandidateState.CRYSTALLIZING,
        message_ids=["m1"],
        missing_fields=["output_format"],
        open_question="Excel or CSV?",
    )
    assert row.open_question == "Excel or CSV?"
    assert row.missing_fields == ["output_format"]


def test_composite_uniqueness_prevents_duplicate_in_same_conversation(session):
    """Verify the composite unique constraint on (conversation_id, candidate_key)."""
    from ley_khaa.persistence.orm import CandidateRow
    import uuid

    repo = CandidateRepository(session)
    # Create first candidate
    _upsert(repo, key="dup", conv="c1")

    # Try to create another row with the same (conversation_id, candidate_key) directly
    # This should fail at the database level
    duplicate = CandidateRow(
        id=str(uuid.uuid4()),
        conversation_id="c1",
        candidate_key="dup",
        state=CandidateState.FORMING.value,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()


def test_scoping_invariant_same_key_different_conversations_allowed(session):
    """Verify that the same candidate_key can exist in different conversations."""
    repo = CandidateRepository(session)
    # Create candidate with key "shared" in conversation c1
    row1 = _upsert(repo, key="shared", conv="c1", state=CandidateState.READY)
    # Create candidate with key "shared" in conversation c2
    row2 = _upsert(repo, key="shared", conv="c2", state=CandidateState.FORMING)

    # Both should exist and be distinct
    assert row1.id != row2.id
    assert row1.conversation_id == "c1"
    assert row2.conversation_id == "c2"
    assert row1.candidate_key == row2.candidate_key == "shared"

    # Each conversation should have exactly one candidate with that key
    assert len(repo.list_for_conversation("c1")) == 1
    assert len(repo.list_for_conversation("c2")) == 1


def test_upsert_recovers_when_a_concurrent_insert_wins_the_race(session):
    """Genuinely drives the IntegrityError recovery branch in upsert.

    The branch needs the pre-check to miss AND the insert to then collide, which
    only happens when another request commits the same (conversation_id,
    candidate_key) in between. A before_flush hook reproduces that interleaving:
    it fires after upsert's get_by_key returned None, and a second session commits
    the winning row right then. The duplicate INSERT, the IntegrityError, the
    rollback and the recovery update are all real.
    """
    from sqlalchemy import event
    from sqlalchemy.orm import sessionmaker

    other_session = sessionmaker(
        bind=session.get_bind(), autoflush=False, expire_on_commit=False, future=True
    )
    repo = CandidateRepository(session)
    winner: dict[str, object] = {}
    fired: list[int] = []

    def commit_the_winner(sess, flush_context, instances):
        if fired:  # only race the first flush; the recovery path flushes again
            return
        fired.append(1)
        other = other_session()
        try:
            winner["row"] = _upsert(
                CandidateRepository(other),
                key="race-key",
                conv="race-conv",
                state=CandidateState.FORMING,
                message_ids=["winner-orig"],
            )
        finally:
            other.close()

    event.listen(session, "before_flush", commit_the_winner)
    try:
        result = repo.upsert(
            conversation_id="race-conv",
            candidate_key="race-key",
            title="Loser title",
            summary="Loser summary",
            state=CandidateState.CRYSTALLIZING,
            message_ids=["m1", "m2"],
            missing_fields=["field1"],
            open_question="Q?",
        )
    finally:
        event.remove(session, "before_flush", commit_the_winner)

    assert fired, "the race was never interposed"
    # The row that won the race is the one that survives...
    assert result.id == winner["row"].id
    assert len(repo.list_for_conversation("race-conv")) == 1
    # ...and the losing caller's update is applied on top of it.
    assert result.state == "crystallizing"
    assert result.title == "Loser title"
    assert result.summary == "Loser summary"
    assert result.message_ids == ["m1", "m2"]
    assert result.missing_fields == ["field1"]
    assert result.open_question == "Q?"
