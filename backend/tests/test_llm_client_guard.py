import pytest
from pydantic import BaseModel

from ley_khaa.llm.client import AnthropicLLM
from ley_khaa.llm.router import Stage, model_for


class Answer(BaseModel):
    verdict: str


class _NoneReturning:
    """Reproduces a response that stopped on max_tokens: the SDK returns a
    message whose parsed_output is None rather than raising."""

    def __init__(self):
        self.messages = self

    def parse(self, **kwargs):
        return type("R", (), {"parsed_output": None})()


def test_a_none_parse_result_raises_naming_the_cause():
    llm = AnthropicLLM(client=_NoneReturning())
    with pytest.raises(ValueError, match="no parsed output"):
        llm.parse(
            choice=model_for(Stage.INTERPRETER),
            system="s",
            user="u",
            output_format=Answer,
        )


def test_a_none_extract_image_result_raises_naming_the_cause():
    """Same defect, one method over: extract_image at client.py:147 was just
    as unguarded as parse() at :106. Phase 7's VisionExtractor contains a
    None from THIS path downstream (isinstance check in extract()), but the
    source itself must not hand a caller a None it never asked for."""
    llm = AnthropicLLM(client=_NoneReturning())
    with pytest.raises(ValueError, match="no parsed output"):
        llm.extract_image(
            choice=model_for(Stage.VISION_EXTRACTION),
            system="s",
            user="u",
            image=b"\x89PNG\r\n\x1a\nbytes",
            media_type="image/png",
            output_format=Answer,
        )
