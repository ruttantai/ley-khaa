from ley_khaa.autonomy.engine import recommend_fold
from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.domain.states import TaskState


def test_auto_and_a_confident_detector_folds():
    decision = recommend_fold(
        mode=AutonomyMode.AUTO,
        detector_confidence=0.95,
        target_state=TaskState.AWAITING_APPROVAL,
        target_missing_fields=[],
    )
    assert decision.fold is True


def test_suggest_never_folds_on_its_own():
    decision = recommend_fold(
        mode=AutonomyMode.SUGGEST,
        detector_confidence=0.99,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=[],
    )
    assert decision.fold is False
    assert "suggest" in decision.reason.lower()


def test_copilot_never_folds_on_its_own():
    decision = recommend_fold(
        mode=AutonomyMode.COPILOT,
        detector_confidence=0.99,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=[],
    )
    assert decision.fold is False


def test_an_executing_target_never_folds_even_on_auto():
    """The structural guard (spec §3.8). A live sandbox workspace and a
    half-written bundle are facts, not thresholds — no confidence and no mode
    may reach past this."""
    for state in (TaskState.EXECUTING, TaskState.VALIDATING):
        decision = recommend_fold(
            mode=AutonomyMode.AUTO,
            detector_confidence=1.0,
            target_state=state,
            target_missing_fields=[],
        )
        assert decision.fold is False, state
        assert "under way" in decision.reason


def test_a_target_with_gaps_does_not_fold_automatically():
    decision = recommend_fold(
        mode=AutonomyMode.AUTO,
        detector_confidence=1.0,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=["output_format"],
    )
    assert decision.fold is False


def test_a_marginal_detector_confidence_does_not_fold():
    decision = recommend_fold(
        mode=AutonomyMode.AUTO,
        detector_confidence=0.85,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=[],
    )
    assert decision.fold is False


def test_no_mode_at_all_does_not_fold():
    """effective_mode is None until the dial has scored the target. Absence of
    a mode is not permission."""
    decision = recommend_fold(
        mode=None,
        detector_confidence=1.0,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=[],
    )
    assert decision.fold is False
