import asyncio
import threading

from ley_khaa.domain.states import TaskState
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


def test_a_tick_drives_one_task_per_project(session):
    a = _task(session, project="acme")
    g = _task(session, project="globex")
    driven = []

    def drive(_session, task_id):
        driven.append(task_id)

    dispatcher = Dispatcher(lambda: session, drive=drive)
    result = asyncio.run(dispatcher.tick())

    assert sorted(result) == sorted([a, g])
    assert sorted(driven) == sorted([a, g])


def test_a_tick_takes_only_the_oldest_task_in_a_project(session):
    first = _task(session, project="acme")
    _task(session, project="acme")

    def drive(_session, task_id):
        pass

    assert asyncio.run(Dispatcher(lambda: session, drive=drive).tick()) == [first]


def test_the_lease_is_released_after_the_work_finishes(session):
    task_id = _task(session, project="acme")

    def drive(_session, _task_id):
        pass

    asyncio.run(Dispatcher(lambda: session, drive=drive).tick())
    assert TaskRepository(session).get(task_id).lease_owner is None


def test_the_lease_is_released_even_when_the_work_raises(session):
    """A dispatcher that leaks leases on failure is worse than inline execution:
    the task stays invisible until its TTL expires, every time it fails."""
    task_id = _task(session, project="acme")

    def drive(_session, _task_id):
        raise RuntimeError("boom")

    asyncio.run(Dispatcher(lambda: session, drive=drive).tick())
    assert TaskRepository(session).get(task_id).lease_owner is None


def test_one_bad_task_does_not_stop_the_other_projects(session, monkeypatch):
    """A raise from `drive` itself never reaches asyncio.gather at all —
    _work_one's own try/except around `_drive` already swallows it, so that
    fault can't tell tick()'s return_exceptions=True apart from False. The
    guarantee this test actually needs is the harder one: a failure in the
    dispatcher's OWN bookkeeping for one project (here, claiming its next
    task) must not take the whole tick down with it. So the fault is injected
    at that level instead of through the drive callback.
    """
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

    result = asyncio.run(Dispatcher(lambda: session, drive=drive).tick())
    assert result == [good]
    assert driven == [good]


def test_a_task_past_the_attempt_cap_fails_instead_of_running_again(session):
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
    """
    from dataclasses import replace

    import ley_khaa.config as config

    task_id = _task(session, project="acme")
    repo = TaskRepository(session)
    row = repo.get(task_id)
    row.lease_attempts = 99
    session.commit()

    original = config.settings
    config.settings = replace(original, max_lease_attempts=3)
    try:
        driven = []
        asyncio.run(
            Dispatcher(lambda: session, drive=lambda s, t: driven.append(t)).tick()
        )
    finally:
        config.settings = original

    assert driven == []
    row = repo.get(task_id)
    assert TaskState(row.state) is TaskState.FAILED
    assert "lease" in (row.failure_reason or "")

    # The off-by-one boundary: a legitimately expired lease, one attempt below
    # the cap, must still be reclaimed and driven rather than abandoned.
    from datetime import datetime, timedelta, timezone

    below_cap_id = _task(session, project="globex")
    below_row = repo.get(below_cap_id)
    below_row.lease_attempts = 2
    below_row.lease_owner = "dead-worker"
    below_row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    session.commit()

    config.settings = replace(original, max_lease_attempts=3)
    try:
        driven = []
        asyncio.run(
            Dispatcher(lambda: session, drive=lambda s, t: driven.append(t)).tick()
        )
    finally:
        config.settings = original

    assert driven == [below_cap_id]
    below_row = repo.get(below_cap_id)
    assert below_row.lease_attempts == 3
    assert TaskState(below_row.state) is not TaskState.FAILED


def test_two_dispatchers_ticking_at_once_do_not_both_take_the_same_task(session, monkeypatch):
    """The claim is what makes one worker per project true under concurrency.

    On a fast local sqlite database, the window between reading the runnable
    task and claiming its lease is narrow enough that two dispatcher threads
    almost never actually land inside it together — without forcing the
    overlap, this test would pass even if the claim_lease guard were deleted,
    which is exactly the "lease test that never actually contends" failure
    mode. The barrier below forces both threads to finish reading the SAME
    unclaimed task before either is allowed to attempt the claim, so the
    dispatcher's own claim_lease guard — not scheduling luck — is what has to
    make only one of them win.
    """
    task_id = _task(session, project="acme")
    driven = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def drive(_session, tid):
        with lock:
            driven.append(tid)

    original_call = Dispatcher._call

    def synced_call(self, session, fn, *args, **kwargs):
        result = original_call(self, session, fn, *args, **kwargs)
        if getattr(fn, "__name__", "") == "next_runnable":
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(Dispatcher, "_call", synced_call)

    async def both():
        one = Dispatcher(lambda: session, drive=drive, owner="w1")
        two = Dispatcher(lambda: session, drive=drive, owner="w2")
        return await asyncio.gather(one.tick(), two.tick())

    results = asyncio.run(both())
    assert sorted(sum(results, [])) == [task_id]
    assert driven == [task_id]


def test_a_project_with_nothing_runnable_is_skipped(session):
    _task(session, project="acme", state=TaskState.AWAITING_APPROVAL)
    assert asyncio.run(Dispatcher(lambda: session, drive=lambda s, t: None).tick()) == []
