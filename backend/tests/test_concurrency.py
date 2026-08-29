"""Two projects run at once; one project runs one task at a time (spec §8).

The barrier is the point. `drive` for project A waits on a barrier that only
project B's `drive` can release, so if the dispatcher were serial this test
would block until the barrier's own timeout and fail — it cannot pass by
accident of timing the way a sleep-based test can.

Both tests drive the dispatcher over `session_factory` (a file-backed sqlite
database handing out independent connections per call), not the shared
in-memory `session` fixture. Two runnable projects mean two genuinely
concurrent OS threads (`asyncio.to_thread`), and each thread's own
claim/release bookkeeping opens a session internally regardless of what
`drive` itself does — concurrent cursor use on one shared sqlite3 connection
corrupts it even with check_same_thread=False. `test_one_project_runs_one_task_at_a_time`'s
`drive` additionally does its own database work (a `claim`, see below), which
needs the same independent-connection guarantee. `test_dispatcher.py` is the
worked example this follows.
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


def test_one_project_runs_one_task_at_a_time(session_factory):
    """The other half of the claim. Two tasks in ONE project must not overlap,
    so a barrier expecting two arrivals must break.

    `drive` also claims its task out of CLASSIFIED into FAILED — a legal edge
    per domain/states.py's _ALLOWED table — taking it out of the runnable set.
    Without that, the task `drive` just ran would still be CLASSIFIED and
    therefore still runnable, and `next_runnable` is FIFO by `created_at`, so
    the second tick would re-claim the exact same task: `len(driven) == 2`
    would hold while the second task never ran at all. Asserting the two
    driven ids are distinct is what actually catches that failure mode.
    """
    with session_factory() as session:
        _task(session, "acme")
        _task(session, "acme")

    met = threading.Barrier(2, timeout=1)
    overlapped: list[bool] = []

    def drive(session, task_id):
        try:
            met.wait()
            overlapped.append(True)
        except threading.BrokenBarrierError:
            overlapped.append(False)
        TaskRepository(session).claim(
            task_id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
        )

    async def two_ticks():
        first = await Dispatcher(session_factory, drive=drive).tick()
        second = await Dispatcher(session_factory, drive=drive).tick()
        return first + second

    driven = asyncio.run(two_ticks())

    assert len(driven) == 2, "both tasks should eventually run"
    assert len(set(driven)) == 2, "the two driven tasks must be distinct"
    assert overlapped == [False, False], "two tasks in one project must never overlap"
