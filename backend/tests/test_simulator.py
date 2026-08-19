from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.intake.simulator import Simulator
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _sim(session):
    orch = Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(debounce_seconds=0),
    )
    return Simulator(orch)


def test_available_lists_the_golden_fixture(session):
    assert "messy_universe_check" in _sim(session).available()


def test_replay_ingests_every_message(session):
    _sim(session).replay("messy_universe_check")
    assert len(MessageRepository(session).list_for_conversation("conv-universe")) == 9


def test_replay_timestamps_are_in_the_past_and_ordered(session):
    _sim(session).replay("messy_universe_check")
    rows = MessageRepository(session).list_for_conversation("conv-universe")
    assert rows[0].timestamp < rows[-1].timestamp


def test_unknown_fixture_raises(session):
    import pytest

    with pytest.raises(FileNotFoundError):
        _sim(session).replay("no_such_conversation")


def test_messy_conversation_yields_a_task_that_excludes_the_chatter(session):
    """The headline integration test: noise in, one clean task out."""
    _sim(session).replay("messy_universe_check")
    tasks = TaskRepository(session).list()
    assert len(tasks) >= 1

    messages = {m.id: m.text for m in MessageRepository(session).list_for_conversation("conv-universe")}
    owned = [messages[mid] for mid in tasks[-1].source_message_ids]
    assert any("Bloomberg" in t for t in owned)
    assert not any("game last night" in t for t in owned)
    assert not any(t.strip().lower() == "thanks!" for t in owned)
