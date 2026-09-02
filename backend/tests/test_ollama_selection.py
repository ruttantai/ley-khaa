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


def test_the_unreachable_daemon_warning_says_to_restart_after_fixing(monkeypatch, caplog):
    """C2: the resolved client is cached for the process's life, so following
    this warning's own advice mid-session (starting the daemon) does nothing
    until the backend restarts -- the warning must say so, or a user who
    fixes the cause keeps getting regex output with no further signal."""
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(raises=ConnectionError("refused"))
    )
    with caplog.at_level(logging.WARNING):
        factory.build_llm("ollama")
    assert "restart" in caplog.text.lower()


def test_the_unpulled_model_warning_says_to_restart_after_fixing(monkeypatch, caplog):
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(_Listing("llama3.1"))
    )
    with caplog.at_level(logging.WARNING):
        factory.build_llm("ollama")
    assert "restart" in caplog.text.lower()


def test_the_probe_client_has_a_short_timeout(monkeypatch):
    """Spec §3.4: "a cheap client.list() with a short timeout". ollama 0.6.2
    defaults to timeout=None, which on a host that DROPs packets to 11434
    (ufw's default) would block the startup path for ~127s. This must never
    reach the network — it inspects the kwargs the real ollama.Client() is
    constructed with."""
    calls = []

    class _RecordingClient:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("ollama.Client", _RecordingClient)
    factory._ollama_client("http://localhost:11434")
    assert calls[0]["timeout"] is not None
    assert 0 < calls[0]["timeout"] <= 30


def test_a_listing_entry_with_no_model_name_degrades_rather_than_raising(monkeypatch, caplog):
    """ollama._types.ListResponse.Model.model is Optional[str]. A listing
    entry lacking a name used to reach `name.startswith(...)` OUTSIDE the
    try/except that is supposed to make this probe crash-proof, raising
    AttributeError past build_llm entirely."""
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(_Listing("qwen2.5", None))
    )
    with caplog.at_level(logging.WARNING):
        llm = factory.build_llm("ollama")
    assert isinstance(llm, OllamaLLM)
    assert llm.name == "ollama:qwen2.5"


def test_a_listing_entry_with_only_a_none_name_still_falls_back_cleanly(monkeypatch, caplog):
    """The degenerate case of the above: NOTHING in the listing has a usable
    name, so this must fall back to the heuristic (not crash)."""
    monkeypatch.setattr(factory, "_ollama_client", lambda host: _Daemon(_Listing(None)))
    with caplog.at_level(logging.WARNING):
        llm = factory.build_llm("ollama")
    assert isinstance(llm, HeuristicLLM)


def test_a_tag_match_is_deterministic_when_multiple_tags_are_pulled(monkeypatch):
    """Several pulled tags can match one configured prefix at once — e.g. both
    qwen2.5:7b and qwen2.5:14b pulled under LEY_KHAA_OLLAMA_MODEL=qwen2.5.
    `pulled` is a set, and CPython randomizes string hashing per process, so a
    naive `next(...)` over it could adopt either tag arbitrarily from one
    process to the next -- flipping the manifest's provenance record and
    invalidating Phase 7's vision cache on every flip. The choice must be the
    same every time: the lexicographically first matching tag."""
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(_Listing("qwen2.5:14b", "qwen2.5:7b"))
    )
    seen = set()
    for _ in range(20):
        factory._ollama_client_cache = None
        llm = factory.build_llm("ollama")
        assert isinstance(llm, OllamaLLM)
        seen.add(llm.model)
    assert seen == {"qwen2.5:14b"}  # lexicographically first, not numerically largest


def test_an_exact_match_is_preferred_over_a_tagged_variant(monkeypatch):
    """model in pulled directly (an exact hit) must win over any qwen2.5:* tag
    match, regardless of iteration order."""
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(_Listing("qwen2.5:7b", "qwen2.5"))
    )
    llm = factory.build_llm("ollama")
    assert isinstance(llm, OllamaLLM)
    assert llm.model == "qwen2.5"


def test_a_tag_match_adopts_the_actually_pulled_tag(monkeypatch):
    """LEY_KHAA_OLLAMA_MODEL=qwen2.5 with only qwen2.5:7b pulled must build
    OllamaLLM(model="qwen2.5:7b") — the bare "qwen2.5" resolves to
    ":latest", which was never pulled, and every request would fail forever
    with the probe having passed and nothing logged."""
    monkeypatch.setattr(factory, "_ollama_client", lambda host: _Daemon(_Listing("qwen2.5:7b")))
    llm = factory.build_llm("ollama")
    assert isinstance(llm, OllamaLLM)
    assert llm.model == "qwen2.5:7b"
    assert llm.name == "ollama:qwen2.5:7b"
