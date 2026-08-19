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
    repo.mark_promoted(row.id, task_id="t1")
    with pytest.raises(InvalidCandidateTransition):
        _upsert(repo, state=CandidateState.FORMING)


def test_mark_promoted_records_task_id(session):
    repo = CandidateRepository(session)
    row = _upsert(repo, state=CandidateState.READY)
    promoted = repo.mark_promoted(row.id, task_id="task-99")
    assert promoted.state == "promoted"
    assert promoted.task_id == "task-99"


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


def test_upsert_handles_race_condition_and_applies_update(session):
    """Verify upsert catches IntegrityError on duplicate and applies the requested update to the winner."""
    from ley_khaa.persistence.orm import CandidateRow
    import uuid
    from sqlalchemy.exc import IntegrityError

    repo = CandidateRepository(session)

    # Simulate a race: create row that will "win"
    race_winner = CandidateRow(
        id=str(uuid.uuid4()),
        conversation_id="race-conv",
        candidate_key="race-key",
        state=CandidateState.FORMING.value,
        title="Winner original title",
        summary="Winner original summary",
        message_ids=["winner-orig"],
        missing_fields=[],
        open_question=None,
    )
    session.add(race_winner)
    session.commit()
    winner_id = race_winner.id

    # Create a separate repository instance (simulating a different request)
    repo2 = CandidateRepository(session)

    # This upsert will fail because the key already exists (race condition)
    # upsert should catch IntegrityError, re-fetch the existing row, and apply the update
    result = repo2.upsert(
        conversation_id="race-conv",
        candidate_key="race-key",
        title="Loser title",  # Different from winner
        summary="Loser summary",
        state=CandidateState.CRYSTALLIZING,  # Different state
        message_ids=["m1", "m2"],  # Different message_ids
        missing_fields=["field1"],
        open_question="Q?",
    )

    # The result should be the existing row (race winner's ID)
    assert result.id == winner_id
    # The state should be updated to what the loser requested
    assert result.state == "crystallizing"
    # The data should be updated to what the loser provided
    assert result.title == "Loser title"
    assert result.summary == "Loser summary"
    assert result.message_ids == ["m1", "m2"]
    assert result.missing_fields == ["field1"]
    assert result.open_question == "Q?"
