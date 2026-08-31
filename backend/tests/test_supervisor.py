import asyncio

import pytest

from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.supervisor import AdapterSupervisor
from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository


class Adapter:
    """Counts starts. `crashes` is how many of them blow up before it settles."""

    def __init__(self, name="slack", crashes=0):
        self.name = name
        self.crashes = crashes
        self.starts = 0
        self.stops = 0
        self.settled = asyncio.Event()

    async def start(self) -> None:
        self.starts += 1
        if self.starts <= self.crashes:
            raise RuntimeError(f"{self.name} socket closed")
        self.settled.set()
        # A real adapter blocks here for the life of the process.
        await asyncio.sleep(3600)

    async def stop(self) -> None:
        self.stops += 1

    async def notify(self, dest: Destination, text: str) -> None: ...


def _supervisor(session_factory, adapters):
    # A tiny backoff so the tests do not sleep. The GROWTH is asserted
    # separately, on the computed delays, rather than by timing anything.
    return AdapterSupervisor(
        adapters, session_factory=session_factory, base_backoff=0.001, max_backoff=0.01
    )


def _run(coro):
    return asyncio.run(coro)


def test_every_adapter_is_started(session_factory):
    slack, discord = Adapter("slack"), Adapter("discord")

    async def scenario():
        supervisor = _supervisor(session_factory, [slack, discord])
        await supervisor.start()
        await asyncio.wait_for(
            asyncio.gather(slack.settled.wait(), discord.settled.wait()), timeout=5
        )
        await supervisor.stop()

    _run(scenario())
    assert (slack.starts, discord.starts) == (1, 1)


def test_a_crashing_adapter_is_restarted(session_factory):
    slack = Adapter("slack", crashes=2)

    async def scenario():
        supervisor = _supervisor(session_factory, [slack])
        await supervisor.start()
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    assert slack.starts == 3, "two crashes, then a start that stuck"


def test_a_crashing_adapter_does_not_take_down_its_neighbour(session_factory):
    """The whole point of decision #2's cost being acknowledged: adapters run
    in-process beside the dispatcher, so one must never poison the others."""
    slack = Adapter("slack", crashes=3)
    discord = Adapter("discord")

    async def scenario():
        supervisor = _supervisor(session_factory, [slack, discord])
        await supervisor.start()
        await asyncio.wait_for(discord.settled.wait(), timeout=5)
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    assert discord.starts == 1
    assert slack.starts == 4


def test_a_crash_is_dead_lettered(session_factory):
    slack = Adapter("slack", crashes=1)

    async def scenario():
        supervisor = _supervisor(session_factory, [slack])
        await supervisor.start()
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    with session_factory() as session:
        rows = DeadLetterRepository(session).list()
    assert [r.kind for r in rows] == ["connection"]
    assert rows[0].source == "slack"
    assert "socket closed" in rows[0].reason


def test_the_backoff_grows_and_is_capped(session_factory):
    """Asserted on the computed delays, not by timing a sleep — a duration
    assertion would pass or fail on how busy the machine is."""
    supervisor = _supervisor(session_factory, [])
    delays = []
    delay = supervisor.base_backoff
    for _ in range(8):
        delays.append(delay)
        delay = supervisor.next_backoff(delay)

    assert delays[0] == 0.001
    assert delays[1] == 0.002
    assert delays[2] == 0.004
    assert delays[-1] == supervisor.max_backoff
    assert all(b >= a for a, b in zip(delays, delays[1:])), "backoff must never shrink"


def test_stop_stops_every_adapter(session_factory):
    slack, discord = Adapter("slack"), Adapter("discord")

    async def scenario():
        supervisor = _supervisor(session_factory, [slack, discord])
        await supervisor.start()
        await asyncio.wait_for(
            asyncio.gather(slack.settled.wait(), discord.settled.wait()), timeout=5
        )
        await supervisor.stop()

    _run(scenario())
    assert (slack.stops, discord.stops) == (1, 1)


def test_stop_is_safe_when_nothing_started(session_factory):
    _run(_supervisor(session_factory, []).stop())


def test_the_registry_is_keyed_by_adapter_name(session_factory):
    """ChannelNotifier routes on dest.source, which is exactly this key."""
    supervisor = _supervisor(session_factory, [Adapter("slack"), Adapter("discord")])
    assert set(supervisor.registry) == {"slack", "discord"}


def test_the_loop_is_captured_at_start(session_factory):
    """ChannelNotifier needs it to hand coroutines across from a worker
    thread, and it must be the loop the adapters actually run on."""
    slack = Adapter("slack")
    captured = {}

    async def scenario():
        supervisor = _supervisor(session_factory, [slack])
        assert supervisor.loop is None
        await supervisor.start()
        captured["loop"] = supervisor.loop
        captured["running"] = asyncio.get_running_loop()
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    assert captured["loop"] is captured["running"]


def test_cancellation_is_not_treated_as_a_crash(session_factory):
    """Shutdown cancels the supervised tasks. If CancelledError were caught as
    a failure, every clean shutdown would dead-letter and try to restart."""
    slack = Adapter("slack")

    async def scenario():
        supervisor = _supervisor(session_factory, [slack])
        await supervisor.start()
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    with session_factory() as session:
        assert DeadLetterRepository(session).list() == []
    assert slack.starts == 1, "a cancelled adapter must not be restarted"
