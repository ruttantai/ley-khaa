import pytest

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
