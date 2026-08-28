import pytest

from ley_khaa.executor.resolver import ResolvedInput
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.router import Stage, model_for
from ley_khaa.persistence.workflow_repository import WorkflowRepository
from ley_khaa.registry.matcher import CONFIDENCE_FLOOR, RegistryMatcher
from ley_khaa.registry.models import RegistryDecision


def _spec(operation="set_difference", output_format="csv"):
    return TaskSpec(
        intent="compare the universes", inputs=["a", "b"], operation=operation,
        output_format=output_format, certainty=0.9,
    )


def _resolved():
    return [
        ResolvedInput(name="a", filename="a.csv", content="t\nAAA\n", source="catalog"),
        ResolvedInput(name="b", filename="b.csv", content="t\nBBB\n", source="catalog"),
    ]


def _seed(session, aliases=("set_difference",)):
    repo = WorkflowRepository(session)
    repo.create(
        name="set_difference", description="rows in A missing from B",
        operation_aliases=list(aliases), output_format="csv",
        inputs=[{"role": "left", "suffixes": [".csv"]}, {"role": "right", "suffixes": [".csv"]}],
        source="print('hi')", origin="seed",
    )
    return repo


def test_a_fingerprint_hit_never_calls_the_model(session):
    """The whole point. A cache that costs a model call to consult is not a
    cache — it is a slower synthesis."""
    repo = _seed(session)
    llm = FakeLLM(responses=[])

    match = RegistryMatcher(repo, llm).match(_spec(), _resolved())

    assert match is not None
    assert match.matched_by == "fingerprint"
    assert match.binding == {"left": "a.csv", "right": "b.csv"}
    assert llm.calls == []


def test_a_paraphrase_is_found_by_the_model(session):
    repo = _seed(session)
    llm = FakeLLM(responses=[
        RegistryDecision(workflow="set_difference", confidence=0.92, reason="same shape")
    ])

    match = RegistryMatcher(repo, llm).match(_spec(operation="compare_lists"), _resolved())

    assert match is not None
    assert match.matched_by == "model"
    assert len(llm.calls) == 1


def test_a_low_confidence_answer_is_not_a_match(session):
    repo = _seed(session)
    llm = FakeLLM(responses=[
        RegistryDecision(workflow="set_difference", confidence=CONFIDENCE_FLOOR - 0.01, reason="maybe")
    ])

    assert RegistryMatcher(repo, llm).match(_spec(operation="compare_lists"), _resolved()) is None


def test_a_model_naming_a_workflow_that_does_not_exist_is_not_a_match(session):
    """Model output is untrusted here exactly as it is in the crystallizer."""
    repo = _seed(session)
    llm = FakeLLM(responses=[
        RegistryDecision(workflow="does_not_exist", confidence=0.99, reason="confident nonsense")
    ])

    assert RegistryMatcher(repo, llm).match(_spec(operation="compare_lists"), _resolved()) is None


def test_a_model_match_that_cannot_bind_is_not_a_match(session):
    repo = _seed(session)
    llm = FakeLLM(responses=[
        RegistryDecision(workflow="set_difference", confidence=0.99, reason="looks right")
    ])
    wrong_shape = [ResolvedInput(name="a", filename="a.csv", content="x", source="catalog")]

    assert RegistryMatcher(repo, llm).match(_spec(operation="compare_lists"), wrong_shape) is None


def test_an_empty_registry_never_calls_the_model(session):
    """Nothing to match against, so asking would be a call that cannot succeed."""
    llm = FakeLLM(responses=[])
    assert RegistryMatcher(WorkflowRepository(session), llm).match(_spec(), _resolved()) is None
    assert llm.calls == []


def test_a_broken_model_call_is_a_miss_not_a_crash(session):
    """A cache that fails must cost only the work it was trying to save."""
    class Boom:
        name = "boom"

        def parse(self, **kwargs):
            raise RuntimeError("connection reset")

    repo = _seed(session)
    assert RegistryMatcher(repo, Boom()).match(_spec(operation="compare_lists"), _resolved()) is None


def test_the_offline_stand_in_answers_no_match(session):
    """With no API key the fast path is fingerprint-only, not broken.

    Asserted directly against HeuristicLLM.parse(), not through
    RegistryMatcher.match(): match() swallows every exception at its
    boundary, including NotImplementedError, so asserting through it would
    still pass even if HeuristicLLM had no rule for RegistryDecision at all
    and every offline match miss silently degraded to a logged traceback.
    """
    from ley_khaa.llm.heuristic import HeuristicLLM

    decision = HeuristicLLM().parse(
        choice=model_for(Stage.REGISTRY_MATCH), system="s", user="u",
        output_format=RegistryDecision,
    )
    assert decision.workflow is None
