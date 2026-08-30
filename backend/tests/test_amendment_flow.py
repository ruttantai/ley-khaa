from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.crystallizer.candidate import CandidateState
from ley_khaa.domain.states import TaskState
from ley_khaa.orchestrator.amendment import AmendmentProposal


class _Detector:
    """Stands in for the model. Returns a fixed proposal, and records that it
    was consulted so a test can prove stage 1 short-circuited."""

    def __init__(self, proposal=None):
        self.proposal = proposal
        self.calls = 0

    def detect(self, **kwargs):
        self.calls += 1
        return self.proposal


def _orchestrator(session):
    from ley_khaa.api.app import build_orchestrator
    from ley_khaa.projects.seeds import ensure_default_project

    ensure_default_project(session)
    return build_orchestrator(session)


def _running_task(orchestrator, *, mode, state=TaskState.AWAITING_APPROVAL):
    repo = orchestrator.repo
    row = repo.create(project="default", title="universe check", source_message_ids=["m1"])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    if state is not TaskState.INTERPRETED:
        repo.claim(row.id, expected=TaskState.INTERPRETED, target=state)
    repo.save_recommendation(row.id, mode=mode.value, confidence=0.9, risk=0.1, reason="x")
    return repo.get(row.id)


def _ready_candidate(orchestrator):
    return orchestrator.candidates.upsert(
        conversation_id="C1",
        candidate_key="k2",
        title="also flag duplicates",
        summary="s",
        state=CandidateState.READY,
        message_ids=["m2"],
        missing_fields=[],
        open_question=None,
    )


def test_auto_folds_the_amendment_without_creating_a_second_task(session, stub_execution):
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.AUTO)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="also flag dupes")
    )

    result = orchestrator._promote(candidate)

    assert result == target.id, "folding must reuse the target task, not create one"
    assert len(orchestrator.repo.list()) == 1
    folded = orchestrator.repo.get(target.id)
    assert "m2" in folded.source_message_ids
    assert orchestrator.candidates.get(candidate.id).task_id == target.id


def test_suggest_parks_the_amendment_for_a_human(session, stub_execution):
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.SUGGEST)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="also flag dupes")
    )

    assert orchestrator._promote(candidate) is None
    parked = orchestrator.candidates.get(candidate.id)
    assert CandidateState(parked.state) is CandidateState.AWAITING_TRIAGE
    assert parked.amends_task_id == target.id
    assert len(orchestrator.repo.list()) == 1, "parking must not create a task"


def test_an_executing_target_parks_even_on_auto(session, stub_execution):
    """The structural guard, end to end rather than in the engine alone."""
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.AUTO, state=TaskState.EXECUTING)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=1.0, reason="also flag dupes")
    )

    assert orchestrator._promote(candidate) is None
    parked = orchestrator.candidates.get(candidate.id)
    assert CandidateState(parked.state) is CandidateState.AWAITING_TRIAGE
    assert TaskState(orchestrator.repo.get(target.id).state) is TaskState.EXECUTING


def test_no_proposal_promotes_normally(session, stub_execution):
    orchestrator = _orchestrator(session)
    _running_task(orchestrator, mode=AutonomyMode.AUTO)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(None)

    task_id = orchestrator._promote(candidate)
    assert task_id is not None
    assert len(orchestrator.repo.list()) == 2


def test_a_human_fold_merges_the_parked_candidate(session, stub_execution):
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.SUGGEST)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="r")
    )
    orchestrator._promote(candidate)

    assert orchestrator.fold(candidate.id) == target.id
    assert "m2" in orchestrator.repo.get(target.id).source_message_ids
    assert CandidateState(orchestrator.candidates.get(candidate.id).state) is CandidateState.PROMOTED


def test_a_human_separate_promotes_it_as_its_own_task(session, stub_execution):
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.SUGGEST)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="r")
    )
    orchestrator._promote(candidate)

    new_id = orchestrator.separate(candidate.id)
    assert new_id != target.id
    assert len(orchestrator.repo.list()) == 2
    assert "m2" not in orchestrator.repo.get(target.id).source_message_ids


def test_a_fold_that_loses_the_race_returns_the_candidate_to_triage(session, stub_execution):
    """The target moved between the decision and the fold. Nothing is lost: the
    candidate stays parked and a human sees it again."""
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.SUGGEST)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="r")
    )
    orchestrator._promote(candidate)

    # The target races ahead into a state a fold may not touch.
    orchestrator.repo.claim(
        target.id, expected=TaskState.AWAITING_APPROVAL, target=TaskState.EXECUTING
    )

    assert orchestrator.fold(candidate.id) is None
    still_parked = orchestrator.candidates.get(candidate.id)
    assert CandidateState(still_parked.state) is CandidateState.AWAITING_TRIAGE
    # The loser must change NOTHING about the target: not its messages, and not
    # its state. Without the second assertion a fold that dragged a running task
    # back to CLASSIFIED without appending anything would still pass here.
    raced = orchestrator.repo.get(target.id)
    assert "m2" not in raced.source_message_ids
    assert TaskState(raced.state) is TaskState.EXECUTING


def test_a_parked_amendment_does_not_wedge_the_rest_of_the_conversation(
    session, stub_execution
):
    """The whole conversation must keep working after one amendment is parked.

    A candidate in AWAITING_TRIAGE is already handled as far as stage B is
    concerned. When it was missing from TERMINAL_STATES it stayed rendered as an
    ACTIVE candidate in the stage-B prompt, so the model re-reported the same
    candidate_key on the next message and upsert hit
    ensure_transition(AWAITING_TRIAGE -> READY), which is forbidden. Parking is
    the COMMON outcome, so one parked amendment made every later message in that
    conversation raise.

    Driven through ingest() rather than the engine directly so the failure is the
    real one: HeuristicLLM re-reports the parked candidate's key verbatim, because
    the key is derived from the first message the candidate owns.
    """
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.SUGGEST)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="r")
    )

    second = orchestrator.ingest(
        {"conversation_id": "C1", "text": "also compare the credit book against custody"}
    )
    parked = orchestrator.candidates.get(second.candidates[0].id)
    assert CandidateState(parked.state) is CandidateState.AWAITING_TRIAGE, "setup: not parked"

    # The third message. Before the fix this raised InvalidCandidateTransition.
    third = orchestrator.ingest(
        {"conversation_id": "C1", "text": "and pull the fx exposure report too"}
    )

    still_parked = orchestrator.candidates.get(parked.id)
    assert CandidateState(still_parked.state) is CandidateState.AWAITING_TRIAGE
    # The parked candidate is retired, so the new request forms a candidate of
    # its own rather than being swallowed into the one waiting on a human.
    assert third.candidates, "the third message formed no candidate at all"
    assert all(c.id != parked.id for c in third.candidates)
