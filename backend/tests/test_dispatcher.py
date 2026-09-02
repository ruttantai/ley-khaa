import asyncio
import threading
import time
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


def test_a_tick_takes_the_oldest_task_in_a_project_first(session_factory):
    """FIFO ordering within a project's drain: the oldest queued task is
    always driven before a newer one, even though (per the drain test below)
    both get driven within the same tick now."""
    with session_factory() as session:
        first = _task(session, project="acme")
        _task(session, project="acme")

    order: list[str] = []

    def drive(session, task_id):
        order.append(task_id)
        # Move the task out of the runnable set so next_runnable can advance
        # to the next-oldest task instead of reclaiming this same one.
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    asyncio.run(Dispatcher(session_factory, drive=drive).tick())
    assert order[0] == first


def test_a_project_drains_its_whole_backlog_in_one_sweep(session_factory):
    """Backlog item 11: a project with several queued tasks must not be
    paced at one task per tick — it should drain everything runnable in a
    single tick() call."""
    with session_factory() as session:
        ids = [_task(session, project="acme") for _ in range(3)]

    def drive(session, task_id):
        # Real business logic advances the task's state, which is what
        # actually removes it from the runnable set between claims.
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())
    assert sorted(result) == sorted(ids)

    with session_factory() as session:
        repo = TaskRepository(session)
        assert repo.next_runnable("acme") is None


def test_a_slow_project_does_not_pace_a_fast_project(session_factory):
    """The direct regression test for item 11: a project with a slow task
    must not hold back another project's own backlog from draining within
    the same tick. Under the old one-claim-per-tick behaviour, `fast` would
    only get one of its three tasks driven per tick regardless of how long
    `slow` takes; under the fix it drains all three in this one tick call,
    concurrently with `slow`'s single long task."""
    with session_factory() as session:
        slow_id = _task(session, project="slow")
        fast_ids = [_task(session, project="fast") for _ in range(3)]

    def drive(session, task_id):
        if task_id == slow_id:
            time.sleep(0.3)
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    start = time.monotonic()
    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())
    elapsed = time.monotonic() - start

    assert sorted(result) == sorted([slow_id, *fast_ids])
    # Both projects run concurrently (separate semaphore slots); the fast
    # project's whole backlog should drain well within the slow task's own
    # duration, not be serialized behind it across several ticks.
    assert elapsed < 1.0


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


def test_a_repeatedly_failing_task_does_not_drain_forever(session_factory):
    """The termination hazard a drain loop introduces: `release_lease` does
    not touch `lease_attempts` (only reclaiming an EXPIRED lease does), so a
    task whose drive raises every time goes right back to the runnable set
    with its attempt count unchanged. A drain that just loops "until
    `_claim_next` returns None" would claim this same task forever and never
    return. It must give up on a task after one failed attempt per tick
    instead of retrying it in a tight loop."""
    with session_factory() as session:
        task_id = _task(session, project="acme")

    calls = []

    def drive(_session, tid):
        calls.append(tid)
        raise RuntimeError("boom")

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())

    assert result == [task_id]
    assert calls == [task_id], "the same task must be attempted only once per tick"
    with session_factory() as session:
        # Still reclaimable — a later tick (or the attempt cap) handles it,
        # but this drain must not have spun on it.
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

    def drive_and_park(session, tid):
        # A drain re-claims within the same tick until a task leaves the
        # runnable set (see dispatcher._work_one), unlike the single-claim
        # model this test was written against. Real driving always moves a
        # task on success (TaskDriver.advance() runs until it hits a WAITING
        # state); a no-op double would instead get reclaimed a second time
        # this same tick and spuriously trip the cap. Parking it in a
        # WAITING-but-not-FAILED state keeps this test's actual subject —
        # the poison threshold — isolated from that drain mechanic.
        driven.append(tid)
        TaskRepository(session).claim(
            tid, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION
        )

    driven = []
    asyncio.run(Dispatcher(session_factory, drive=drive_and_park).tick())

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

    `drive` also parks the task out of the runnable set (see the same note in
    test_a_task_past_the_attempt_cap...): under the drain, the winner makes
    one further next_runnable call after driving to confirm nothing is left.
    That call must find the task already non-runnable, or claim_lease would
    fire a second, unsynchronized time and the barrier below — sized for
    exactly the two contended calls this test is about — would hang. The
    read barrier is additionally only engaged for the first two calls total
    (one from each thread's genuinely contested read) for the same reason:
    the winner's own confirmation call is real drain behaviour, not part of
    the race being tested here, and must pass through unsynchronized.
    """
    with session_factory() as session:
        task_id = _task(session, project="acme")
    driven = []
    lock = threading.Lock()
    read_barrier = threading.Barrier(2)
    claim_barrier = threading.Barrier(2)
    read_calls = 0

    def drive(session, tid):
        with lock:
            driven.append(tid)
        TaskRepository(session).claim(
            tid, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION
        )

    original_next_runnable = TaskRepository.next_runnable
    original_claim_lease = TaskRepository.claim_lease

    def synced_next_runnable(self, project, *args, **kwargs):
        nonlocal read_calls
        result = original_next_runnable(self, project, *args, **kwargs)
        with lock:
            call_index = read_calls
            read_calls += 1
        if call_index < 2:
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


def test_the_heartbeat_keeps_a_long_running_task_leased(session_factory, monkeypatch):
    """The lease has to outlive the work, and only the heartbeat makes it.

    Every other dispatcher test drives instantly, so the TTL never has a chance
    to elapse mid-flight: replacing `_heartbeat` with `asyncio.sleep(0)` left the
    whole suite green while every real task longer than the TTL would have had
    its lease reclaimed out from under it — two lanes over one workspace, which
    is the exact thing the lease exists to prevent.

    So: a TTL shorter than the work, and the expiry read from a SEPARATE session
    while the worker thread is still holding the task.
    """
    from ley_khaa.config import settings as real_settings

    monkeypatch.setattr(
        dispatcher_module,
        "settings",
        replace(real_settings, lease_ttl_seconds=1.0, lease_heartbeat_seconds=0.1),
    )
    with session_factory() as session:
        task_id = _task(session, project="acme")

    seen: list[tuple[datetime, datetime]] = []

    def drive(_session, tid):
        # 1.5s of work against a 1.0s TTL: without renewal the lease lapses
        # partway through, and next_runnable would hand the task to a second
        # worker while this one is still on it.
        for _ in range(15):
            time.sleep(0.1)
            with session_factory() as fresh:
                row = TaskRepository(fresh).get(tid)
                seen.append((datetime.now(timezone.utc), _as_utc(row.lease_expires_at)))

    asyncio.run(Dispatcher(session_factory, drive=drive, owner="w1").tick())

    assert len(seen) == 15
    assert seen[-1][1] > seen[0][1], "the expiry never moved: nothing renewed the lease"
    # The load-bearing one: at the last check, 1.5s in, the original 1.0s lease
    # would long since have expired. It has not.
    checked_at, expires_at = seen[-1]
    assert expires_at > checked_at, "the lease lapsed while its worker still held the task"


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes even for timezone=True columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
