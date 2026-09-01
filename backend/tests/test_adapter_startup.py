import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ley_khaa.adapters import notifier as notifier_module
from ley_khaa.adapters import supervisor as supervisor_module
from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.notifier import NullNotifier, current_notifier
from ley_khaa.adapters.supervisor import build_adapters
from ley_khaa.api import app as app_module
from ley_khaa.config import settings as real_settings

NOOP = {"ingest": lambda raw: None, "dead_letter": lambda **kw: None}


def _pin(monkeypatch, **fields):
    """Settings is frozen (Phase 0 invariant). Pin it on every module that
    bound the object at import time."""
    patched = replace(real_settings, **fields)
    monkeypatch.setattr(supervisor_module, "settings", patched)
    monkeypatch.setattr(notifier_module, "settings", patched)
    monkeypatch.setattr(app_module, "settings", patched)
    return patched


def test_no_tokens_means_no_adapters(monkeypatch):
    """The whole zero-account demo rests on this line."""
    _pin(monkeypatch, slack_bot_token="", slack_app_token="", discord_bot_token="")
    assert build_adapters(**NOOP) == []


def test_a_half_configured_slack_does_not_start(monkeypatch):
    """Both tokens or nothing (spec §5): a bot token with no app token cannot
    open a Socket Mode connection, and starting anyway would crash-loop."""
    _pin(monkeypatch, slack_bot_token="xoxb-x", slack_app_token="", discord_bot_token="")
    assert build_adapters(**NOOP) == []


def test_slack_starts_when_both_tokens_are_present(monkeypatch):
    _pin(
        monkeypatch,
        slack_bot_token="xoxb-x",
        slack_app_token="xapp-x",
        slack_channels="C1, C2",
        discord_bot_token="",
    )
    adapters = build_adapters(**NOOP)
    assert [a.name for a in adapters] == ["slack"]
    assert adapters[0].allowed_channels == frozenset({"C1", "C2"})


def test_discord_starts_on_its_token_alone(monkeypatch):
    _pin(monkeypatch, slack_bot_token="", discord_bot_token="d", discord_channels="9")
    adapters = build_adapters(**NOOP)
    assert [a.name for a in adapters] == ["discord"]
    assert adapters[0].allowed_channels == frozenset({"9"})


def test_an_adapter_with_an_empty_allowlist_still_starts(monkeypatch):
    """Spec §5: it starts and ingests nothing, logging that plainly — the safe
    reading of an incomplete configuration."""
    _pin(monkeypatch, slack_bot_token="xoxb-x", slack_app_token="xapp-x", slack_channels="")
    adapters = build_adapters(**NOOP)
    assert [a.name for a in adapters] == ["slack"]
    assert adapters[0].allowed_channels == frozenset()


def test_a_token_free_startup_leaves_the_notifier_null(monkeypatch, session):
    """A fresh clone behaves precisely as it did before this phase."""
    _pin(monkeypatch, disable_startup=False, dispatch_mode="inline",
         slack_bot_token="", discord_bot_token="")
    monkeypatch.setattr(app_module, "run_migrations", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)

    with TestClient(app_module.app) as client:
        assert client.get("/health").status_code == 200
        assert app_module.app.state.supervisor is None
        assert isinstance(current_notifier(), NullNotifier)

    assert isinstance(current_notifier(), NullNotifier)


def test_a_configured_startup_runs_a_supervisor_and_installs_a_channel_notifier(
    monkeypatch, session
):
    started = []

    class StubAdapter:
        name = "slack"

        async def start(self):
            started.append(1)
            await asyncio.Event().wait()

        async def stop(self): ...

        async def notify(self, dest: Destination, text: str): ...

    _pin(monkeypatch, disable_startup=False, dispatch_mode="inline")
    monkeypatch.setattr(app_module, "run_migrations", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(app_module, "build_adapters", lambda **kw: [StubAdapter()])

    with TestClient(app_module.app) as client:
        assert client.get("/health").status_code == 200
        supervisor = app_module.app.state.supervisor
        assert supervisor is not None
        assert set(supervisor.registry) == {"slack"}
        assert current_notifier().name == "channel"

    # Shutdown must put it back, or a later test in the same process inherits a
    # notifier pointing at a dead loop.
    assert isinstance(current_notifier(), NullNotifier)
    assert app_module.app.state.supervisor is None


def test_the_ingest_callable_reaches_the_orchestrator(monkeypatch, session):
    """What build_adapters hands an adapter must actually create work, or the
    whole chain is wired to nothing."""
    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)

    app_module._ingest_from_channel(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": "slack:T1:C1:1.0",
            "external_id": "slack:C1:1.0",
            "text": "compare the Bloomberg universe against FactSet as an Excel file",
        }
    )

    from ley_khaa.persistence.repository import TaskRepository

    assert TaskRepository(session).list()


def test_the_dead_letter_callable_writes_a_row(monkeypatch, session):
    from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository

    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)
    app_module._record_dead_letter(
        source="slack", kind="inbound", reason="bad payload", payload={"token": "x"}
    )

    rows = DeadLetterRepository(session).list()
    assert len(rows) == 1
    assert "x" not in rows[0].payload
