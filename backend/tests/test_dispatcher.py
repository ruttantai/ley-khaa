import asyncio
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.orchestrator import dispatcher as dispatcher_module
from ley_khaa.orchestrator.dispatcher import Dispatcher
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
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


def _task_with_message(session, *, project):
    """A CLASSIFIED task that a REAL TaskDriver can actually interpret: it has
    a source message, which `_task` above (a dispatcher-only fixture) does not
    need and does not create."""
    repo = TaskRepository(session)
    message = MessageRepository(session).add(
        Message(source="s", client="c", conversation_id="conv-1", author="boss",
                text="compare bloomberg against factset")
    )
    row = repo.create(project=project, title="t", source_message_ids=[message.id])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    return row.id


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two universes",
        inputs=["bloomberg", "factset"],
        operation="set_difference",
        output_format="xlsx",
        certainty=0.95,
    )
    return TaskSpec(**{**base, **overrides})


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
        second = _task(session, project="acme")

    order: list[str] = []

    def drive(session, task_id):
        order.append(task_id)
        # Move the task out of the runnable set so next_runnable can advance
        # to the next-oldest task instead of reclaiming this same one.
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    asyncio.run(Dispatcher(session_factory, drive=drive).tick())
    # Both ids, not just order[0] == first: under single-claim behaviour
    # order would be [first] and order[0] == first would still (vacuously)
    # hold, so the ordering claim alone can't tell "FIFO within a drain"
    # apart from "only ever claims one task, which happens to be the oldest."
    assert order == [first, second]


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

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())

    # The id assertion alone fully discriminates the regression this test
    # is for: under the old one-claim-per-tick behaviour `fast` contributes
    # only one of its three ids here, regardless of timing. A companion
    # `elapsed < ...` wall-clock assertion was deliberately dropped — it
    # could never be the assertion that actually catches that regression
    # (this one already does, first), only ever a flaky false failure on a
    # loaded machine. See test_a_long_backlog_does_not_delay_another_projects_first_task
    # for the timing-sensitive claim (the semaphore holding per-task, not
    # per-drain), which needs wall-clock measurement to mean anything.
    assert sorted(result) == sorted([slow_id, *fast_ids])


def test_a_poison_capped_head_task_does_not_block_the_rest_of_the_backlog(
    session_factory, monkeypatch
):
    """FIX 1 (item 11's own symptom, one level down from the per-tick drain):
    `_claim_next` has three distinct `return None` sites — nothing runnable,
    the head task just got poison-failed, and the head task lost a claim
    race — and only the first of those means the project's lane is actually
    empty. A poison-capped head task must not block the fresh tasks queued
    behind it."""
    with session_factory() as session:
        poisoned_id = _task(session, project="acme")
        row = TaskRepository(session).get(poisoned_id)
        row.lease_attempts = 99
        session.commit()
        rest = [_task(session, project="acme") for _ in range(2)]

    from ley_khaa.config import settings as real_settings

    monkeypatch.setattr(dispatcher_module, "settings", replace(real_settings, max_lease_attempts=3))

    def drive(session, task_id):
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())

    assert sorted(result) == sorted(rest)
    with session_factory() as session:
        row = TaskRepository(session).get(poisoned_id)
        assert TaskState(row.state) is TaskState.FAILED


def test_a_lost_claim_race_on_the_head_task_does_not_block_the_rest_of_the_backlog(
    session_factory, monkeypatch
):
    """Same symptom as the poison-cap test above, different one of
    `_claim_next`'s three `return None` sites: the head task's `claim_lease`
    call loses the race (another worker won it first). The rest of the
    project's backlog must still drain in this tick rather than being
    abandoned because the head couldn't be claimed.

    `claim_lease` is mocked to always fail for one specific task id — never
    recovering within this tick — the strongest version of "contested": if
    the rest of the backlog drains anyway, it can only be because the drain
    stepped past the contested head rather than stopping at it.
    """
    with session_factory() as session:
        contested_id = _task(session, project="acme")
        rest = [_task(session, project="acme") for _ in range(2)]

    original_claim_lease = TaskRepository.claim_lease

    def losing_claim_lease(self, task_id, **kwargs):
        if task_id == contested_id:
            return False  # another worker always wins this one, this tick
        return original_claim_lease(self, task_id, **kwargs)

    monkeypatch.setattr(TaskRepository, "claim_lease", losing_claim_lease)

    def drive(session, task_id):
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())

    assert sorted(result) == sorted(rest)
    with session_factory() as session:
        row = TaskRepository(session).get(contested_id)
        # Never claimed by us — still sitting there for the next tick to try.
        assert row.lease_owner is None
        assert TaskState(row.state) is TaskState.CLASSIFIED


