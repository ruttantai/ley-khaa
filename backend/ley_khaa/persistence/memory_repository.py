"""Task memory storage (spec §5.14)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..interpreter.spec import TaskSpec
from .orm import MemoryRow

# memory/matcher.py renders every row for_project() returns into the Haiku
# prompt on every fingerprint miss, and nothing prunes task_memory — there is
# no delete API, so growth is monotonic. Left unbounded, the rendered prompt
# eventually exceeds Haiku's context window, the call raises, and
# MemoryMatcher.recall's blanket except turns that into a permanent, silent
# "always a miss": the exact-fingerprint path still works, so nothing looks
# broken except a repeating stack trace in the logs. Capping here, ordered by
# last_seen_at desc, keeps the 50 memories most likely to be a repeat.
RECALL_CANDIDATE_LIMIT = 50


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self, *, project: str, fingerprint: str, intent: str, spec: TaskSpec, task_id: str
    ) -> MemoryRow | None:
        """Remember a proven spec, or note that we have seen this again.

        An upsert, not an insert: times_seen is what the autonomy dial reads,
        and a second row for the same request would keep every repeat at 1 —
        familiarity would never accumulate and the feature would do nothing
        while appearing to work.

        Check-then-insert, so it races: the orchestrator runs concurrent
        per-project queues, and two identical requests can finish at once. A
        `(project, fingerprint)` unique constraint on the table means the
        loser's insert raises IntegrityError instead of creating a second row
        — caught here and turned into the same increment the winner's request
        would have produced on a later, non-racing repeat.

        An empty fingerprint is not remembered at all — it returns None
        without touching the table. by_fingerprint() refuses to match one
        (so does Task 12's MemoryMatcher.recall, before ever querying), which
        makes a stored empty-fingerprint row unrecallable by construction. A
        second one would only exist to collide with the first under the
        unique constraint above — refusing keeps that invariant in one place
        instead of teaching every layer its own way around it.
        """
        if not fingerprint:
            return None

        existing = self.by_fingerprint(project, fingerprint)
        if existing is not None:
            return self._touch(existing)

        row = MemoryRow(
            id=str(uuid.uuid4()),
            project=project,
            fingerprint=fingerprint,
            intent=intent,
            spec=spec.model_dump(mode="json"),
            source_task_id=task_id,
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            existing = self.by_fingerprint(project, fingerprint)
            if existing is None:
                # The IntegrityError was not the duplicate key — should not
                # happen in normal operation.
                raise
            return self._touch(existing)
        return row

    def _touch(self, row: MemoryRow) -> MemoryRow:
        row.times_seen += 1
        row.last_seen_at = datetime.now(timezone.utc)
        # source_task_id is NOT updated: it points at the run that first
        # proved this spec, which is what the dashboard links to.
        self.session.commit()
        return row

    def by_fingerprint(self, project: str, fingerprint: str) -> MemoryRow | None:
        if not fingerprint:
            return None
        return self.session.scalars(
            select(MemoryRow).where(
                MemoryRow.project == project, MemoryRow.fingerprint == fingerprint
            )
        ).one_or_none()

    def for_project(self, project: str) -> list[MemoryRow]:
        return list(
            self.session.scalars(
                select(MemoryRow)
                .where(MemoryRow.project == project)
                .order_by(MemoryRow.last_seen_at.desc())
                .limit(RECALL_CANDIDATE_LIMIT)
            )
        )

    def get(self, memory_id: str) -> MemoryRow | None:
        return self.session.get(MemoryRow, memory_id)
