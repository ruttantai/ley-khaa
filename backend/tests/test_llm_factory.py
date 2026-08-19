import logging

from ley_khaa.llm.factory import build_llm
from ley_khaa.llm.heuristic import HeuristicLLM


def test_explicit_heuristic_backend_is_not_a_fallback(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="ley_khaa.llm.factory"):
        client = build_llm("heuristic")
    assert isinstance(client, HeuristicLLM)
    assert caplog.records == []  # asked for it explicitly: nothing to warn about


def test_missing_api_key_warns_about_the_offline_stand_in(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger="ley_khaa.llm.factory"):
        client = build_llm("anthropic")
    assert isinstance(client, HeuristicLLM)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "ANTHROPIC_API_KEY" in message
    assert "HeuristicLLM" in message


def test_a_present_api_key_does_not_warn(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    sentinel = object()
    # Never construct the real client in tests: it would reach for the network.
    monkeypatch.setattr("ley_khaa.llm.factory.AnthropicLLM", lambda: sentinel)
    with caplog.at_level(logging.WARNING, logger="ley_khaa.llm.factory"):
        assert build_llm("anthropic") is sentinel
    assert caplog.records == []
