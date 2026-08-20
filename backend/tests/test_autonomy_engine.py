from ley_khaa.autonomy.engine import recommend
from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.interpreter.spec import TaskSpec


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two security universes",
        inputs=["bloomberg", "factset"],
        operation="set_difference",
        output_format="xlsx",
        recipient=None,
        urgency="normal",
        missing_fields=[],
        source_message_ids=["m1"],
        certainty=0.95,
    )
    return TaskSpec(**{**base, **overrides})


def test_confident_and_harmless_earns_auto():
    rec = recommend(_spec())
    assert rec.mode is AutonomyMode.AUTO


def test_delivering_to_someone_pulls_it_back_from_auto():
    """Irreversibility is the point: a report you can re-run is not a sent email."""
    rec = recommend(_spec(recipient="boss"))
    assert rec.mode is AutonomyMode.COPILOT
    assert "delivers" in rec.reason


def test_money_and_ambiguity_stay_in_suggest():
    rec = recommend(_spec(intent="settle the invoice differences", certainty=0.6,
                          missing_fields=["output_format"]))
    assert rec.mode is AutonomyMode.SUGGEST
    assert "touches money" in rec.reason


def test_each_missing_field_costs_confidence():
    none_missing = recommend(_spec()).confidence
    one_missing = recommend(_spec(missing_fields=["output_format"])).confidence
    two_missing = recommend(_spec(missing_fields=["output_format", "inputs"])).confidence
    assert none_missing > one_missing > two_missing


def test_an_unsettled_conversation_costs_confidence():
    settled = recommend(_spec()).confidence
    unsettled = recommend(_spec(), candidate_missing_fields=["deadline"]).confidence
    assert unsettled < settled


def test_urgency_raises_risk():
    assert recommend(_spec(urgency="high")).risk > recommend(_spec()).risk


def test_scores_stay_in_range():
    hot = _spec(intent="urgent wire payment", recipient="boss", urgency="high",
                certainty=0.1, missing_fields=["a", "b", "c", "d", "e"])
    rec = recommend(hot)
    assert 0.0 <= rec.confidence <= 1.0
    assert 0.0 <= rec.risk <= 1.0


def test_the_reason_reads_like_the_spec_examples():
    assert recommend(_spec()).reason == "95% sure, low risk → I suggest Auto"
    reason = recommend(_spec(certainty=0.5, missing_fields=["output_format"])).reason
    assert reason.startswith("30% sure")
    assert reason.endswith("→ stay in Suggest")
    assert "1 field(s) still unknown" in reason


def test_recommendation_is_deterministic():
    """Same inputs, same answer — every time. This is why it is not an LLM call."""
    spec = _spec(certainty=0.7, recipient="boss")
    assert recommend(spec) == recommend(spec)
