from ley_khaa.crystallizer.engine import CrystallizerOutput
from ley_khaa.crystallizer.relevance import RelevanceVerdict
from ley_khaa.interpreter.spec import TaskSpec
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


_INTERPRETER = model_for(Stage.INTERPRETER)

_UNIVERSE_PROMPT = """## Request
title: compare the Bloomberg universe against FactSet

## Messages
[m1] boss: can you compare the Bloomberg universe against FactSet
[m2] boss: month end please, and send me what's missing as an Excel file"""

_VAGUE_PROMPT = """## Request
title: put together a report

## Messages
[m1] boss: can you put together a report on the holdings"""


def _interpret(prompt: str) -> TaskSpec:
    return HeuristicLLM().parse(
        choice=_INTERPRETER, system="", user=prompt, output_format=TaskSpec
    )


def test_heuristic_reads_a_complete_request():
    spec = _interpret(_UNIVERSE_PROMPT)
    assert spec.operation == "set_difference"
    assert spec.output_format == "xlsx"
    assert spec.missing_fields == []
    assert spec.source_message_ids == ["m1", "m2"]


def test_heuristic_flags_a_missing_output_format():
    spec = _interpret(_VAGUE_PROMPT)
    assert "output_format" in spec.missing_fields


def test_heuristic_never_earns_auto_on_its_own():
    """The offline stand-in is a regex, not a mind. It must not claim high
    certainty, or a fresh clone with no API key would silently run tasks
    end-to-end on keyword matching."""
    assert _interpret(_UNIVERSE_PROMPT).certainty < 0.85


def test_heuristic_reads_urgency_and_recipient():
    spec = _interpret(
        "## Messages\n[m1] boss: urgent - compare the lists and send to alice as csv"
    )
    assert spec.urgency == "high"
    assert spec.recipient == "alice"
    assert spec.output_format == "csv"
