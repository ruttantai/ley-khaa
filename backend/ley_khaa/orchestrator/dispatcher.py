"""One worker per project (spec §3.3).

The queue is the tasks table; this is the thing that drains it. Serial within a
project, concurrent across projects — which is the direct reading of §5.4's
"each project has its own task queue", and what keeps the set of tasks an
amendment could target small and stable.

Nothing here knows how a task is driven. `drive` is injected, so this module can
be tested without an LLM, a sandbox or an orchestrator.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterable
from contextlib import suppress

from sqlalchemy.orm import Session

from ..adapters.base import Destination
from ..adapters.notifier import Notifier, NullNotifier, message_for
from ..config import settings
from ..domain.states import TaskState
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import TaskRow
from ..persistence.repository import TaskRepository

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
Drive = Callable[[Session, str], None]


class Dispatcher:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        drive: Drive,
        owner: str | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.drive = drive
        # Identifies this dispatcher in the lease. Distinct per process so a
        # second backend cannot silently believe it holds another's tasks.
        self.owner = owner or f"dispatcher-{uuid.uuid4().hex[:8]}"
        # NullNotifier by default, the same fallback TaskDriver uses, so every
        # existing construction (a bare Dispatcher(...), every dispatcher test)
        # keeps behaving exactly as it did before this notifier existed.
        self.notifier = notifier or NullNotifier()

    async def run_forever(self, interval: float) -> None:
        """Tick until cancelled. A failing tick is logged, never fatal —
        the same contract as _periodic_sweeper."""
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("dispatcher tick failed")
            await asyncio.sleep(interval)

    async def tick(self) -> list[str]:
        """Give every project with runnable work one worker. Returns the task
        ids actually driven."""
        projects = await asyncio.to_thread(self._runnable_projects)
        if not projects:
            return []

        # Clamped to at least 1, the same posture (and for the same reason)
        # as DeadLetterRepository._prune's cap: a misconfigured setting must
        # degrade, never wedge the service. `asyncio.Semaphore(0)` can never
        # be acquired, so LEY_KHAA_MAX_PROJECTS=0 would leave every
        # `async with limit` below blocked for ever — `gather` never
        # completes, `tick()` never returns, and `run_forever`'s
        # `except Exception` cannot see a hang, so the dispatcher would stop
        # draining every project silently, with no log line and no error.
        # A negative value is the loud version of the same mistake
        # (`ValueError: Semaphore initial value must be >= 0`). "Set it to 0
        # to disable the dispatcher" is a natural operator gesture; it now
        # means "one project at a time", which is the right direction to be
        # wrong in for a setting whose only job is to BOUND concurrency.
        limit = asyncio.Semaphore(max(1, settings.max_concurrent_projects))

        results = await asyncio.gather(
            *(self._work_one(project, limit) for project in projects), return_exceptions=True
        )
        driven: list[str] = []
        for result in results:
            if isinstance(result, BaseException):
                # Already logged in _work_one; one project's failure must never
                # take the others' results with it.
                logger.exception("dispatching a project failed", exc_info=result)
            else:
                driven.extend(result)
        return driven

    async def _work_one(self, project: str, limit: asyncio.Semaphore) -> list[str]:
        """Drain this project's whole backlog for this tick (spec §3.6, item
        11): claim and drive tasks until every runnable one has had its turn,
        rather than stopping after one — so a project's queue depth no longer
        depends on how many external ticks it happens to get.

        "Every runnable one has had its turn" and not "until nothing runnable
        is left": those differ, and the difference is the whole subject of the
        `attempted` paragraph below. Exactly one attempt per task per tick.
        A task the driver deliberately leaves unadvanced is STILL runnable
        when this returns; what changed in v0.10.0 is that the rest of the
        queue is drained anyway rather than abandoned behind it.

        `limit` is held per TASK, not for the whole drain: `async with limit`
        wraps one claim-drive-release cycle per loop iteration, then is
        released before the next one is attempted. Holding it for the whole
        loop instead (the first version of this fix) would have a project
        with a deep backlog occupy a `max_concurrent_projects` slot for its
        entire drain — trading the task-level head-of-line blocking this task
        removes for the same problem one layer up, at the project level.
        Releasing between tasks lets another project's own first task start
        as soon as this one's CURRENT task finishes, not after this whole
        backlog does.

        `attempted` guards termination, and is PASSED DOWN to `_claim_next`
        as `exclude_ids` rather than only being checked on the way back.
        That distinction is the whole point.

        `release_lease` (in `_release`) does not touch `lease_attempts` —
        only reclaiming an EXPIRED lease does — so a task whose drive raises,
        or which the driver deliberately leaves unadvanced (`driver.py`'s
        no-progress path: a lost claim, a retryable interpretation failure,
        the step ceiling), goes right back into the runnable set unchanged.
        Without `attempted` that task would be reclaimed and retried in a
        tight loop forever and this call would never return.

        But merely CHECKING `attempted` after the claim and stopping the lane
        (the first version of this guard) traded that infinite loop for the
        other half of the same defect: the tasks queued BEHIND the offender
        were abandoned, every tick, permanently — `release_lease` leaves
        `lease_attempts` at 0, so the poison cap that might eventually have
        evicted the head can never fire, and the identical head is re-picked
        on every subsequent tick. That is item 11's own symptom (one task
        blocking a project's queue) surviving inside item 11's fix, and it is
        the same shape Ruling 5 removed from `_claim_next`'s two "skip this
        one" sites. This is the third such site, and it now behaves the same
        way: skip past, do not stop.

        Excluding `attempted` in the query is what makes that safe. Every
        iteration either returns (nothing left this lane can claim) or adds
        exactly one NEW id to `attempted`, and a project's backlog is finite,
        so the loop still runs at most len(backlog) + 1 times. Termination is
        not weakened; only the "and abandon the rest" part is gone.

        A task skipped this way is left RUNNABLE and its `lease_attempts` is
        deliberately NOT incremented. `lease_attempts` means one specific
        thing — "a worker died holding this task and its lease had to be
        reclaimed" — and it is the input to the poison cap that FAILs a task
        outright. Counting "did not advance this tick" into it would fail
        healthy tasks for transient reasons the drain cannot tell apart from
        permanent ones: a lost claim race, or an interpretation retry that
        the driver's own `_MAX_INTERPRET_ATTEMPTS` is already counting and
        will itself escalate. Permanently-stuck tasks are the business of the
        ceilings that know WHY they are stuck (`_MAX_INTERPRET_ATTEMPTS`,
        `_MAX_STEPS`, the lease-attempt cap for dead workers); the drain's
        only job is to make sure such a task costs its own lane one attempt
        per tick and nothing else's.
        """
        driven: list[str] = []
        attempted: set[str] = set()
        while True:
            async with limit:
                # frozenset(): _claim_next runs on a worker thread, so it gets
                # an immutable snapshot rather than the live set this loop
                # keeps mutating.
                task_id = await asyncio.to_thread(
                    self._claim_next, project, frozenset(attempted)
                )
                if task_id is None:
                    # Nothing left in this project that this lane has not
                    # already had its one attempt at. The only exit.
                    return driven
                attempted.add(task_id)

                beat = asyncio.create_task(self._heartbeat(task_id))
                try:
                    await asyncio.to_thread(self._drive, task_id)
                except Exception:
                    logger.exception("driving task %s failed", task_id)
                finally:
                    beat.cancel()
                    with suppress(asyncio.CancelledError):
                        await beat
                    await asyncio.to_thread(self._release, task_id)
            driven.append(task_id)

    async def _heartbeat(self, task_id: str) -> None:
        """Keep the lease alive while the worker thread is busy.

        A False from heartbeat_lease means this worker no longer holds the task
        (its lease expired and someone reclaimed it). Stop beating: continuing
        would extend a lease we do not own.
        """
        while True:
            await asyncio.sleep(settings.lease_heartbeat_seconds)
            held = await asyncio.to_thread(self._beat, task_id)
            if not held:
                logger.warning("lost the lease on task %s mid-flight", task_id)
                return

    # --- the synchronous half, each call on its own session ----------------

    def _runnable_projects(self) -> list[str]:
        session = self.session_factory()
        try:
            return TaskRepository(session).runnable_projects()
        finally:
            self._close(session)

    def _claim_next(self, project: str, exclude_ids: Iterable[str] = ()) -> str | None:
        """The next task this worker can actually claim in `project`, or
        `None` if there genuinely is none right now.

        `exclude_ids` is the caller's own "already handled, do not hand it
        back to me" set — `_work_one` passes its `attempted` — and it seeds
        `skipped` below, so those rows are excluded by the QUERY rather than
        rejected after a claim. Excluding them in SQL is what lets the drain
        step past a task it has already attempted this tick instead of
        stopping the lane there; rejecting after the fact would also have to
        undo a lease it should never have taken.

        `next_runnable` can hand back a head task this worker cannot use for
        two different reasons that are NOT "the backlog is empty": it just
        hit the poison-attempt cap (failed in `_fail_poison` below), or
        another worker won the claim race first. Either way the row stops
        being usable but the rest of the project's queue behind it is
        unaffected — so this loops past it via `exclude_ids` instead of
        returning `None`, which would otherwise abandon every task behind an
        unclaimable head task for the whole tick (item 11's own symptom,
        reappearing one level down from the per-tick drain this task added).

        `skipped` guarantees this terminates: every iteration either returns
        (claimed, or truly nothing runnable) or adds exactly one more id to
        `skipped`, and a project's backlog is finite, so the loop can run at
        most len(backlog) + 1 times.
        """
        session = self.session_factory()
        try:
            repo = TaskRepository(session)
            skipped: set[str] = set(exclude_ids)
            while True:
                row = repo.next_runnable(project, exclude_ids=skipped)
                if row is None:
                    return None
                # Read the attempt count BEFORE claiming: claim_lease increments it
                # when it takes over an expired lease, so checking afterwards would
                # be off by one and let a poison task have one extra run.
                attempts = row.lease_attempts or 0
                if attempts >= settings.max_lease_attempts:
                    self._fail_poison(repo, row.id, attempts)
                    skipped.add(row.id)
                    continue
                if not repo.claim_lease(
                    row.id, owner=self.owner, ttl_seconds=settings.lease_ttl_seconds
                ):
                    skipped.add(row.id)
                    continue
                return row.id
        finally:
            self._close(session)

    def _fail_poison(self, repo: TaskRepository, task_id: str, attempts: int) -> None:
        """A task that has outlived its workers this many times is not going to
        finish. Fail it visibly rather than paying for it forever."""
        row = repo.get(task_id)
        if row is None:
            return
        state = TaskState(row.state)
        # Claim before recording, the ordering c043c46 established: writing the
        # reason first stamps it onto a task whose transition then lost a race.
        if repo.claim(task_id, expected=state, target=TaskState.FAILED):
            repo.record_failure(
                task_id, f"abandoned after {attempts} lease attempts; no worker finished it"
            )
            self._announce_poisoned(repo, task_id)

    def _announce_poisoned(self, repo: TaskRepository, task_id: str) -> None:
        """Tell the human this task died, the same shape TaskDriver._announce
        uses (spec §3.6): claim-then-send via mark_notified, best-effort, and
        nothing here can turn a notification failure into a task failure — the
        task is already FAILED by the time this runs.
        """
        try:
            row = repo.get(task_id)
            if row is None:
                return
            text = message_for(row)
            if text is None:
                return
            dest = self._destination(repo, row)
            if dest is None:
                # No originating message means no channel to answer into.
                return
            if not repo.mark_notified(row.id, row.state):
                return
            self.notifier.notify(dest, text)
        except Exception:
            logger.exception("could not announce poisoned task %s", task_id)

    def _destination(self, repo: TaskRepository, row: TaskRow) -> Destination | None:
        """Where this task's channel conversation is (mirrors
        TaskDriver._destination): the FIRST source message is the anchor."""
        sources = MessageRepository(repo.session).get_many(list(row.source_message_ids or []))
        if not sources:
            return None
        first = sources[0]
        return Destination(
            source=first.source,
            conversation_id=first.conversation_id,
            external_id=first.external_id,
        )

    def _drive(self, task_id: str) -> None:
        session = self.session_factory()
        try:
            self.drive(session, task_id)
        finally:
            self._close(session)

    def _beat(self, task_id: str) -> bool:
        session = self.session_factory()
        try:
            return TaskRepository(session).heartbeat_lease(
                task_id, owner=self.owner, ttl_seconds=settings.lease_ttl_seconds
            )
        finally:
            self._close(session)

    def _release(self, task_id: str) -> None:
        session = self.session_factory()
        try:
            TaskRepository(session).release_lease(task_id, owner=self.owner)
        finally:
            self._close(session)

    def _close(self, session: Session) -> None:
        """Tests hand in one long-lived session and must keep it; the app hands
        in SessionLocal and wants each unit of work to close its own."""
        if getattr(session, "_ley_khaa_shared", False):
            return
        session.close()
