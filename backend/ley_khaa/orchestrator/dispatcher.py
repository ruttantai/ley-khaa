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

from ..adapters.base import Destination
from ..adapters.notifier import Notifier, NullNotifier, message_for
from ..config import settings
from ..domain.states import TaskState
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import TaskRow
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

    def _runnable_projects(self) -> list[str]:
        session = self.session_factory()
        try:
            return TaskRepository(session).runnable_projects()
        finally:
            self._close(session)

    def _claim_next(self, project: str) -> str | None:
        session = self.session_factory()
        try:
            repo = TaskRepository(session)
            row = repo.next_runnable(project)
            if row is None:
                return None
            # Read the attempt count BEFORE claiming: claim_lease increments it
            # when it takes over an expired lease, so checking afterwards would
            # be off by one and let a poison task have one extra run.
            attempts = row.lease_attempts or 0
            if attempts >= settings.max_lease_attempts:
                self._fail_poison(repo, row.id, attempts)
                return None
            if not repo.claim_lease(
                row.id, owner=self.owner, ttl_seconds=settings.lease_ttl_seconds
            ):
                return None
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

    def _close(self, session) -> None:
        """Tests hand in one long-lived session and must keep it; the app hands
        in SessionLocal and wants each unit of work to close its own."""
        if getattr(session, "_ley_khaa_shared", False):
            return
        session.close()