def test_a_long_backlog_does_not_delay_another_projects_first_task(
    session_factory, monkeypatch
):
    """FIX 2: the semaphore must be held per TASK, not for a whole project's
    drain. With the cap forced to 1, `busy`'s five-task backlog (each task
    ~0.2s) must not make `other`'s first task wait for the whole ~1.0s
    drain — only for whichever single task (its own, or `busy`'s current
    one) is already holding the one slot when it gets its turn.
    """
    from ley_khaa.config import settings as real_settings

    monkeypatch.setattr(
        dispatcher_module, "settings", replace(real_settings, max_concurrent_projects=1)
    )

    with session_factory() as session:
        busy_ids = [_task(session, project="busy") for _ in range(5)]
        other_id = _task(session, project="other")

    started: dict[str, float] = {}
    lock = threading.Lock()

    def drive(session, task_id):
        with lock:
            started[task_id] = time.monotonic()
        time.sleep(0.2)
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    t0 = time.monotonic()
    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())

    assert sorted(result) == sorted([*busy_ids, other_id])
    # Regardless of which project's first task wins the initial race for the
    # one slot, `other` must start within roughly one task's duration — not
    # after `busy`'s whole five-task, ~1.0s backlog. 0.6s gives comfortable
    # margin above the ~0.2-0.4s a correct implementation needs while
    # staying well under the ~1.0s+ the bug this pins would produce.
    assert started[other_id] - t0 < 0.6


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
        row = TaskRepository(session).get(task_id)
        # Still reclaimable, and left EXACTLY as it was found: released, and
        # with `lease_attempts` untouched.
        #
        # This comment used to say "a later tick (or the attempt cap) handles
        # it". Both halves were false and the whole-branch review caught it.
        # A later tick re-drives this same task and fails the same way — that
        # is by design, since the drain cannot tell a transient failure from a
        # permanent one, and the driver's own ceilings (_MAX_INTERPRET_ATTEMPTS,
        # _MAX_STEPS) are what escalate a genuinely stuck task. And the
        # lease-attempt cap CANNOT fire here: `release_lease` never touches
        # `lease_attempts` (this test's own docstring says so), so the count
        # asserted below stays 0 for ever.
        #
        # What this test pins is only the TERMINATION half of the guard. The
        # queue-preservation half — that a task like this one does not take
        # the rest of its project's backlog down with it — is pinned by
        # test_a_head_task_that_makes_no_progress_does_not_starve_the_queue_behind_it
        # and its siblings below, which is the property this single-task
        # test is structurally blind to.
        assert row.lease_owner is None
        assert (row.lease_attempts or 0) == 0


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


def test_a_head_task_that_makes_no_progress_does_not_starve_the_queue_behind_it(
    session_factory,
):
    """The whole-branch review's Critical 1, and the property
    test_a_repeatedly_failing_task_does_not_drain_forever is structurally
    blind to because it queues exactly ONE task.

    `TaskDriver` has an explicit no-progress path (`driver.py`: "No progress:
    a lost claim (another caller won the race) or a retryable failure. Either
    way, stop here") which returns the row UNADVANCED — so the head task is
    still the oldest runnable row the moment the drain asks for the next one.
    The drain used to answer that by stopping the lane, abandoning every task
    queued behind it. It must skip past instead: one attempt for the head,
    then on to the rest.

    `stuck` is driven and left exactly where it was, which is what makes it
    the head again; `rest` advances the way real business logic does.
    """
    with session_factory() as session:
        stuck = _task(session, project="acme")
        rest = [_task(session, project="acme") for _ in range(2)]

    driven: list[str] = []

    def drive(session, task_id):
        driven.append(task_id)
        if task_id == stuck:
            return  # no state change at all: still runnable, still the head
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())

    assert sorted(result) == sorted([stuck, *rest])
    assert driven.count(stuck) == 1, "the stuck head must be attempted once, not spun on"
    for task_id in rest:
        assert task_id in driven, "a stuck head task must not starve the queue behind it"

    with session_factory() as session:
        row = TaskRepository(session).get(stuck)
        # Skipped, not punished: still runnable for the next tick, and its
        # lease-attempt count is deliberately untouched (that counter means
        # "a worker died holding this", and the poison cap it feeds FAILs a
        # task outright — a task that merely did not advance has not earned
        # that).
        assert TaskState(row.state) is TaskState.CLASSIFIED
        assert row.lease_owner is None
        assert (row.lease_attempts or 0) == 0


def test_a_head_task_whose_drive_raises_does_not_starve_the_queue_behind_it(
    session_factory,
):
    """The second shape of Critical 1. `_work_one` swallows any exception out
    of `drive` (`except Exception: logger.exception(...)`) and leaves the row
    untouched, which lands on the identical still-the-head state as the
    no-progress path above.

    Distinct from test_the_lease_is_released_even_when_the_work_raises, whose
    raising task and surviving task are in DIFFERENT projects: there,
    `gather` keeps the lanes apart. Here both tasks are in the SAME project,
    so the only thing that can let the second one run is the drain stepping
    past the first.
    """
    with session_factory() as session:
        exploding = _task(session, project="acme")
        behind = _task(session, project="acme")

    driven: list[str] = []

    def drive(session, task_id):
        driven.append(task_id)
        if task_id == exploding:
            raise RuntimeError("boom")
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())

    assert sorted(result) == sorted([exploding, behind])
    assert driven.count(exploding) == 1
    assert behind in driven, "a raising head task must not starve the queue behind it"


