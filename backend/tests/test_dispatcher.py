import asyncio
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ley_khaa.domain.states import TaskState
from ley_khaa.orchestrator import dispatcher as dispatcher_module
from ley_khaa.orchestrator.dispatcher import Dispatcher
from ley_khaa.persistence.repository import TaskRepository


# The legal chain from RECEIVED to each state this helper is asked to reach.
# RECEIVED -> AWAITING_APPROVAL is not a single legal hop (see domain/states.py's
# _ALLOWED table), so a task destined to sit in AWAITING_APPROVAL must actually
# walk CLASSIFIED -> INTERPRETED -> AWAITING_APPROVAL like a real task would.
_PATH_TO: dict[TaskState, tuple[TaskState, ...]] = {
    TaskState.CLASSIFIED: (TaskState.CLASSIFIED,),
    TaskState.AWAITING_APPROVAL: (
        TaskState.CLASSIFIED,
        TaskState.INTERPRETED,
        TaskState.AWAITING_APPROVAL,
    ),
}


def _task(session, *, project, state=TaskState.CLASSIFIED):
    repo = TaskRepository(session)
    row = repo.create(project=project, title="t", source_message_ids=[])
    current = TaskState.RECEIVED
    for step in _PATH_TO[state]:
        repo.claim(row.id, expected=current, target=step)
        current = step
    return row.id


def test_a_tick_drives_one_task_per_project(session_factory):
    with session_factory() as session:
        a = _task(session, project="acme")
        g = _task(session, project="globex")
    driven = []

    def drive(_session, task_id):
        driven.append(task_id)

    dispatcher = Dispatcher(session_factory, drive=drive)
    result = asyncio.run(dispatcher.tick())

    assert sorted(result) == sorted([a, g])
    assert sorted(driven) == sorted([a, g])


def test_a_tick_takes_only_the_oldest_task_in_a_project(session_factory):
    with session_factory() as session:
        first = _task(session, project="acme")
        _task(session, project="acme")

    def drive(_session, task_id):
        pass

    assert asyncio.run(Dispatcher(session_factory, drive=drive).tick()) == [first]


def test_the_lease_is_released_after_the_work_finishes(session_factory):
    with session_factory() as session:
        task_id = _task(session, project="acme")

    def drive(_session, _task_id):
        pass

    asyncio.run(Dispatcher(session_factory, drive=drive).tick())
    with session_factory() as session:
        assert TaskRepository(session).get(task_id).lease_owner is None


def test_the_lease_is_released_even_when_the_work_raises(session_factory):
    """A dispatcher that leaks leases on failure is worse than inline execution:
    the task stays invisible until its TTL expires, every time it fails.

    Also proves the `except Exception` around `_drive` is load-bearing rather
    than decorative: without it, the raise would propagate out of `_work_one`
    (its `finally` still releases the lease either way, so asserting only
    `lease_owner is None` cannot tell the two apart) into `gather`'s
    `return_exceptions=True`, which turns that project's slot into an
    exception object instead of a task id — silently dropping the failed
    task from `tick()`'s own return value. `good` still surviving in the
    result is guaranteed independently by `return_exceptions=True`; the
    thing only the inner `except` decides is whether `task_id` itself is
    still reported alongside it.
    """
    with session_factory() as session:
        task_id = _task(session, project="acme")
        good = _task(session, project="globex")

    def drive(_session, tid):
        if tid == task_id:
            raise RuntimeError("boom")

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())
    assert sorted(result) == sorted([task_id, good])

    with session_factory() as session:
        assert TaskRepository(session).get(task_id).lease_owner is None


def test_one_bad_task_does_not_stop_the_other_projects(session_factory, monkeypatch):
    """A raise from `drive` itself never reaches asyncio.gather at all —
    _work_one's own try/except around `_drive` already swallows it, so that
    fault can't tell tick()'s return_exceptions=True apart from False. The
    guarantee this test actually needs is the harder one: a failure in the
    dispatcher's OWN bookkeeping for one project (here, claiming its next
    task) must not take the whole tick down with it. So the fault is injected
    at that level instead of through the drive callback.
    """
    with session_factory() as session:
        _task(session, project="acme")
        good = _task(session, project="globex")
    driven = []

    def drive(_session, task_id):
        driven.append(task_id)

    original_next_runnable = TaskRepository.next_runnable

    def raising_next_runnable(self, project, *args, **kwargs):
        if project == "acme":
            raise RuntimeError("boom")
        return original_next_runnable(self, project, *args, **kwargs)

    monkeypatch.setattr(TaskRepository, "next_runnable", raising_next_runnable)

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())
    assert result == [good]
    assert driven == [good]


