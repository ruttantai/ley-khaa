import pytest

from datetime import datetime, timedelta, timezone

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


def test_fold_into_a_not_yet_interpreted_task_just_appends(session):
    """A target still in CLASSIFIED has not been interpreted yet, so there is
    nothing for a fold to re-trigger — it stays in CLASSIFIED, not stuck
    because CLASSIFIED -> CLASSIFIED is absent from the transition table."""
    task = _task(session, TaskState.CLASSIFIED)
    repo = TaskRepository(session)
    assert repo.fold_into(task.id, message_ids=["m2"], expected=TaskState.CLASSIFIED) is True
    row = repo.get(task.id)
    assert row.source_message_ids == ["m1", "m2"]
    assert TaskState(row.state) is TaskState.CLASSIFIED


def test_fold_into_a_classified_target_loses_if_it_moved_on(session):
    """The distinguishing behaviour _claim_same_state exists for: winning
    requires the row to STILL be in CLASSIFIED, not just to have been there
    when the caller looked. Replacing the guard with `return True`
    unconditionally passes every other fold_into test — this is the one that
    catches it."""
    task = _task(session, TaskState.CLASSIFIED)
    repo = TaskRepository(session)
    repo.claim(task.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)

    assert repo.fold_into(task.id, message_ids=["m2"], expected=TaskState.CLASSIFIED) is False
    row = repo.get(task.id)
    assert row.source_message_ids == ["m1"], "a lost fold must not append messages"
    assert TaskState(row.state) is TaskState.INTERPRETED


def test_fold_into_a_classified_target_loses_while_a_worker_holds_it(session):
    """A same-state CAS has no state change to make it mutually exclusive
    with an in-flight interpretation the way every other fold_into branch
    is (see fold_into's docstring), so the lease has to do that job instead.
    Winning here would silently fold a message the in-flight _interpret call
    never sees — the candidate is PROMOTED and terminal by that point, so
    nothing would ever return it to triage."""
    task = _task(session, TaskState.CLASSIFIED)
    repo = TaskRepository(session)
    assert repo.claim_lease(task.id, owner="w1", ttl_seconds=60) is True

    assert repo.fold_into(task.id, message_ids=["m2"], expected=TaskState.CLASSIFIED) is False
    row = repo.get(task.id)
    assert row.source_message_ids == ["m1"], "a lost fold must not append messages"
    assert TaskState(row.state) is TaskState.CLASSIFIED


def test_fold_into_survives_sqlites_naive_round_trip_against_a_leased_target(session_factory):
    """The same regression test_leased_task_id_survives_sqlites_naive_round_trip
    guards, one layer deeper: Orchestrator.fold calls repo.get() on the target
    BEFORE _fold -> fold_into (orchestrator.py's fold()), loading the row into
    the request session's identity map with a lease_expires_at that round-
    tripped NAIVE through SQLite. Without synchronize_session="fetch" on
    _claim_same_state's update, the default "evaluate" strategy re-checks the
    lease predicate against that identity-mapped naive value in Python and
    raises TypeError — exactly what POST /candidates/{id}/fold would do
    against a leased target. The shared `session` fixture the other CLASSIFIED
    tests above use cannot catch this: claim_lease there leaves an AWARE value
    in the identity map, so the comparison never round-trips through SQLite's
    naive storage at all.

    `target` below MUST stay a live local: Session's identity map holds ORM
    objects by WEAK reference, so an unassigned `reader.get(task.id)` is
    garbage-collected the instant the statement ends and the identity map
    forgets it — which would silently stop exercising the bug this test
    exists for. Orchestrator.fold's own `target = self.repo.get(...)` keeps
    exactly this kind of live reference across into fold_into, which is why
    it hits the identity map on the real route; `target` here does the same.
    """
    write = session_factory()
    task = _task(write, TaskState.CLASSIFIED)
    writer = TaskRepository(write)
    assert writer.claim_lease(task.id, owner="w1", ttl_seconds=60) is True
    write.close()

    read = session_factory()
    reader = TaskRepository(read)
    target = reader.get(task.id)  # kept alive, exactly like Orchestrator.fold's `target`
    assert target is not None

    assert reader.fold_into(task.id, message_ids=["m2"], expected=TaskState.CLASSIFIED) is False
    row = reader.get(task.id)
    assert row.source_message_ids == ["m1"], "a lost fold must not append messages"


def test_fold_into_treats_an_expired_lease_as_free(session):
    """Gives fold_into's `now` parameter its one caller: an expired lease must
    not block a fold the way test_..._while_a_worker_holds_it's live one does,
    and stating `now` explicitly is what makes that expiry boundary provable
    rather than implicit in whatever the clock happens to read."""
    task = _task(session, TaskState.CLASSIFIED)
    repo = TaskRepository(session)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert repo.claim_lease(task.id, owner="dead-worker", ttl_seconds=30, now=past) is True

    assert repo.fold_into(
        task.id,
        message_ids=["m2"],
        expected=TaskState.CLASSIFIED,
        now=datetime.now(timezone.utc),
    ) is True
    row = repo.get(task.id)
    assert row.source_message_ids == ["m1", "m2"]


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
