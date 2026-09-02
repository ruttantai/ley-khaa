import logging

import ollama
import pytest

from ley_khaa.llm import factory
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.llm.ollama import OllamaLLM


class _Listing:
    def __init__(self, *names):
        self.models = [type("M", (), {"model": n})() for n in names]


class _Daemon:
    """Stands in for ollama.Client at probe time."""

    def __init__(self, listing=None, raises=None):
        self._listing = listing
        self._raises = raises
        self.list_calls = 0

    def list(self):
        self.list_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._listing


@pytest.fixture(autouse=True)
def _reset_warning():
    factory._warned_about_fallback = False
    factory._ollama_client_cache = None
    yield
    factory._warned_about_fallback = False
    factory._ollama_client_cache = None


def test_a_reachable_daemon_with_the_model_pulled_gives_an_ollama_client(monkeypatch):
    monkeypatch.setattr(factory, "_ollama_client", lambda host: _Daemon(_Listing("qwen2.5")))
    llm = factory.build_llm("ollama")
    assert isinstance(llm, OllamaLLM)
    assert llm.name == "ollama:qwen2.5"


def test_an_unreachable_daemon_falls_back_to_the_heuristic_loudly(monkeypatch, caplog):
    """A dead daemon raises builtins.ConnectionError — NOT an ollama.* type."""
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(raises=ConnectionError("refused"))
    )
    with caplog.at_level(logging.WARNING):
        llm = factory.build_llm("ollama")
    assert isinstance(llm, HeuristicLLM)
    assert llm.name == "heuristic"
    assert "not reachable" in caplog.text
    assert "11434" in caplog.text or "host" in caplog.text.lower()


def test_a_model_that_is_not_pulled_says_how_to_pull_it(monkeypatch, caplog):
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(_Listing("llama3.1", "mistral"))
    )
    with caplog.at_level(logging.WARNING):
        llm = factory.build_llm("ollama")
    assert isinstance(llm, HeuristicLLM)
    assert "ollama pull qwen2.5" in caplog.text


def test_an_ollama_response_error_also_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(
        factory,
        "_ollama_client",
        lambda host: _Daemon(raises=ollama.ResponseError("boom")),
    )
    with caplog.at_level(logging.WARNING):
        assert isinstance(factory.build_llm("ollama"), HeuristicLLM)


def test_an_unexpected_exception_type_also_degrades_rather_than_escaping(monkeypatch, caplog):
    """The probe's job is "decide, never crash deciding" — any exception, not
    just the ones seen so far, must degrade to the heuristic."""
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(raises=RuntimeError("kaboom"))
    )
    with caplog.at_level(logging.WARNING):
        llm = factory.build_llm("ollama")
    assert isinstance(llm, HeuristicLLM)
    assert llm.name == "heuristic"
    assert "RuntimeError" in caplog.text


def test_the_fallback_warning_is_said_once_not_every_sweep(monkeypatch, caplog):
    """build_llm runs per request and per background sweep, but the probe — a
    live network round-trip — must run at most once per process, with later
    calls reusing the cached decision rather than re-probing the daemon."""
    calls = []

    def _client(host):
        calls.append(host)
        return _Daemon(raises=ConnectionError("refused"))

    monkeypatch.setattr(factory, "_ollama_client", _client)
    with caplog.at_level(logging.WARNING):
        factory.build_llm("ollama")
        factory.build_llm("ollama")
        factory.build_llm("ollama")
    assert caplog.text.count("not reachable") == 1
    assert len(calls) == 1


def test_a_successful_probe_is_cached_and_reused(monkeypatch):
    """The resolved OllamaLLM is the same object across calls — build_llm does
    not re-probe or re-construct a client for a decision already made."""
    calls = []

    def _client(host):
        calls.append(host)
        return _Daemon(_Listing("qwen2.5"))

    monkeypatch.setattr(factory, "_ollama_client", _client)
    first = factory.build_llm("ollama")
    second = factory.build_llm("ollama")
    assert first is second
    assert len(calls) == 1


def test_the_other_backends_are_untouched(monkeypatch):
    assert isinstance(factory.build_llm("heuristic"), HeuristicLLM)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(factory.build_llm("anthropic"), HeuristicLLM)
