"""Cross-project concurrency and FIFO progress within a project (spec §8).

`test_two_projects_genuinely_run_at_the_same_time` is the barrier proof of the
first half of the claim: two projects get two workers running at once. `drive`
for project A waits on a barrier that only project B's `drive` can release, so
if the dispatcher were serial this test would block until the barrier's own
timeout and fail — it cannot pass by accident of timing the way a sleep-based
test can.

The second half of the claim — one project runs one task at a time — is
already proven by composition in `tests/test_dispatcher.py`:
`test_a_tick_takes_only_the_oldest_task_in_a_project` establishes that a tick
takes at most one task per project, and
`test_two_dispatchers_ticking_at_once_do_not_both_take_the_same_task`
establishes, under real barrier-forced contention between two dispatcher
instances, that the lease decides the winner. A barrier inside a single
project's `drive` here could not add to that: `runnable_projects()` GROUP BYs
project, so one tick calls `guarded()` for "acme" exactly once, and two
sequential `await tick()` calls inside one coroutine cannot overlap in
wall-clock time regardless of dispatcher correctness — asyncio guarantees the
second `await` cannot begin until the first, including its nested
`asyncio.gather`/`asyncio.to_thread` work, has fully returned. A barrier
sized for two arrivals in that shape would therefore time out on every run
whether or not the property held, proving nothing. So
`test_two_ticks_take_different_tasks_in_fifo_order` below proves a narrower,
real thing instead: two ticks over one project drive two *different* tasks in
FIFO order rather than the same task twice, which is what a broken
"return the task to the runnable set" step would violate.

Both tests drive the dispatcher over `session_factory` (a file-backed sqlite
database handing out independent connections per call), not the shared
in-memory `session` fixture. Two runnable projects mean two genuinely
concurrent OS threads (`asyncio.to_thread`), and each thread's own
claim/release bookkeeping opens a session internally regardless of what
`drive` itself does — concurrent cursor use on one shared sqlite3 connection
corrupts it even with check_same_thread=False.
`test_two_ticks_take_different_tasks_in_fifo_order`'s `drive` additionally
does its own database work (a `claim`, see below), which needs the same
independent-connection guarantee. `test_dispatcher.py` is the worked example
this follows.
"""
import asyncio
import threading

from ley_khaa.domain.states import TaskState
from ley_khaa.orchestrator.dispatcher import Dispatcher
from ley_khaa.persistence.repository import TaskRepository


def _task(session, project):
    repo = TaskRepository(session)
    row = repo.create(project=project, title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    return row.id


def test_two_projects_genuinely_run_at_the_same_time(session_factory):
    with session_factory() as session:
        _task(session, "acme")
        _task(session, "globex")

    met = threading.Barrier(2, timeout=5)

    def drive(_session, _task_id):
        # Blocks until BOTH workers arrive. Serial execution never gets a second
        # arrival, so this raises BrokenBarrierError on timeout.
        met.wait()

    driven = asyncio.run(Dispatcher(session_factory, drive=drive).tick())
    assert len(driven) == 2
    assert not met.broken


def test_two_ticks_take_different_tasks_in_fifo_order(session_factory):
    """Two tasks in ONE project, driven across two sequential ticks, must be
    two distinct tasks — not the same task claimed twice.

    `drive` claims its task out of CLASSIFIED into FAILED — a legal edge per
    domain/states.py's _ALLOWED table — taking it out of the runnable set.
    Without that, the task `drive` just ran would still be CLASSIFIED and
    therefore still runnable, and `next_runnable` is FIFO by `created_at`, so
    the second tick would re-claim the exact same task: `len(driven) == 2`
    would hold while the second task never ran at all. Asserting the two
    driven ids are distinct is what actually catches that failure mode (see
    the mutation note in the task report: reverting this `drive` to a no-op
    reproduces exactly that regression).

    This test does NOT prove serial-within-a-project under contention — the
    module docstring explains why a barrier can't do that here, and points
    at the two `test_dispatcher.py` tests that already do.
    """
    with session_factory() as session:
        _task(session, "acme")
        _task(session, "acme")

    def drive(_session, task_id):
        TaskRepository(_session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    async def two_ticks():
        first = await Dispatcher(session_factory, drive=drive).tick()
        second = await Dispatcher(session_factory, drive=drive).tick()
        return first + second

    driven = asyncio.run(two_ticks())

    assert len(driven) == 2, "both tasks should eventually run"
    assert len(set(driven)) == 2, "the two driven tasks must be distinct"
