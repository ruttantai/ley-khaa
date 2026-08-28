import pytest

from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.router import Stage, model_for
from ley_khaa.memory.fingerprint import request_fingerprint
from ley_khaa.memory.matcher import CONFIDENCE_FLOOR, MemoryMatcher
from ley_khaa.memory.models import MemoryDecision
from ley_khaa.persistence.memory_repository import MemoryRepository
from ley_khaa.persistence.orm import MemoryRow


def _spec() -> TaskSpec:
    return TaskSpec(
        intent="compare the universes", inputs=["bloomberg", "factset"],
        operation="set_difference", output_format="csv", certainty=0.9,
    )


def _seed(session, *, project="acme", texts=("compare bloomberg against factset",)):
    repo = MemoryRepository(session)
    fingerprint = request_fingerprint(list(texts))
    row = repo.record(
        project=project, fingerprint=fingerprint, intent="compare universes",
        spec=_spec(), task_id="source-task-1",
    )
    assert row is not None
    return repo, row


def test_a_fingerprint_hit_never_calls_the_model(session):
    """The whole point. A cache that costs a model call to consult is not a
    cache — it is a slower interpretation."""
    repo, row = _seed(session)
    llm = FakeLLM(responses=[])

    match = MemoryMatcher(repo, llm).recall("acme", ["compare bloomberg against factset"])

    assert match is not None
    assert match.id == row.id
    assert llm.calls == []


def test_a_paraphrase_is_found_by_the_model(session):
    repo, row = _seed(session)
    llm = FakeLLM(responses=[
        MemoryDecision(memory_id=row.id, confidence=0.9, reason="same standing request")
    ])

    match = MemoryMatcher(repo, llm).recall(
        "acme", ["please compare the bloomberg universe against factset again"]
    )

    assert match is not None
    assert match.id == row.id
    assert len(llm.calls) == 1


def test_a_low_confidence_answer_is_not_a_match(session):
    repo, row = _seed(session)
    llm = FakeLLM(responses=[
        MemoryDecision(memory_id=row.id, confidence=CONFIDENCE_FLOOR - 0.01, reason="maybe")
    ])

    assert MemoryMatcher(repo, llm).recall("acme", ["something entirely unrelated today"]) is None


def test_a_model_naming_an_unknown_memory_is_not_a_match(session):
    """Model output is untrusted here exactly as it is in the registry matcher."""
    repo, row = _seed(session)
    llm = FakeLLM(responses=[
        MemoryDecision(memory_id="does-not-exist", confidence=0.99, reason="confident nonsense")
    ])

    assert MemoryMatcher(repo, llm).recall("acme", ["something entirely unrelated today"]) is None


def test_a_memory_from_another_project_is_never_recalled(session):
    repo, row = _seed(session, project="acme")
    llm = FakeLLM(responses=[])

    match = MemoryMatcher(repo, llm).recall("globex", ["compare bloomberg against factset"])

    assert match is None
    # Nothing in scope to match: the model must never even be asked.
    assert llm.calls == []


def test_a_broken_model_call_is_a_miss_not_a_crash(session):
    """A cache that fails must cost only the work it was trying to save."""
    class Boom:
        name = "boom"

        def parse(self, **kwargs):
            raise RuntimeError("connection reset")

    repo, row = _seed(session)
    assert MemoryMatcher(repo, Boom()).recall("acme", ["something entirely unrelated today"]) is None


def test_the_offline_stand_in_answers_no_match(session):
    """With no API key the fast path is fingerprint-only, not broken.

    Asserted directly against HeuristicLLM.parse(), not through
    MemoryMatcher.recall(): recall() swallows every exception at its
    boundary, including NotImplementedError, so asserting through it would
    still pass even if HeuristicLLM had no rule for MemoryDecision at all and
    every offline recall miss silently degraded to a logged traceback.
    """
    from ley_khaa.llm.heuristic import HeuristicLLM

    decision = HeuristicLLM().parse(
        choice=model_for(Stage.MEMORY_MATCH), system="s", user="u",
        output_format=MemoryDecision,
    )
    assert decision.memory_id is None


def test_an_empty_request_never_recalls(session):
    """Even a stored row with an empty fingerprint (defensively, in case one
    ever exists) must never be recalled by another blank request — the same
    invariant MemoryRepository.record() enforces on the write side."""
    session.add(MemoryRow(
        id="mem-empty", project="acme", fingerprint="", intent="blank",
        spec=_spec().model_dump(mode="json"), source_task_id="task-0",
    ))
    session.commit()
    llm = FakeLLM(responses=[])

    match = MemoryMatcher(MemoryRepository(session), llm).recall("acme", ["", "the", "and"])

    assert match is None
    assert llm.calls == []
