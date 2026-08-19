from ley_khaa.crystallizer.engine import CrystallizerOutput
from ley_khaa.crystallizer.relevance import RelevanceVerdict
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.llm.router import Stage, model_for


def _judge(text, author="boss"):
    return HeuristicLLM().parse(
        choice=model_for(Stage.RELEVANCE_FILTER),
        system="s",
        user=f"author: {author}\ntext: {text}",
        output_format=RelevanceVerdict,
    )


def test_request_language_is_relevant():
    assert _judge("Can you compare the two universes and send the difference?").relevant is True


def test_greeting_is_noise():
    assert _judge("morning all").relevant is False


def test_laughter_is_noise():
    assert _judge("haha nice one").relevant is False


def test_crystallizer_output_groups_the_window_into_one_candidate():
    llm = HeuristicLLM()
    user = "## Recent messages\n[m1] boss: please compare the universes\n[m2] boss: haha\n"
    out = llm.parse(
        choice=model_for(Stage.CRYSTALLIZER),
        system="s",
        user=user,
        output_format=CrystallizerOutput,
    )
    assert isinstance(out, CrystallizerOutput)
    assert len(out.candidates) == 1
    assert out.candidates[0].message_ids == ["m1"]


def test_unknown_output_format_raises_clearly():
    import pytest
    from pydantic import BaseModel

    class Other(BaseModel):
        x: int

    with pytest.raises(NotImplementedError, match="HeuristicLLM"):
        HeuristicLLM().parse(
            choice=model_for(Stage.CRYSTALLIZER), system="s", user="u", output_format=Other
        )
