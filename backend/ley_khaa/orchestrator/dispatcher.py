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
from collections.abc import Callable
from contextlib import suppress

from ..config import settings
from ..domain.states import TaskState
from ..persistence.repository import TaskRepository

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], object]
Drive = Callable[[object, str], None]


class Dispatcher:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        drive: Drive,
        owner: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.drive = drive
        # Identifies this dispatcher in the lease. Distinct per process so a
        # second backend cannot silently believe it holds another's tasks.
        self.owner = owner or f"dispatcher-{uuid.uuid4().hex[:8]}"

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

        limit = asyncio.Semaphore(settings.max_concurrent_projects)

        async def guarded(project: str) -> str | None:
            async with limit:
                return await self._work_one(project)

        results = await asyncio.gather(
            *(guarded(project) for project in projects), return_exceptions=True
        )
        driven: list[str] = []
        for result in results:
            if isinstance(result, BaseException):
                # Already logged in _work_one; one project's failure must never
                # take the others' results with it.
                logger.exception("dispatching a project failed", exc_info=result)
            elif result is not None:
                driven.append(result)
        return driven

    async def _work_one(self, project: str) -> str | None:
        task_id = await asyncio.to_thread(self._claim_next, project)
        if task_id is None:
            return None

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
        return task_id

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

    def _call(self, session, fn, *args, **kwargs):
        """Run one repository call, serialized on the session's lock if it has
        one.

        A test's `session_factory` returns one shared object for every call —
        marked `_ley_khaa_shared` so `_close` leaves it open — and this
        dispatcher's own DB calls can genuinely run in different OS threads at
        the same moment (that IS "concurrent across projects"). A bare sqlite3
        connection is not safe for concurrent cursor use even with
        check_same_thread=False: it corrupts. `SessionLocal` in production
        hands each unit of work its own connection from a real pool and never
        carries a lock, so production dispatch is never serialized by this.

        The lock wraps exactly ONE repository call, not the surrounding
        method: two threads' calls to `next_runnable` and `claim_lease` must
        still be free to interleave, because it is `claim_lease`'s own atomic
        UPDATE...WHERE guard that has to decide the winner. Locking the whole
        read-then-write sequence would serialize the decision itself and hide
        a broken guard behind coarse Python-level mutual exclusion.
        """
        lock = getattr(session, "_ley_khaa_lock", None)
        if lock is None:
            return fn(*args, **kwargs)
        with lock:
            return fn(*args, **kwargs)

    def _runnable_projects(self) -> list[str]:
        session = self.session_factory()
        try:
            repo = TaskRepository(session)
            return self._call(session, repo.runnable_projects)
        finally:
            self._close(session)

    def _claim_next(self, project: str) -> str | None:
        session = self.session_factory()
        try:
            repo = TaskRepository(session)
            row = self._call(session, repo.next_runnable, project)
            if row is None:
                return None
            # Read the attempt count BEFORE claiming: claim_lease increments it
            # when it takes over an expired lease, so checking afterwards would
            # be off by one and let a poison task have one extra run.
            attempts = row.lease_attempts or 0
            if attempts >= settings.max_lease_attempts:
                self._fail_poison(session, repo, row.id, attempts)
                return None
            if not self._call(
                session,
                repo.claim_lease,
                row.id,
                owner=self.owner,
                ttl_seconds=settings.lease_ttl_seconds,
            ):
                return None
            return row.id
        finally:
            self._close(session)

    def _fail_poison(self, session, repo: TaskRepository, task_id: str, attempts: int) -> None:
        """A task that has outlived its workers this many times is not going to
        finish. Fail it visibly rather than paying for it forever."""
        row = self._call(session, repo.get, task_id)
        if row is None:
            return
        state = TaskState(row.state)
        # Claim before recording, the ordering c043c46 established: writing the
        # reason first stamps it onto a task whose transition then lost a race.
        if self._call(session, repo.claim, task_id, expected=state, target=TaskState.FAILED):
            self._call(
                session,
                repo.record_failure,
                task_id,
                f"abandoned after {attempts} lease attempts; no worker finished it",
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
            repo = TaskRepository(session)
            return self._call(
                session,
                repo.heartbeat_lease,
                task_id,
                owner=self.owner,
                ttl_seconds=settings.lease_ttl_seconds,
            )
        finally:
            self._close(session)

    def _release(self, task_id: str) -> None:
        session = self.session_factory()
        try:
            repo = TaskRepository(session)
            self._call(session, repo.release_lease, task_id, owner=self.owner)
        finally:
            self._close(session)

    def _close(self, session) -> None:
        """Tests hand in one long-lived session and must keep it; the app hands
        in SessionLocal and wants each unit of work to close its own."""
        if getattr(session, "_ley_khaa_shared", False):
            return
        session.close()
