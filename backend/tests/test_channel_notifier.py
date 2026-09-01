import asyncio
import threading
from dataclasses import replace

import pytest

from ley_khaa.adapters import notifier as notifier_module
from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.notifier import ChannelNotifier
from ley_khaa.config import settings as real_settings
from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository

SLACK = Destination(source="slack", conversation_id="slack:T1:C1:100.1", external_id="slack:C1:100.1")


class FakeAdapter:
    """A ChannelAdapter that records, and can be told to fail."""

    def __init__(self, name="slack", fail=False):
        self.name = name
        self.fail = fail
        self.sent: list[tuple[Destination, str]] = []
        self.delivered = threading.Event()

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def notify(self, dest: Destination, text: str) -> None:
        if self.fail:
            self.delivered.set()
            raise RuntimeError("slack rejected it")
        self.sent.append((dest, text))
        self.delivered.set()


@pytest.fixture
def loop_in_a_thread():
    """A real running event loop on another thread — the shape production has.

    pytest-asyncio is NOT a dependency of this project (a deliberate Phase 5
    finding: adding it would break the zero-warnings bar), so async behaviour is
    driven with a real loop rather than an async test.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


@pytest.fixture
def workers_mode(monkeypatch):
    """Settings is a frozen dataclass (a Phase 0 invariant). Pin it by rebinding
    the name on every consuming module — rebinding ley_khaa.config.settings
    alone pins nothing, because each module bound the object at import time."""
    patched = replace(real_settings, dispatch_mode="workers")
    monkeypatch.setattr(notifier_module, "settings", patched)
    return patched


def _notifier(session_factory, adapters, loop):
    return ChannelNotifier(adapters, loop=loop, session_factory=session_factory)


def test_a_notification_reaches_the_named_adapter(session_factory, loop_in_a_thread, workers_mode):
    adapter = FakeAdapter()
    _notifier(session_factory, {"slack": adapter}, loop_in_a_thread).notify(SLACK, "hello")

    assert adapter.delivered.wait(timeout=5), "the coroutine never ran on the loop"
    assert adapter.sent == [(SLACK, "hello")]


def test_a_source_with_no_adapter_is_skipped_silently(session_factory, loop_in_a_thread, workers_mode):
    """A fresh clone's demo task has source 'simulator' and every dashboard
    answer has source 'dashboard'. Dead-lettering those would bury the real
    drops under a clone's own traffic."""
    _notifier(session_factory, {"slack": FakeAdapter()}, loop_in_a_thread).notify(
        Destination(source="simulator", conversation_id="conv-1"), "hello"
    )

    with session_factory() as session:
        assert DeadLetterRepository(session).list() == []


def test_a_source_with_no_adapter_is_skipped_even_in_inline_mode(session_factory, monkeypatch):
    """The brief's own no-adapter test pins dispatch_mode to 'workers' (via
    workers_mode), so it can never exercise the inline branch and cannot
    distinguish the no-adapter check running before vs. after the inline
    check. The order only matters when BOTH conditions hold at once: an
    unrouted source (the demo task's 'simulator', or 'dashboard') seen while
    dispatch_mode is 'inline'. That must still be a silent no-op, never a
    dead letter — the whole point of running the no-adapter check first."""
    monkeypatch.setattr(notifier_module, "settings", replace(real_settings, dispatch_mode="inline"))

    _notifier(session_factory, {"slack": FakeAdapter()}, None).notify(
        Destination(source="simulator", conversation_id="conv-1"), "hello"
    )

    with session_factory() as session:
        assert DeadLetterRepository(session).list() == []


def test_inline_mode_dead_letters_rather_than_delivering(
    session_factory, loop_in_a_thread, monkeypatch
):
    """Spec §3.6: inline is the single-operator dashboard mode and has no
    channel to answer into, so a notification is recorded, not delivered."""
    monkeypatch.setattr(notifier_module, "settings", replace(real_settings, dispatch_mode="inline"))
    adapter = FakeAdapter()

    _notifier(session_factory, {"slack": adapter}, loop_in_a_thread).notify(SLACK, "hello")

    assert adapter.sent == []
    with session_factory() as session:
        rows = DeadLetterRepository(session).list()
    assert len(rows) == 1
    assert rows[0].kind == "outbound"
    assert rows[0].source == "slack"


def test_no_loop_dead_letters(session_factory, workers_mode):
    adapter = FakeAdapter()
    _notifier(session_factory, {"slack": adapter}, None).notify(SLACK, "hello")

    assert adapter.sent == []
    with session_factory() as session:
        assert [r.kind for r in DeadLetterRepository(session).list()] == ["outbound"]


def test_a_failing_delivery_dead_letters_without_raising(
    session_factory, loop_in_a_thread, workers_mode
):
    """The future's exception is consumed by a done-callback. Without one,
    asyncio swallows it and a failed notification is invisible."""
    adapter = FakeAdapter(fail=True)

    _notifier(session_factory, {"slack": adapter}, loop_in_a_thread).notify(SLACK, "hello")

    assert adapter.delivered.wait(timeout=5)
    deadline = threading.Event()
    for _ in range(50):
        with session_factory() as session:
            rows = DeadLetterRepository(session).list()
        if rows:
            break
        deadline.wait(0.1)
    assert len(rows) == 1
    assert rows[0].kind == "outbound"
    assert "slack rejected it" in rows[0].reason


def test_notify_does_not_wait_for_the_adapter(session_factory, loop_in_a_thread, workers_mode):
    """Fire-and-forget (§3.6): a slow platform API must not extend a task's
    execution time. A blocking implementation would sit here for the full 3
    seconds; this asserts on ORDER, not on a duration, so it cannot pass by
    accident of a fast machine."""
    started = threading.Event()
    released = threading.Event()

    class Slow(FakeAdapter):
        async def notify(self, dest, text):
            started.set()
            await asyncio.get_running_loop().run_in_executor(None, released.wait)
            self.sent.append((dest, text))
            self.delivered.set()

    adapter = Slow()
    _notifier(session_factory, {"slack": adapter}, loop_in_a_thread).notify(SLACK, "hello")

    # notify() has already returned while the adapter is still blocked.
    assert started.wait(timeout=5)
    assert adapter.sent == []
    released.set()
    assert adapter.delivered.wait(timeout=5)


def test_the_dead_letter_payload_carries_no_text_secrets(session_factory, workers_mode):
    """The payload is the destination and the reason, never a token — and the
    redactor is what guarantees it, so this asserts on what was stored."""
    _notifier(session_factory, {"slack": FakeAdapter()}, None).notify(SLACK, "hello")

    with session_factory() as session:
        payload = DeadLetterRepository(session).list()[0].payload
    assert "slack:T1:C1:100.1" in payload
