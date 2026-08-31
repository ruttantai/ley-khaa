import asyncio
import dataclasses
import inspect

import pytest

from ley_khaa.adapters.base import ChannelAdapter, Destination, channel_set
from ley_khaa.intake.simulator import Simulator


def test_a_destination_is_hashable():
    """It is used as a log key and passed across a thread boundary, so it must
    be usable in a set or dict."""
    dest = Destination(source="slack", conversation_id="slack:T:C:1.0", external_id="slack:C:1.0")
    assert hash(dest)
    assert {dest, dest} == {dest}


def test_a_destination_is_frozen():
    """Separate from hashability on purpose: a dataclass can be hashable and
    still mutable (eq=False gives an identity hash), so one mutation that kills
    the hashability assertion would otherwise leave this property unguarded."""
    dest = Destination(source="slack", conversation_id="slack:T:C:1.0", external_id="slack:C:1.0")
    with pytest.raises(dataclasses.FrozenInstanceError):
        dest.source = "discord"  # type: ignore[misc]


def test_channel_set_parses_a_comma_separated_allowlist():
    assert channel_set("C1, C2 ,C3") == frozenset({"C1", "C2", "C3"})


def test_channel_set_of_nothing_is_empty_not_permissive():
    """An empty allowlist must mean 'ingest nothing', never 'ingest
    everything'. Decision #4: being invited to a channel is not consent."""
    assert channel_set("") == frozenset()
    assert channel_set("  ,, ") == frozenset()


def test_the_simulator_satisfies_the_adapter_protocol():
    sim = Simulator(orchestrator=None)
    assert isinstance(sim, ChannelAdapter)
    assert sim.name == "simulator"


def test_the_simulator_start_stop_and_notify_are_awaitable_no_ops():
    """It has no socket to open and no channel to answer into. The point of the
    retrofit is that the protocol has three implementations, not that the
    simulator gains a network."""
    sim = Simulator(orchestrator=None)
    for method in (sim.start, sim.stop):
        assert inspect.iscoroutinefunction(method)

    asyncio.run(sim.start())
    asyncio.run(sim.notify(Destination(source="simulator", conversation_id="c", external_id=None), "hi"))
    asyncio.run(sim.stop())


def test_the_simulator_still_replays(session):
    """The retrofit must not disturb the behaviour every existing simulator
    test depends on — asserted here too so a reviewer sees the guarantee in the
    task that made the change."""
    from ley_khaa.crystallizer.gate import ReadinessGate
    from ley_khaa.llm.heuristic import HeuristicLLM
    from ley_khaa.orchestrator.orchestrator import Orchestrator
    from ley_khaa.persistence.candidate_repository import CandidateRepository
    from ley_khaa.persistence.message_repository import MessageRepository
    from ley_khaa.persistence.repository import TaskRepository

    orchestrator = Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
    )
    results = Simulator(orchestrator).replay("messy_universe_check")
    assert results
