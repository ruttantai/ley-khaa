"""The registry's storage (spec §5.6).

Follows the house pattern: rows in orm.py, access here, no ORM objects
constructed anywhere else.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
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
        row = self._row(name)
        row.runs_ok += 1
        row.last_used_at = _now()
        if learned_alias and learned_alias not in (row.operation_aliases or []):
            # Reassign rather than append: a JSON column mutated in place is not
            # always seen as dirty by SQLAlchemy, and the alias would be lost on
            # commit — the learning loop failing silently.
            row.operation_aliases = list(row.operation_aliases or []) + [learned_alias]
        self.session.commit()
        self.session.refresh(row)
        return row

    def record_failure(self, name: str) -> WorkflowRow:
        row = self._row(name)
        row.runs_failed += 1
        row.quarantined = True
        self.session.commit()
        self.session.refresh(row)
        return row

    def unquarantine(self, name: str) -> WorkflowRow:
        row = self._row(name)
        row.quarantined = False
        self.session.commit()
        self.session.refresh(row)
        return row

    def delete(self, name: str) -> None:
        self.session.delete(self._row(name))
        self.session.commit()
