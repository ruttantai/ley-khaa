from dataclasses import replace

from ley_khaa.api import app as app_module
from ley_khaa.config import settings as real_settings
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _driver(session, extractor=None):
    return TaskDriver(
        TaskRepository(session), llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        extractor=extractor,
    )


def test_the_driver_hands_the_extractor_to_the_interpreter(session):
    sentinel = object()
    assert _driver(session, sentinel).interpreter.extractor is sentinel


def test_the_driver_hands_the_extractor_to_the_executor(session):
    """Both consumers, or an image is understood but never computed on."""
    sentinel = object()
    assert _driver(session, sentinel).executor.extractor is sentinel


def test_a_driver_with_no_extractor_still_works(session):
    driver = _driver(session)
    assert driver.interpreter.extractor is None
    assert driver.executor.extractor is None


def test_build_vision_extractor_wires_a_real_fetcher(session):
    extractor = app_module.build_vision_extractor(session)

    assert extractor.fetcher is not None
    assert "files.slack.com" in extractor.fetcher.allowed_hosts
    assert extractor.fetcher.max_bytes == real_settings.image_max_bytes
    # The extraction repository must be built on the SAME session this call
    # was given, not a fresh one of its own — otherwise every extraction row
    # lands in a session the caller never commits or rolls back, and every
    # build_orchestrator call leaks an unclosed connection.
    assert extractor.extractions.session is session


def test_build_vision_extractor_reads_the_configured_max_bytes(session, monkeypatch):
    """max_bytes has to come from settings, not a hardcoded default — the
    default and the configured value coincide under test, so a test that only
    checks against real_settings.image_max_bytes cannot tell a hardcoded
    5*1024*1024 apart from a real setting read."""
    monkeypatch.setattr(app_module, "settings", replace(real_settings, image_max_bytes=12345))
    extractor = app_module.build_vision_extractor(session)
    assert extractor.fetcher.max_bytes == 12345


def test_build_vision_extractor_reads_the_configured_llm_backend(session, monkeypatch):
    """build_llm must be called with settings.llm_backend, not a hardcoded
    backend — conftest.py sets LEY_KHAA_LLM=heuristic globally, so any test
    that merely checks the resulting LLM's TYPE cannot tell a hardcoded
    'heuristic' apart from a real settings read."""
    recorded = []

    def _fake_build_llm(backend):
        recorded.append(backend)
        return HeuristicLLM()

    monkeypatch.setattr(app_module, "build_llm", _fake_build_llm)
    monkeypatch.setattr(app_module, "settings", replace(real_settings, llm_backend="distinctive-backend"))
    app_module.build_vision_extractor(session)
    assert recorded == ["distinctive-backend"]


def test_build_vision_extractor_respects_the_off_switch(session, monkeypatch):
    monkeypatch.setattr(app_module, "settings", replace(real_settings, vision_enabled=False))
    assert app_module.build_vision_extractor(session).enabled is False


def test_the_fetcher_is_given_the_slack_token_from_settings(session, monkeypatch):
    monkeypatch.setattr(
        app_module, "settings", replace(real_settings, slack_bot_token="xoxb-wired")
    )
    extractor = app_module.build_vision_extractor(session)
    assert extractor.fetcher._slack_token == "xoxb-wired"


def test_build_orchestrator_gives_its_driver_an_extractor(session):
    """The line that connects all of the above to the running application. It
    was mutated in phase 6's review with the whole suite staying green."""
    driver = app_module.build_orchestrator(session).driver
    assert driver.interpreter.extractor is not None
    assert driver.executor.extractor is not None


def test_the_extractor_dead_letters_through_the_repository(session, monkeypatch):
    """A refused fetch has to become a visible dead letter, not a log line.

    _record_dead_letter opens its OWN session (real SessionLocal) rather than
    reusing the caller's, by design (see build_vision_extractor's docstring).
    Pinned to the test's session the same way test_adapter_startup.py does,
    so this stays hermetic instead of depending on a real Postgres being up.

    Drives the REAL failure path -- extractor.extract() on a non-allowlisted
    host, through VisionExtractor._record_drop -- rather than calling
    extractor.dead_letter(...) with hand-typed keywords. This is the ONLY
    visibility surface for a dropped image, and typing _record_drop's
    keyword shape by hand would let a real mismatch there pass silently:
    _record_drop's own `except Exception` swallows the TypeError a keyword
    typo would raise, so only actually calling it proves the wiring works.
    """
    from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository

    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)
    extractor = app_module.build_vision_extractor(session)
    extractor.extract(
        {"kind": "image", "name": "a.png", "content": "https://not-allowlisted.example/a.png"}
    )

    rows = DeadLetterRepository(session).list()
    assert len(rows) == 1 and rows[0].source == "vision"


def test_a_mixed_case_configured_host_still_matches(session, monkeypatch):
    """channel_set() strips whitespace but does not lowercase, and urlparse's
    .hostname ALWAYS lowercases. An operator writing `Files.Example.Com` must
    still get a working allowlist entry, not a silent, unexplained refusal.

    Deliberately a host ABSENT from the built-in default (files.slack.com IS
    in the default, so asserting membership alone cannot tell a real
    settings.image_hosts read apart from a hardcoded default string). The
    equality assertion below pins the setting read and the lowercasing
    together: it fails if either the read or the .lower() is dropped.
    """
    monkeypatch.setattr(
        app_module, "settings", replace(real_settings, image_hosts="Files.Example.Com")
    )
    extractor = app_module.build_vision_extractor(session)
    assert extractor.fetcher.allowed_hosts == frozenset({"files.example.com"})
