"""The background sweeper that gives the readiness gate a trigger in live use."""

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from ley_khaa.api import app as app_module
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.orm import MessageRow
from ley_khaa.persistence.repository import TaskRepository


def _run_sweeper_until(sweep, calls, target):
    """Run the real loop at a tiny interval until `calls` reaches `target`."""

    async def main():
        task = asyncio.create_task(app_module._periodic_sweeper(0.001, sweep))
        for _ in range(500):
            await asyncio.sleep(0.002)
            if len(calls) >= target:
                break
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return task

    return asyncio.run(main())


def test_sweeper_runs_repeatedly_on_its_interval():
    calls: list[int] = []
    _run_sweeper_until(lambda: (calls.append(1), 0)[1], calls, 3)
    assert len(calls) >= 3


def test_a_failing_sweep_does_not_kill_the_loop():
    calls: list[int] = []

    def sweep() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("one bad sweep")
        return 1

    _run_sweeper_until(sweep, calls, 3)
    assert len(calls) >= 3


def test_sweeper_is_cancelled_cleanly_on_shutdown():
    calls: list[int] = []
    task = _run_sweeper_until(lambda: (calls.append(1), 0)[1], calls, 1)
    assert task.cancelled()


def test_startup_does_not_launch_the_sweeper_when_disabled(client):
    # conftest sets LEY_KHAA_DISABLE_STARTUP=1, so the suite gets no background
    # tasks — this asserts that, rather than assuming it.
    from ley_khaa.api.app import app

    assert app.state.sweeper is None


def test_sweep_once_promotes_a_candidate_whose_conversation_went_quiet(session, monkeypatch):
    """The function the loop actually calls, on its own session."""
    monkeypatch.setattr(
        app_module,
        "SessionLocal",
        sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False),
    )
    orch = Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(debounce_seconds=600),
    )
    result = orch.ingest({"text": "compare the universes"})
    assert result.task_ids == []

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=700)
    session.query(MessageRow).update({"timestamp": cutoff})
    session.commit()

    assert app_module._sweep_once() == 1

    # The sweep ran on its own session; drop this one's cached copies before reading.
    session.expire_all()
    assert CandidateRepository(session).list_for_conversation("conv-1")[0].state == "promoted"
    assert len(TaskRepository(session).list()) == 1
