from dataclasses import replace

from fastapi.testclient import TestClient

from ley_khaa.config import settings as real_settings
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.repository import TaskRepository


def _pin(monkeypatch, mode: str) -> None:
    """Swap the settings object on every module that consumes it, not the
    shared global in ley_khaa.config.

    driver.py and api/app.py both did `from ..config import settings`, which
    binds the object at import time. Rebinding `ley_khaa.config.settings`
    afterwards hits a name nothing in either module reads, so the mode switch
    would silently never take effect — the same bug Task 6 shipped and a
    reviewer caught by running a test with a different env var. Patching each
    consuming module's own `settings` name is the only pin that is actually
    observed.
    """
    patched = replace(real_settings, dispatch_mode=mode)
    import ley_khaa.api.app as app_module
    import ley_khaa.orchestrator.driver as driver_module

    monkeypatch.setattr(driver_module, "settings", patched)
    monkeypatch.setattr(app_module, "settings", patched)


def test_inline_mode_drives_the_task_on_the_calling_thread(session, stub_execution, monkeypatch):
    from ley_khaa.api.app import build_orchestrator
    from ley_khaa.projects.seeds import ensure_default_project

    _pin(monkeypatch, "inline")
    ensure_default_project(session)
    orchestrator = build_orchestrator(session)
    orchestrator.ingest(
        {"text": "compare the bloomberg universe against the factset universe, csv"}
    )
    orchestrator.sweep()
    states = {TaskState(t.state) for t in orchestrator.repo.list()}

    assert states <= {TaskState.DONE, TaskState.AWAITING_APPROVAL, TaskState.NEEDS_CLARIFICATION}
    assert TaskState.RECEIVED not in states


def test_workers_mode_returns_before_the_task_runs(session, stub_execution, monkeypatch):
    """The visible win of this phase: intake stops blocking through a sandbox run."""
    from ley_khaa.api.app import build_orchestrator
    from ley_khaa.projects.seeds import ensure_default_project

    _pin(monkeypatch, "workers")
    ensure_default_project(session)
    orchestrator = build_orchestrator(session)
    orchestrator.ingest(
        {"text": "compare the bloomberg universe against the factset universe, csv"}
    )
    orchestrator.sweep()
    tasks = orchestrator.repo.list()

    assert tasks, "the candidate never became a task"
    # Created and left runnable — not driven.
    assert {TaskState(t.state) for t in tasks} == {TaskState.RECEIVED}
    assert TaskRepository(session).runnable_projects() == ["default"]


def test_workers_mode_leaves_an_approved_task_for_the_dispatcher(
    session, stub_execution, monkeypatch
):
    from ley_khaa.orchestrator.driver import TaskDriver
    from ley_khaa.persistence.candidate_repository import CandidateRepository
    from ley_khaa.persistence.message_repository import MessageRepository
    from ley_khaa.llm.heuristic import HeuristicLLM

    repo = TaskRepository(session)
    row = repo.create(project="acme", title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    repo.claim(row.id, expected=TaskState.INTERPRETED, target=TaskState.AWAITING_APPROVAL)

    driver = TaskDriver(
        repo,
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
    )
    _pin(monkeypatch, "workers")
    result = driver.approve(row.id)

    # Approval performed its own transition and stopped. It did NOT run the task.
    assert TaskState(result.state) is TaskState.EXECUTING


def test_the_sweeper_does_not_drive_stalled_tasks_in_workers_mode(session, monkeypatch):
    """Otherwise the sweeper drives tasks with no lease, in parallel with the
    dispatcher — exactly the double-lane bug the lease exists to prevent."""
    from ley_khaa.api.app import _sweep_once
    from ley_khaa.orchestrator.orchestrator import Orchestrator

    called: list[str] = []
    monkeypatch.setattr(
        Orchestrator, "advance_stalled", lambda self: called.append("advanced") or []
    )

    import ley_khaa.api.app as app_module

    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)

    _pin(monkeypatch, "workers")
    _sweep_once()
    assert called == []

    # Prove the empty list above is the guard, not a sweep that never ran at
    # all: with the exact same session and mock, inline mode DOES reach
    # advance_stalled. A test that only ever asserted `called == []` would
    # still pass if _sweep_once silently did nothing.
    _pin(monkeypatch, "inline")
    _sweep_once()
    assert called == ["advanced"]


def test_workers_mode_actually_starts_the_dispatcher(session, monkeypatch):
    """The phase's headline feature, at the one place it is wired up.

    conftest pins LEY_KHAA_DISPATCH=inline and every dispatcher test builds its
    own Dispatcher and calls tick() directly, so `lifespan`'s "if workers: start
    the dispatcher" branch was covered by nothing at all: deleting it left the
    whole suite green while the default configuration silently ran no tasks.

    Startup is disabled suite-wide (conftest), so it is re-enabled here with its
    heavy half — migrations, seeding, the demo replay — stubbed out, leaving the
    dispatch-mode branch itself as the thing under test.
    """
    import asyncio
    import time

    import ley_khaa.api.app as app_module
    from ley_khaa.orchestrator.dispatcher import Dispatcher

    ticks: list[int] = []

    async def _recording_tick(self) -> list[str]:
        ticks.append(1)
        # Nothing to do, and this must not keep the loop busy: run_forever
        # sleeps for sweep_interval_seconds after each tick.
        await asyncio.sleep(0)
        return []

    monkeypatch.setattr(Dispatcher, "tick", _recording_tick)
    monkeypatch.setattr(app_module, "run_migrations", lambda: None)
    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(app_module, "ensure_seed_workflows", lambda s: None)
    monkeypatch.setattr(app_module, "ensure_default_project", lambda s: None)
    # A non-empty task table skips lifespan's fresh-clone demo replay.
    TaskRepository(session).create(project="default", title="t", source_message_ids=[])

    def _started(mode: str, *, wait: float) -> dict:
        """Everything observed while the app is UP. Read after the context
        exits, `dispatcher` is always a cancelled task — lifespan's shutdown
        cancels it — so "is it alive?" has to be asked from inside."""
        ticks.clear()
        monkeypatch.setattr(
            app_module,
            "settings",
            replace(real_settings, dispatch_mode=mode, disable_startup=False),
        )
        with TestClient(app_module.app) as client:
            deadline = time.monotonic() + wait
            while not ticks and time.monotonic() < deadline:
                # Drives the app's event loop from this thread, which is what
                # lets the dispatcher task get its first tick in.
                client.get("/health")
            task = app_module.app.state.dispatcher
            return {
                "started": task is not None,
                "alive": task is not None and not task.done(),
                "ticks": list(ticks),
            }

    workers = _started("workers", wait=5)
    assert workers["started"], "workers mode started no dispatcher"
    assert workers["alive"], "the dispatcher task died immediately"
    assert workers["ticks"], "the dispatcher task exists but never ran a tick"

    # The other direction, so the assertions above are about the branch and not
    # about lifespan starting a dispatcher unconditionally.
    # A short window rather than the full one: there is nothing to wait FOR
    # here, only a chance for a tick that must not happen.
    inline = _started("inline", wait=0.5)
    assert not inline["started"]
    assert inline["ticks"] == []
