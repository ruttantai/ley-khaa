import pytest
from pydantic import BaseModel

from ley_khaa.llm.ollama import OllamaLLM
from ley_khaa.llm.router import Stage, model_for
from ley_khaa.vision.contract import VisionExtraction


class Answer(BaseModel):
    verdict: str
    score: float


class _Recorder:
    """Stands in for ollama.Client. Records the kwargs of every chat() call."""

    def __init__(self, content='{"verdict": "yes", "score": 0.5}'):
        self.calls: list[dict] = []
        self._content = content

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return type(
            "R", (), {"message": type("M", (), {"content": self._content})()}
        )()


def _llm(rec, model="qwen2.5"):
    return OllamaLLM(model=model, client=rec)


def test_the_output_schema_is_sent_as_the_format():
    """This is what makes one implementation cover every stage: the caller's
    pydantic model becomes the JSON schema Ollama is constrained to."""
    rec = _Recorder()
    _llm(rec).parse(
        choice=model_for(Stage.RELEVANCE_FILTER), system="s", user="u", output_format=Answer
    )
    assert rec.calls[0]["format"] == Answer.model_json_schema()


def test_the_response_is_validated_into_the_requested_type():
    rec = _Recorder()
    out = _llm(rec).parse(
        choice=model_for(Stage.RELEVANCE_FILTER), system="s", user="u", output_format=Answer
    )
    assert isinstance(out, Answer)
    assert out.verdict == "yes"
    assert out.score == 0.5


def test_the_routers_token_budget_is_honoured():
    """The router's max_tokens still means something locally: synthesis needs
    room for a whole program."""
    rec = _Recorder()
    choice = model_for(Stage.SYNTHESIS)
    _llm(rec).parse(choice=choice, system="s", user="u", output_format=Answer)
    assert rec.calls[0]["options"]["num_predict"] == choice.max_tokens
    assert choice.max_tokens == 16000


def test_the_routers_claude_model_id_is_ignored():
    """The local model comes from config; a Claude id must never be sent to a
    local daemon that has never heard of it."""
    rec = _Recorder()
    choice = model_for(Stage.INTERPRETER)
    _llm(rec, model="llama3.1").parse(
        choice=choice, system="s", user="u", output_format=Answer
    )
    assert rec.calls[0]["model"] == "llama3.1"
    assert "claude" not in rec.calls[0]["model"]


def test_the_system_and_user_prompts_are_separate_messages():
    rec = _Recorder()
    _llm(rec).parse(
        choice=model_for(Stage.INTERPRETER), system="be terse", user="the ask", output_format=Answer
    )
    assert rec.calls[0]["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "the ask"},
    ]


def test_the_name_records_the_actual_local_model():
    """`ollama` alone does not identify what produced a script."""
    assert _llm(_Recorder(), model="qwen2.5").name == "ollama:qwen2.5"
    assert _llm(_Recorder(), model="llama3.1").name == "ollama:llama3.1"


def test_an_empty_response_is_a_clear_error_not_a_confusing_one():
    """ChatResponse.message.content is Optional[str]. Passing None into
    model_validate_json raises an opaque TypeError; the same None-return shape
    was a Critical in phase 7."""
    rec = _Recorder(content=None)
    with pytest.raises(ValueError, match="empty response"):
        _llm(rec).parse(
            choice=model_for(Stage.RELEVANCE_FILTER), system="s", user="u", output_format=Answer
        )


def test_an_image_is_carried_not_read():
    """Ollama is text-only in v1 (spec §11). This returns the same shape as the
    heuristic stand-in, which phase 7's VisionExtractor already stores as a
    carried-not-read record — so no new degradation path is needed."""
    rec = _Recorder()
    out = _llm(rec).extract_image(
        choice=model_for(Stage.VISION_EXTRACTION),
        system="s",
        user="chart.png",
        image=b"\x89PNG_bytes",
        media_type="image/png",
        output_format=VisionExtraction,
    )
    assert out.content == ""
    assert out.kind == "text"
    assert "chart.png" in out.summary
    assert rec.calls == []  # no model call at all
