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


def test_messy_conversation_yields_one_clean_task(session):
    """The headline integration test: noise in, one clean task out.

    replay() ingests the whole conversation with promotion suppressed (see
    Simulator.replay) and only lets the gate decide once, via a single sweep()
    after every message is in. That's what keeps the two halves of the same
    request — "compare Bloomberg against FactSet" and, three messages later,
    "month end, as an Excel file" — together as ONE candidate instead of being
    torn into two half-specified tasks by a gate that fired mid-conversation.
    """
    _sim(session).replay("messy_universe_check")
    tasks = TaskRepository(session).list()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.spec["operation"] == "set_difference"
    assert task.spec["output_format"] == "xlsx"
    assert task.spec["missing_fields"] == []

    messages = {m.id: m.text for m in MessageRepository(session).list_for_conversation("conv-universe")}
    owned = [messages[mid] for mid in task.source_message_ids]
    assert any("Bloomberg" in t for t in owned)
    assert any("what's missing as an Excel file" in t for t in owned)
    assert not any("game last night" in t for t in owned)
    assert not any(t.strip().lower() == "thanks!" for t in owned)
    assert not any(t.strip().lower() == "morning all" for t in owned)
