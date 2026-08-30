from ley_khaa.domain.states import TaskState
from ley_khaa.llm.client import FakeLLM
from ley_khaa.orchestrator.amendment import AmendmentChoice, AmendmentDetector
from ley_khaa.persistence.repository import TaskRepository


def _task(session, *, project="acme", state=TaskState.AWAITING_APPROVAL, title="universe check"):
    repo = TaskRepository(session)
    row = repo.create(project=project, title=title, source_message_ids=[])
    if state is not TaskState.RECEIVED:
        repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    if state not in (TaskState.RECEIVED, TaskState.CLASSIFIED):
        repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    if state is TaskState.DONE:
        # DONE is not one hop from INTERPRETED (INTERPRETED -> {AWAITING_APPROVAL,
        # EXECUTING, FAILED} only) — walk the real path so this helper never
        # asserts a transition the state machine itself forbids.
        repo.claim(row.id, expected=TaskState.INTERPRETED, target=TaskState.EXECUTING)
        repo.claim(row.id, expected=TaskState.EXECUTING, target=TaskState.VALIDATING)
        repo.claim(row.id, expected=TaskState.VALIDATING, target=TaskState.DONE)
    elif state not in (TaskState.RECEIVED, TaskState.CLASSIFIED, TaskState.INTERPRETED):
        repo.claim(row.id, expected=TaskState.INTERPRETED, target=state)
    return repo.get(row.id)


def test_a_project_with_no_active_tasks_costs_nothing(session):
    """Stage 1 is free and is the common case — almost every request arrives
    into a project with nothing running."""
    llm = FakeLLM(responses=[])
    detector = AmendmentDetector(TaskRepository(session), llm)
    assert detector.detect(project="acme", title="t", summary="s") is None
    assert llm.calls == []


def test_a_done_task_is_not_active(session):
    _task(session, state=TaskState.DONE)
    llm = FakeLLM(responses=[])
    assert AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s"
    ) is None
    assert llm.calls == []


def test_a_task_parked_for_a_human_IS_active(session):
    """Deliberate: a task waiting in front of a person is the one a follow-up
    message is most likely to be amending."""
    target = _task(session, state=TaskState.AWAITING_APPROVAL)
    llm = FakeLLM(
        responses=[AmendmentChoice(task_id=target.id, confidence=0.9, reason="also flag dupes")]
    )
    proposal = AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="also flag duplicates", summary="s"
    )
    assert proposal is not None
    assert proposal.task_id == target.id
    assert len(llm.calls) == 1


def test_a_low_confidence_answer_is_no_proposal(session):
    target = _task(session)
    llm = FakeLLM(responses=[AmendmentChoice(task_id=target.id, confidence=0.5, reason="maybe")])
    assert AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s"
    ) is None


def test_a_hallucinated_task_id_is_discarded(session, caplog):
    _task(session)
    llm = FakeLLM(responses=[AmendmentChoice(task_id="not-a-task", confidence=0.99, reason="x")])
    with caplog.at_level("INFO", logger="ley_khaa.orchestrator.amendment"):
        result = AmendmentDetector(TaskRepository(session), llm).detect(
            project="acme", title="t", summary="s"
        )
    assert result is None
    # A project with no active tasks, a low-confidence answer, and a failed
    # model call all also produce None with no log record at all. The only
    # route that logs "unknown task" is the id-verification guard itself, so
    # this — rather than the bare None — is what proves THIS guard fired
    # instead of some other path (or the broad except in detect()) doing so.
    assert any("unknown task" in r.message for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_a_task_in_another_project_is_never_proposed(session, caplog):
    """Cross-project amendment is out of scope, and a task from another client's
    project must never be offered as a fold target."""
    other = _task(session, project="globex")
    _task(session, project="acme")
    llm = FakeLLM(responses=[AmendmentChoice(task_id=other.id, confidence=0.99, reason="x")])
    with caplog.at_level("INFO", logger="ley_khaa.orchestrator.amendment"):
        proposal = AmendmentDetector(TaskRepository(session), llm).detect(
            project="acme", title="t", summary="s"
        )
    assert proposal is None
    # Same guard as the hallucination test above: `other` is a real task id,
    # just not one active() returned for THIS project, so the only way this
    # can be None is the same unknown-task-id check discarding a name that
    # was never on the list it was shown.
    assert any("unknown task" in r.message for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_the_candidate_s_own_task_is_excluded(session):
    """Guards against a task proposing itself as its own amendment target."""
    mine = _task(session)
    llm = FakeLLM(responses=[])
    assert AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s", exclude_task_ids=(mine.id,)
    ) is None
    assert llm.calls == []


def test_a_failing_model_call_yields_no_proposal(session):
    """A missed amendment costs one duplicate task a human can see and reject.
    A detector that raises would take intake down with it."""
    _task(session)
    llm = FakeLLM(responses=[RuntimeError("transport exploded")])
    assert AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s"
    ) is None
