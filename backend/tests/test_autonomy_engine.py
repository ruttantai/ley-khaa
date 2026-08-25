from ley_khaa.autonomy.engine import (
    _AUTO_CONFIDENCE,
    _AUTO_RISK,
    _BASELINE_RISK,
    _COPILOT_CONFIDENCE,
    _COPILOT_RISK,
    recommend,
)
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


def test_the_offline_heuristic_can_never_reach_auto():
    """Cross-module safety invariant: a fresh clone with no API key must never
    run tasks unattended on regex keyword matching."""
    from ley_khaa.llm.heuristic import _HEURISTIC_CERTAINTY

    assert _HEURISTIC_CERTAINTY < _AUTO_CONFIDENCE
    # Proven through the public API too, not only by comparing constants.
    assert recommend(_spec(certainty=_HEURISTIC_CERTAINTY)).mode is not AutonomyMode.AUTO


def test_the_mode_thresholds_are_pinned():
    """These four numbers are the policy. Pin them so a drift fails here."""
    assert (_AUTO_CONFIDENCE, _AUTO_RISK) == (0.85, 0.25)
    assert (_COPILOT_CONFIDENCE, _COPILOT_RISK) == (0.6, 0.6)


def test_each_risk_contribution_is_pinned_by_magnitude():
    base = recommend(_spec()).risk
    assert base == _BASELINE_RISK == 0.1
    assert round(recommend(_spec(recipient="boss")).risk - base, 4) == 0.35
    assert round(recommend(_spec(intent="settle the invoice")).risk - base, 4) == 0.4
    assert round(recommend(_spec(urgency="high")).risk - base, 4) == 0.15


def test_the_unsettled_conversation_penalty_is_pinned():
    settled = recommend(_spec()).confidence
    unsettled = recommend(_spec(), candidate_missing_fields=["deadline"]).confidence
    assert round(settled - unsettled, 4) == 0.1