def test_a_stuck_head_task_keeps_letting_the_queue_behind_it_drain_tick_after_tick(
    session_factory,
):
    """Critical 1 was PERMANENT, not per-tick, and this is the half that made
    it so.

    `_release` calls `release_lease`, which sets only `lease_owner` and
    `lease_expires_at` — never `lease_attempts`. So the poison-attempt cap,
    the one mechanism that could eventually have evicted a stuck head, can
    never fire for a task that is released cleanly. The stuck task is the
    oldest runnable row again on the very next tick, for ever. The reviewer
    observed ten ticks, ten drives of the head, ZERO drives of the task
    behind it.

    A second tick with a freshly queued task is therefore the assertion that
    matters: work arriving AFTER the head got stuck must still run.
    """
    with session_factory() as session:
        stuck = _task(session, project="acme")
        first = _task(session, project="acme")

    driven: list[str] = []

    def drive(session, task_id):
        driven.append(task_id)
        if task_id == stuck:
            return
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    dispatcher = Dispatcher(session_factory, drive=drive)
    asyncio.run(dispatcher.tick())
    assert first in driven

    with session_factory() as session:
        second = _task(session, project="acme")

    driven.clear()
    asyncio.run(dispatcher.tick())

    assert second in driven, "the stuck head must not starve work queued after it either"
    assert driven.count(stuck) == 1, "one attempt per tick, still — not a spin"

    with session_factory() as session:
        row = TaskRepository(session).get(stuck)
        # Named explicitly because the old comment on
        # test_a_repeatedly_failing_task_does_not_drain_forever claimed the
        # opposite: the cap does NOT fire here, and nothing is relying on it.
        assert (row.lease_attempts or 0) == 0


def test_a_real_driver_stuck_on_the_head_task_does_not_starve_the_queue(session_factory):
    """The same property, pinned through a REAL `TaskDriver` rather than a
    `drive` double — the one thing the whole-branch review said it could not
    verify.

    `FakeLLM` raises the queued `ConnectionError` on the head task's
    interpretation. `TaskDriver._interpret`'s bare `except Exception` routes
    that to `_fail_interpret(cause=None)`, which — below
    `_MAX_INTERPRET_ATTEMPTS` — deliberately returns False and leaves the
    task in CLASSIFIED for a later retry. That is the production no-progress
    path, reached without a single test double inside the driver.

    The second task's queued response is a spec with a missing field, so it
    lands in NEEDS_CLARIFICATION: one model call, no sandbox, and a state
    only a real drive can produce.
    """
    with session_factory() as session:
        stuck = _task_with_message(session, project="acme")
        behind = _task_with_message(session, project="acme")

    llm = FakeLLM([ConnectionError("upstream is down"), _spec(missing_fields=["output_format"])])

    def drive(session, task_id):
        TaskDriver(
            TaskRepository(session),
            llm=llm,
            messages=MessageRepository(session),
            candidates=CandidateRepository(session),
        ).advance(task_id)

    result = asyncio.run(Dispatcher(session_factory, drive=drive).tick())

    assert sorted(result) == sorted([stuck, behind])
    with session_factory() as session:
        repo = TaskRepository(session)
        # Unadvanced and still runnable — the real driver's own retry posture.
        assert TaskState(repo.get(stuck).state) is TaskState.CLASSIFIED
        # Only a real interpretation can have put it here, so this is proof
        # the second task was actually driven, not merely listed.
        assert TaskState(repo.get(behind).state) is TaskState.NEEDS_CLARIFICATION


def test_a_zero_concurrency_cap_still_ticks_instead_of_hanging_for_ever(
    session_factory, monkeypatch
):
    """`asyncio.Semaphore(0)` can never be acquired. Unclamped,
    LEY_KHAA_MAX_PROJECTS=0 left every `_work_one` blocked on `async with
    limit`, so `gather` never completed and `tick()` never returned — and
    `run_forever`'s `except Exception` cannot see a hang, so the dispatcher
    stopped draining every project silently, with no log line at all.

    Same ruling as `dead_letter_max_rows`: clamp, do not reject and do not
    reinterpret 0 as "unlimited". `wait_for` is what turns the regression
    into a failing test rather than a suite that hangs.
    """
    from ley_khaa.config import settings as real_settings

    monkeypatch.setattr(
        dispatcher_module, "settings", replace(real_settings, max_concurrent_projects=0)
    )

    with session_factory() as session:
        task_id = _task(session, project="acme")

    driven: list[str] = []

    def drive(session, task_id):
        driven.append(task_id)
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    result = asyncio.run(
        asyncio.wait_for(Dispatcher(session_factory, drive=drive).tick(), timeout=10.0)
    )

    assert result == [task_id]
    assert driven == [task_id]
