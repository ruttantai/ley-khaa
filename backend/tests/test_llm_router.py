import pytest

from ley_khaa.llm.router import Stage, model_for


@pytest.mark.parametrize(
    "stage,complexity,expected_model",
    [
        (Stage.RELEVANCE_FILTER, "routine", "claude-haiku-4-5"),
        (Stage.RELEVANCE_FILTER, "hard", "claude-haiku-4-5"),
        (Stage.CRYSTALLIZER, "routine", "claude-haiku-4-5"),
        (Stage.CRYSTALLIZER, "hard", "claude-opus-5"),
        (Stage.INTERPRETER, "routine", "claude-opus-5"),
        (Stage.VISION_EXTRACTION, "routine", "claude-opus-5"),
    ],
)
def test_model_for_policy(stage, complexity, expected_model):
    assert model_for(stage, complexity).model == expected_model


def test_haiku_never_advertises_thinking():
    # Haiku 4.5 is pre-4.6: adaptive thinking and effort both error on it.
    choice = model_for(Stage.RELEVANCE_FILTER, "routine")
    assert choice.supports_thinking is False


def test_opus_advertises_thinking():
    assert model_for(Stage.INTERPRETER, "routine").supports_thinking is True


def test_unknown_complexity_falls_back_to_routine():
    assert model_for(Stage.CRYSTALLIZER, "banana").model == "claude-haiku-4-5"
