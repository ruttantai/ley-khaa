import pytest

from ley_khaa.crystallizer.candidate import CandidateState, InvalidCandidateTransition
from ley_khaa.domain.states import TaskState, can_transition
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.repository import TaskRepository


def _candidate(session, state=CandidateState.READY):
    return CandidateRepository(session).upsert(
        conversation_id="C1",
        candidate_key="k",
        title="also flag duplicates",
        summary="s",
        state=state,
        message_ids=["m2"],
        missing_fields=[],
        open_question=None,
    )


def _task(session, state=TaskState.AWAITING_APPROVAL):
    repo = TaskRepository(session)
    row = repo.create(project="acme", title="t", source_message_ids=["m1"])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    if state is not TaskState.CLASSIFIED:
        repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    if state not in (TaskState.CLASSIFIED, TaskState.INTERPRETED):
        repo.claim(row.id, expected=TaskState.INTERPRETED, target=state)
    return repo.get(row.id)


def test_an_interpreted_task_can_go_back_to_classified():
    """Folding re-interprets over the enlarged message set, so it needs this
    edge. Declared because it is reachable — phase 2 removed one that was not."""
    assert can_transition(TaskState.INTERPRETED, TaskState.CLASSIFIED)


def test_a_parked_task_can_go_back_to_classified():
    assert can_transition(TaskState.AWAITING_APPROVAL, TaskState.CLASSIFIED)


def test_executing_still_cannot_go_back_to_classified():
    """The structural guard's counterpart in the state table: even if some caller
    tried, the machine refuses."""
    assert not can_transition(TaskState.EXECUTING, TaskState.CLASSIFIED)


def test_a_ready_candidate_can_be_claimed_for_triage(session):
    candidate = _candidate(session)
    repo = CandidateRepository(session)
    assert repo.claim_for_triage(
        candidate.id, task_id="t1", reason="also flag dupes", confidence=0.9
    ) is True
    row = repo.get(candidate.id)
    assert CandidateState(row.state) is CandidateState.AWAITING_TRIAGE
    assert row.amends_task_id == "t1"
    assert row.amendment_confidence == 0.9


def test_only_one_caller_wins_the_triage_claim(session):
    candidate = _candidate(session)
    repo = CandidateRepository(session)
    assert repo.claim_for_triage(candidate.id, task_id="t1", reason="r", confidence=0.9) is True
    assert repo.claim_for_triage(candidate.id, task_id="t2", reason="r", confidence=0.9) is False
    assert repo.get(candidate.id).amends_task_id == "t1"


def test_a_triaged_candidate_can_be_claimed_for_folding(session):
    candidate = _candidate(session)
    repo = CandidateRepository(session)
    repo.claim_for_triage(candidate.id, task_id="t1", reason="r", confidence=0.9)
    assert repo.claim_for_fold(candidate.id) is True
    assert CandidateState(repo.get(candidate.id).state) is CandidateState.PROMOTED
    assert repo.claim_for_fold(candidate.id) is False


def test_a_promoted_candidate_cannot_slide_back_to_triage(session):
    """PROMOTED stays terminal. A folded candidate is done."""
    repo = CandidateRepository(session)
    candidate = _candidate(session)
    repo.claim_for_promotion(candidate.id)
    with pytest.raises(InvalidCandidateTransition):
        _candidate(session, state=CandidateState.AWAITING_TRIAGE)


def test_fold_into_appends_messages_and_reopens_the_task(session):
    task = _task(session, TaskState.AWAITING_APPROVAL)
    repo = TaskRepository(session)
    assert repo.fold_into(
        task.id, message_ids=["m2"], expected=TaskState.AWAITING_APPROVAL
    ) is True
    row = repo.get(task.id)
    assert row.source_message_ids == ["m1", "m2"]
    assert TaskState(row.state) is TaskState.CLASSIFIED
    assert row.open_question is None


def test_fold_into_loses_when_the_task_has_already_moved(session):
    """The race the spec names: the target can move between the decision and the
    fold. The loser must change nothing at all."""
    task = _task(session, TaskState.EXECUTING)
    repo = TaskRepository(session)
    assert repo.fold_into(
        task.id, message_ids=["m2"], expected=TaskState.AWAITING_APPROVAL
    ) is False
    row = repo.get(task.id)
    assert row.source_message_ids == ["m1"], "a lost fold must not append messages"
    assert TaskState(row.state) is TaskState.EXECUTING
