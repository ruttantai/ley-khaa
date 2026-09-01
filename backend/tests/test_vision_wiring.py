from dataclasses import replace

from ley_khaa.api import app as app_module
from ley_khaa.config import settings as real_settings
from ley_khaa.crystallizer.gate import ReadinessGate
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
    """
    from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository

    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)
    extractor = app_module.build_vision_extractor(session)
    extractor.dead_letter(source="vision", kind="inbound", reason="refused", payload={"name": "a.png"})

    rows = DeadLetterRepository(session).list()
    assert len(rows) == 1 and rows[0].source == "vision"


def test_a_mixed_case_configured_host_still_matches(session, monkeypatch):
    """channel_set() strips whitespace but does not lowercase, and urlparse's
    .hostname ALWAYS lowercases. An operator writing `Files.Slack.Com` must
    still get a working allowlist entry, not a silent, unexplained refusal."""
    monkeypatch.setattr(
        app_module, "settings", replace(real_settings, image_hosts="Files.Slack.Com")
    )
    extractor = app_module.build_vision_extractor(session)
    assert "files.slack.com" in extractor.fetcher.allowed_hosts
