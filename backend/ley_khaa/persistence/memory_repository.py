"""Task memory storage (spec §5.14)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..interpreter.spec import TaskSpec
from .orm import MemoryRow


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self, *, project: str, fingerprint: str, intent: str, spec: TaskSpec, task_id: str
    ) -> MemoryRow:
        """Remember a proven spec, or note that we have seen this again.

        An upsert, not an insert: times_seen is what the autonomy dial reads,
        and a second row for the same request would keep every repeat at 1 —
        familiarity would never accumulate and the feature would do nothing
        while appearing to work.
        """
        existing = self.by_fingerprint(project, fingerprint)
        if existing is not None:
            existing.times_seen += 1
            existing.last_seen_at = datetime.now(timezone.utc)
            # source_task_id is NOT updated: it points at the run that first
            # proved this spec, which is what the dashboard links to.
            self.session.commit()
            return existing

        row = MemoryRow(
            id=str(uuid.uuid4()),
            project=project,
            fingerprint=fingerprint,
            intent=intent,
            spec=spec.model_dump(mode="json"),
            source_task_id=task_id,
        )
        self.session.add(row)
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
            )
        )

    def get(self, memory_id: str) -> MemoryRow | None:
        return self.session.get(MemoryRow, memory_id)
