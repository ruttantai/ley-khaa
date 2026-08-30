"""Backlog item 6: two writes landed before the state claim that authorises them."""
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.repository import TaskRepository


def test_a_task_that_loses_the_interpretation_race_keeps_no_spec(session, monkeypatch):
    """A task whose CLASSIFIED -> INTERPRETED claim loses must not be left
    carrying the spec (or the memory attribution) for a path it did not take."""
    from ley_khaa.orchestrator.driver import TaskDriver
    from ley_khaa.llm.heuristic import HeuristicLLM
    from ley_khaa.persistence.candidate_repository import CandidateRepository
    from ley_khaa.persistence.message_repository import MessageRepository

    repo = TaskRepository(session)
    row = repo.create(project="default", title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)

    driver = TaskDriver(
        repo,
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
    )

    # Simulate another worker winning the claim first: every claim from here on
    # loses, which is exactly what the real race looks like from this side.
    monkeypatch.setattr(TaskRepository, "claim", lambda *a, **k: False)

    driver._after_spec(repo.get(row.id), _spec())

    assert repo.get(row.id).spec is None, "a lost claim must leave no spec behind"


def _spec():
    from ley_khaa.interpreter.spec import TaskSpec

    return TaskSpec(
        intent="compare",
        inputs=["a", "b"],
        operation="set_difference",
        output_format="csv",
        recipient=None,
        urgency="normal",
        missing_fields=[],
        source_message_ids=[],
        certainty=0.9,
    )
