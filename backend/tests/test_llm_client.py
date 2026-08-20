import pytest
from pydantic import BaseModel

from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.router import Stage, model_for


class Verdict(BaseModel):
    relevant: bool


def test_fake_llm_returns_queued_responses_in_order():
    llm = FakeLLM([Verdict(relevant=True), Verdict(relevant=False)])
    choice = model_for(Stage.RELEVANCE_FILTER)
    first = llm.parse(choice=choice, system="s", user="u", output_format=Verdict)
    second = llm.parse(choice=choice, system="s", user="u", output_format=Verdict)
    assert first.relevant is True
    assert second.relevant is False


def test_fake_llm_records_calls():
    llm = FakeLLM([Verdict(relevant=True)])
    choice = model_for(Stage.RELEVANCE_FILTER)
    llm.parse(choice=choice, system="sys-prompt", user="the message", output_format=Verdict)
    assert len(llm.calls) == 1
    assert llm.calls[0].choice.model == "claude-haiku-4-5"
    assert llm.calls[0].user == "the message"


def test_fake_llm_raises_when_exhausted():
    llm = FakeLLM([])
    with pytest.raises(AssertionError, match="FakeLLM exhausted"):
        llm.parse(choice=model_for(Stage.RELEVANCE_FILTER), system="s", user="u", output_format=Verdict)
