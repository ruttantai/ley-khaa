import base64

from ley_khaa.llm.client import AnthropicLLM
from ley_khaa.llm.router import Stage, model_for
from ley_khaa.vision.contract import VisionExtraction

IMAGE = b"\x89PNG\r\n\x1a\nfake-bytes"


class _Recorder:
    """Stands in for anthropic.Anthropic. Records the request it was handed."""

    def __init__(self, parsed):
        self.parsed = parsed
        self.kwargs = None
        self.messages = self

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return type("R", (), {"parsed_output": self.parsed})()


def _call(choice=None):
    parsed = VisionExtraction(kind="table", content="a,b\n1,2", summary="a table")
    rec = _Recorder(parsed)
    result = AnthropicLLM(client=rec).extract_image(
        choice=choice or model_for(Stage.VISION_EXTRACTION),
        system="read this image",
        user="chart.png",
        image=IMAGE,
        media_type="image/png",
        output_format=VisionExtraction,
    )
    return rec, result


def test_the_image_is_sent_as_a_base64_content_block():
    rec, _ = _call()
    content = rec.kwargs["messages"][0]["content"]

    image_block = content[0]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"
    assert image_block["source"]["data"] == base64.standard_b64encode(IMAGE).decode()


def test_the_image_block_precedes_the_text():
    """Anthropic's documented ordering for vision requests."""
    rec, _ = _call()
    content = rec.kwargs["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "text"]


def test_the_parsed_output_is_returned():
    _, result = _call()
    assert isinstance(result, VisionExtraction)
    assert result.content == "a,b\n1,2"


def test_the_vision_stage_routes_to_opus_with_its_own_budget():
    rec, _ = _call()
    assert rec.kwargs["model"] == "claude-opus-5"
    assert rec.kwargs["max_tokens"] == 8000


def test_thinking_is_sent_only_when_the_model_supports_it():
    """Haiku 4.5 returns 400 on `thinking`. The existing supports_thinking gate
    is the guard and must not be bypassed for vision."""
    from dataclasses import replace

    choice = model_for(Stage.VISION_EXTRACTION)
    rec, _ = _call(replace(choice, supports_thinking=False))
    assert "thinking" not in rec.kwargs

    rec, _ = _call(replace(choice, supports_thinking=True))
    assert rec.kwargs["thinking"] == {"type": "adaptive"}


def test_no_image_bytes_appear_in_the_text_prompt():
    """A base64 payload pasted into the text block would double the token bill
    and silently truncate the prompt."""
    rec, _ = _call()
    text_block = rec.kwargs["messages"][0]["content"][1]
    assert base64.standard_b64encode(IMAGE).decode() not in text_block["text"]
