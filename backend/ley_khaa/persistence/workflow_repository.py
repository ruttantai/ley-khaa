"""The registry's storage (spec §5.6).

Follows the house pattern: rows in orm.py, access here, no ORM objects
constructed anywhere else.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .orm import WorkflowRow


class DuplicateWorkflow(Exception):
    """That name is taken. Names are how a human refers to a capability, so
    silently versioning behind one would make the registry unreadable."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        name: str,
        description: str,
        operation_aliases: list[str],
        output_format: str,
        inputs: list[dict],
        source: str,
        origin: str = "promoted",
        promoted_from_task_id: str | None = None,
    ) -> WorkflowRow:
        if self.get(name) is not None:
            raise DuplicateWorkflow(name)
        row = WorkflowRow(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            operation_aliases=list(operation_aliases),
            output_format=output_format,
            inputs=list(inputs),
            source=source,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            origin=origin,
            promoted_from_task_id=promoted_from_task_id,
            promoted_at=_now(),
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get(self, name: str) -> WorkflowRow | None:
        return self.session.scalars(
            select(WorkflowRow).where(WorkflowRow.name == name)
        ).one_or_none()

    def list(self) -> list[WorkflowRow]:
        return list(self.session.scalars(select(WorkflowRow).order_by(WorkflowRow.name)))

    def active(self) -> list[WorkflowRow]:
        """What the matcher is allowed to consider."""
        return [row for row in self.list() if not row.quarantined]

    def _row(self, name: str) -> WorkflowRow:
        row = self.get(name)
        if row is None:
            raise KeyError(name)
        return row

    def record_success(self, name: str, *, learned_alias: str | None = None) -> WorkflowRow:
        """Count a successful run, and learn the phrasing that found it.

        The counter is an atomic UPDATE rather than a read-modify-write: the
        dispatcher runs projects in parallel, so two cached runs of the same
        workflow can interleave and a Python-side increment loses one of them.

        The alias list cannot be incremented, so it is a compare-and-swap on the
        value we read, retried once. A lost retry costs one extra Haiku call the
        next time that phrasing appears; it never corrupts the list.
        """
        self.session.execute(
            update(WorkflowRow)
            .where(WorkflowRow.name == name)
            .values(runs_ok=WorkflowRow.runs_ok + 1, last_used_at=_now())
        )
        self.session.commit()

        if learned_alias:
            for _ in range(2):
                row = self._row(name)
                current = list(row.operation_aliases or [])
                if learned_alias in current:
                    break
                result = self.session.execute(
                    update(WorkflowRow)
                    .where(
                        WorkflowRow.name == name,
                        WorkflowRow.operation_aliases == current,
                    )
                    .values(operation_aliases=current + [learned_alias])
                )
                self.session.commit()
                if result.rowcount == 1:
                    break
                self.session.expire_all()
        self.session.expire_all()
        return self._row(name)

    def record_failure(self, name: str) -> WorkflowRow:
        self.session.execute(
            update(WorkflowRow)
            .where(WorkflowRow.name == name)
            .values(runs_failed=WorkflowRow.runs_failed + 1, quarantined=True)
        )
        self.session.commit()
        self.session.expire_all()
        return self._row(name)

    def unquarantine(self, name: str) -> WorkflowRow:
        row = self._row(name)
        row.quarantined = False
        self.session.commit()
        self.session.refresh(row)
        return row

    def delete(self, name: str) -> None:
        self.session.delete(self._row(name))
        self.session.commit()
