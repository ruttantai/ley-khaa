"""Backlog item 6: two writes landed before the state claim that authorises them."""
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.repository import TaskRepository


def test_a_task_that_loses_the_interpretation_race_keeps_no_spec(session, monkeypatch):
    """A task whose CLASSIFIED -> INTERPRETED claim loses must not be left
    carrying the spec for a path it did not take.

    Covers the half of _after_spec every path shares (save_spec /
    set_open_question). The remembered path's own extra write
    (save_memory_hit) needs memories= wired up and an actual memory hit to
    reach at all, and is covered separately below."""
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


def test_a_task_that_loses_the_interpretation_race_keeps_no_memory_attribution(
    session, monkeypatch
):
    """The remembered-spec branch in _interpret makes an extra write
    (save_memory_hit) that the generic _after_spec test above cannot reach —
    it never wires up memories=, so self.memory is None, _recall short-
    circuits, and the remembered branch is never entered. This wires up a
    real MemoryRepository and seeds a hit that fingerprint-matches the task's
    own message, so _interpret actually takes the remembered path, then loses
    the claim the same way the test above does.
    """
    from ley_khaa.domain.models import Message
    from ley_khaa.interpreter.spec import TaskSpec
    from ley_khaa.llm.heuristic import HeuristicLLM
    from ley_khaa.memory.fingerprint import request_fingerprint
    from ley_khaa.orchestrator.driver import TaskDriver
    from ley_khaa.persistence.candidate_repository import CandidateRepository
    from ley_khaa.persistence.memory_repository import MemoryRepository
    from ley_khaa.persistence.message_repository import MessageRepository

    text = "compare bloomberg against factset"
    messages = MessageRepository(session)
    msg = messages.add(
        Message(source="s", client="c", conversation_id="conv-1", author="boss", text=text)
    )

    repo = TaskRepository(session)
    row = repo.create(project="default", title=text[:40], source_message_ids=[msg.id])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)

    memories = MemoryRepository(session)
    remembered = memories.record(
        project="default",
        fingerprint=request_fingerprint([text]),
        intent="compare",
        spec=_spec(),
        task_id="some-other-task",
    )
    assert remembered is not None, "the seed itself must produce a recallable memory"

    driver = TaskDriver(
        repo,
        llm=HeuristicLLM(),
        messages=messages,
        candidates=CandidateRepository(session),
        memories=memories,
    )

    # Same simulated loss as the test above: every claim from here on loses.
    monkeypatch.setattr(TaskRepository, "claim", lambda *a, **k: False)

    won = driver._interpret(repo.get(row.id))

    assert won is False, "the fixture must actually reach the remembered branch and lose"
    result = repo.get(row.id)
    assert result.spec is None, "a lost claim must leave no spec behind"
    assert result.remembered_from_task_id is None, (
        "a lost claim must leave no memory attribution behind"
    )
    assert result.familiarity == 0


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