def test_a_task_past_the_attempt_cap_fails_instead_of_running_again(
    session_factory, monkeypatch
):
    """The poison-task ceiling. Without it a task that kills its worker every
    time is re-run forever, at two Opus calls a go.

    Also proves the attempt count is read BEFORE claim_lease, not after:
    claim_lease only increments lease_attempts when it reclaims an already
    expired lease (a dead worker's) — never on a task's first-ever claim — so
    the first block below (an unleased task already at the cap) cannot tell
    "check before" from "check after" apart; claim_lease's own increment is
    zero either way there. The second block puts a task in the state where
    that ordering actually bites: a legitimately expired lease one attempt
    below the cap. Reading attempts before the reclaim must still let this
    last legitimate run happen; reading it after would see the reclaim's own
    +1 and abandon a task that was never actually run.

    The settings pin uses monkeypatch on the `settings` name bound into
    `dispatcher`'s own module namespace (the idiom `_pin` in
    test_sandbox_selection.py uses for `sandbox` module), not a manual
    `config.settings = replace(...)` swap: `dispatcher.py` does
    `from ..config import settings`, which binds the object into
    `dispatcher`'s globals at import time. Rebinding `config.settings`
    afterwards only changes the name `config` module points at — the
    dispatcher module's own `settings` name, which is what `_claim_next`
    actually reads, never moves. A manual pin would silently pass regardless
    of what it set, verified only by the *default* env value happening to
    equal it.
    """
    with session_factory() as session:
        task_id = _task(session, project="acme")
        repo = TaskRepository(session)
        row = repo.get(task_id)
        row.lease_attempts = 99
        session.commit()

    from ley_khaa.config import settings as real_settings

    monkeypatch.setattr(dispatcher_module, "settings", replace(real_settings, max_lease_attempts=3))

    driven = []
    asyncio.run(
        Dispatcher(session_factory, drive=lambda s, t: driven.append(t)).tick()
    )

    assert driven == []
    with session_factory() as session:
        row = TaskRepository(session).get(task_id)
        assert TaskState(row.state) is TaskState.FAILED
        assert "lease" in (row.failure_reason or "")

    # The off-by-one boundary: a legitimately expired lease, one attempt below
    # the cap, must still be reclaimed and driven rather than abandoned.
    with session_factory() as session:
        below_cap_id = _task(session, project="globex")
        below_row = TaskRepository(session).get(below_cap_id)
        below_row.lease_attempts = 2
        below_row.lease_owner = "dead-worker"
        below_row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        session.commit()

    driven = []
    asyncio.run(
        Dispatcher(session_factory, drive=lambda s, t: driven.append(t)).tick()
    )

    assert driven == [below_cap_id]
    with session_factory() as session:
        below_row = TaskRepository(session).get(below_cap_id)
        assert below_row.lease_attempts == 3
        assert TaskState(below_row.state) is not TaskState.FAILED


def test_two_dispatchers_ticking_at_once_do_not_both_take_the_same_task(
    session_factory, monkeypatch
):
    """The claim is what makes one worker per project true under concurrency.

    On a fast local sqlite database, the window between reading the runnable
    task and claiming its lease is narrow enough that two dispatcher threads
    almost never actually land inside it together — without forcing the
    overlap, this test would pass even if the claim_lease guard were deleted,
    which is exactly the "lease test that never actually contends" failure
    mode. The first barrier forces both threads to finish reading the SAME
    unclaimed task before either is allowed to attempt the claim, so the
    dispatcher's own claim_lease guard — not scheduling luck — is what has to
    make only one of them win.

    A second barrier, synchronized on claim_lease's own return, is equally
    load-bearing and not just belt-and-suspenders: `drive` here is a no-op, so
    without it the winner can claim, drive (instantly) and release the lease
    all before the loser even calls claim_lease. The loser would then find the
    task genuinely free again and legitimately reclaim it a second time — a
    real task_id driven twice, but not a broken guard, just an uncontended
    second claim after the first was already done and released. Holding both
    threads at the SAME point right after their own claim_lease call ensures
    the loser's attempt genuinely lands while the winner still holds the
    lease, which is the only moment that actually exercises the guard.

    Both dispatchers here get their own real connection from `session_factory`
    (a file-backed sqlite database), so the two threads' concurrent reads and
    writes are arbitrated by SQLite's own locking, the same way two backend
    processes would be arbitrated by Postgres in production.
    """
    with session_factory() as session:
        task_id = _task(session, project="acme")
    driven = []
    lock = threading.Lock()
    read_barrier = threading.Barrier(2)
    claim_barrier = threading.Barrier(2)

    def drive(_session, tid):
        with lock:
            driven.append(tid)

    original_next_runnable = TaskRepository.next_runnable
    original_claim_lease = TaskRepository.claim_lease

    def synced_next_runnable(self, project, *args, **kwargs):
        result = original_next_runnable(self, project, *args, **kwargs)
        read_barrier.wait(timeout=5)
        return result

    def synced_claim_lease(self, task_id, **kwargs):
        result = original_claim_lease(self, task_id, **kwargs)
        claim_barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(TaskRepository, "next_runnable", synced_next_runnable)
    monkeypatch.setattr(TaskRepository, "claim_lease", synced_claim_lease)

    async def both():
        one = Dispatcher(session_factory, drive=drive, owner="w1")
        two = Dispatcher(session_factory, drive=drive, owner="w2")
        return await asyncio.gather(one.tick(), two.tick())

    results = asyncio.run(both())
    assert sorted(sum(results, [])) == [task_id]
    assert driven == [task_id]


def test_a_project_with_nothing_runnable_is_skipped(session_factory):
    with session_factory() as session:
        _task(session, project="acme", state=TaskState.AWAITING_APPROVAL)
    assert asyncio.run(Dispatcher(session_factory, drive=lambda s, t: None).tick()) == []
