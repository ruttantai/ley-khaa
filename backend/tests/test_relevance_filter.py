from datetime import datetime, timezone

from ley_khaa.crystallizer.relevance import RelevanceFilter, RelevanceVerdict
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.orm import MessageRow


def _row(text="Can you compare the two universes?", author="boss"):
    return MessageRow(
        id="m1",
        external_id=None,
        source="simulator",
        client="demo",
        conversation_id="c1",
        author=author,
        text=text,
        attachments=[],
        timestamp=datetime.now(timezone.utc),
    )


def test_judge_returns_the_models_verdict():
    llm = FakeLLM([RelevanceVerdict(relevant=True, topic="universe-reconciliation", confidence=0.9)])
    verdict = RelevanceFilter(llm).judge(_row())
    assert verdict.relevant is True
    assert verdict.topic == "universe-reconciliation"


def test_judge_routes_to_the_cheap_model():
    llm = FakeLLM([RelevanceVerdict(relevant=False, topic="chatter", confidence=0.8)])
    RelevanceFilter(llm).judge(_row("lol same"))
    assert llm.calls[0].choice.model == "claude-haiku-4-5"
    assert llm.calls[0].choice.supports_thinking is False


def test_judge_includes_author_and_text_in_the_prompt():
    llm = FakeLLM([RelevanceVerdict(relevant=True, topic="t", confidence=0.5)])
    RelevanceFilter(llm).judge(_row("reconcile the holdings", author="alice"))
    user_prompt = llm.calls[0].user
    assert "alice" in user_prompt
    assert "reconcile the holdings" in user_prompt


def test_attachments_are_summarized_into_the_prompt():
    llm = FakeLLM([RelevanceVerdict(relevant=True, topic="t", confidence=0.5)])
    row = _row("see attached")
    row.attachments = [{"kind": "table", "name": "holdings.csv", "content": "a,b\n1,2"}]
    RelevanceFilter(llm).judge(row)
    assert "holdings.csv" in llm.calls[0].user
    assert "table" in llm.calls[0].user


def test_confidence_is_clamped_to_unit_range():
    # Pydantic bounds keep a hallucinated 4.2 out of downstream scoring.
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RelevanceVerdict(relevant=True, topic="t", confidence=4.2)
