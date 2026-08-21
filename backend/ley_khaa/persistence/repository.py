from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..domain.states import TaskState, ensure_transition
from ..interpreter.spec import TaskSpec
from .orm import TaskRow


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        project: str,
        title: str,
        source_message_ids: list[str],
        candidate_id: str | None = None,
    ) -> TaskRow:
        row = TaskRow(
            id=str(uuid.uuid4()),
            project=project,
            state=TaskState.RECEIVED.value,
            title=title,
            source_message_ids=source_message_ids,
            candidate_id=candidate_id,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get(self, task_id: str) -> TaskRow | None:
        return self.session.get(TaskRow, task_id)

    def list(self) -> list[TaskRow]:
        return list(self.session.scalars(select(TaskRow).order_by(TaskRow.created_at)))

    def claim(self, task_id: str, *, expected: TaskState, target: TaskState) -> bool:
        """Atomically move a task from `expected` to `target`. True if we won it.

        The driver is re-entrant and the sweeper runs concurrently with HTTP
        handlers, so two callers can read the same task in the same state. The
        WHERE guard means exactly one of them performs the transition; the loser
        gets False and must simply stop, the same contract as
        CandidateRepository.claim_for_promotion.
        """
        ensure_transition(expected, target)
        result = self.session.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.state == expected.value)
            .values(state=target.value, updated_at=datetime.now(timezone.utc))
        )
        self.session.commit()
        return result.rowcount == 1

    def _row(self, task_id: str) -> TaskRow:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        return row

    def save_spec(self, task_id: str, spec: TaskSpec) -> TaskRow:
        row = self._row(task_id)
        row.spec = spec.model_dump(mode="json")
        self.session.commit()
        self.session.refresh(row)
        return row

    def save_recommendation(
        self, task_id: str, *, mode: str, confidence: float, risk: float, reason: str
    ) -> TaskRow:
        row = self._row(task_id)
        row.recommended_mode = mode
        row.confidence = confidence
        row.risk = risk
        row.autonomy_reason = reason
        self.session.commit()
        self.session.refresh(row)
        return row

    def set_override(self, task_id: str, mode: str | None) -> TaskRow:
        row = self._row(task_id)
        row.mode_override = mode
        self.session.commit()
        self.session.refresh(row)
        return row

    def set_open_question(self, task_id: str, question: str | None) -> TaskRow:
        row = self._row(task_id)
        row.open_question = question
        self.session.commit()
        self.session.refresh(row)
        return row

    def append_source_messages(self, task_id: str, message_ids: list[str]) -> TaskRow:
        row = self._row(task_id)
        existing = list(row.source_message_ids or [])
        # Re-assigning rather than mutating: SQLAlchemy does not track in-place
        # edits to a JSON column, so row.source_message_ids.append() would not
        # be persisted.
        row.source_message_ids = existing + [m for m in message_ids if m not in existing]
        self.session.commit()
        self.session.refresh(row)
        return row

    def record_failure(self, task_id: str, reason: str) -> TaskRow:
        row = self._row(task_id)
        row.failure_reason = reason
        self.session.commit()
        self.session.refresh(row)
        return row

    def increment_interpret_attempts(self, task_id: str) -> int:
        row = self._row(task_id)
        row.interpret_attempts = (row.interpret_attempts or 0) + 1
        self.session.commit()
        self.session.refresh(row)
        return row.interpret_attempts

    def increment_clarification_rounds(self, task_id: str) -> int:
        row = self._row(task_id)
        row.clarification_rounds = (row.clarification_rounds or 0) + 1
        self.session.commit()
        self.session.refresh(row)
        return row.clarification_rounds

    def list_by_state(self, state: TaskState) -> list[TaskRow]:
        return list(
            self.session.scalars(
                select(TaskRow).where(TaskRow.state == state.value).order_by(TaskRow.created_at)
            )
        )
