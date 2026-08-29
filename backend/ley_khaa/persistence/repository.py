from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, or_, select, update
from sqlalchemy.orm import Session

from ..domain.states import TERMINAL, WAITING, TaskState, ensure_transition
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

    def save_execution(self, task_id: str, *, workspace_path: str, verdict: dict) -> TaskRow:
        """Persist where the bundle is and what the run came to.

        One write for both, because a workspace_path without its verdict is a
        bundle nobody can interpret and a verdict without its path is a claim
        with no evidence behind it.
        """
        row = self._row(task_id)
        row.workspace_path = workspace_path
        row.execution_verdict = verdict
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

    def save_memory_hit(self, task_id: str, *, source_task_id: str, familiarity: int) -> TaskRow:
        """Record that this spec came from memory rather than the interpreter.

        familiarity feeds the autonomy dial; source_task_id is what the
        dashboard links back to so a human can see what is being reused.
        """
        row = self._row(task_id)
        row.remembered_from_task_id = source_task_id
        row.familiarity = familiarity
        self.session.commit()
        return row

    # --- the lease that makes this table the queue (spec §3.2) --------------

    def claim_lease(
        self, task_id: str, *, owner: str, ttl_seconds: int, now: datetime | None = None
    ) -> bool:
        """Take the lease on a task. True if we won it.

        Wins only when the lease is free or has EXPIRED — an unexpired lease
        means another worker is genuinely mid-flight, and stealing it would run
        two lanes over one workspace, which is exactly what advance_stalled()
        excluded EXECUTING to avoid.

        The CASE is load-bearing: lease_attempts counts reclaims of an expired
        lease, never ordinary claims. Incrementing unconditionally would count
        every hand-off between states, so a healthy task would trip the attempt
        cap just by making normal progress.
        """
        moment = now or datetime.now(timezone.utc)
        result = self.session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                or_(
                    TaskRow.lease_owner.is_(None),
                    TaskRow.lease_expires_at < moment,
                ),
            )
            .values(
                lease_owner=owner,
                lease_expires_at=moment + timedelta(seconds=ttl_seconds),
                lease_attempts=TaskRow.lease_attempts
                + case((TaskRow.lease_owner.is_(None), 0), else_=1),
            )
            # synchronize_session="auto" (the default) tries the "evaluate"
            # strategy first: re-check this WHERE clause in Python against any
            # already-loaded copy of this row in the identity map (e.g. the one
            # next_runnable() just loaded on the same session). SQLite has no
            # native tz-aware datetime type, so a value written as aware and
            # then re-read comes back naive — Python then refuses to compare
            # it against the aware `moment` above ("can't compare offset-naive
            # and offset-aware datetimes"). Postgres returns real tz-aware
            # timestamps, so this never bites in production. "fetch" keeps the
            # identity map in sync the way callers rely on (a SELECT for the
            # matched ids, then setting attributes from .values() directly)
            # without re-evaluating the WHERE clause in Python, sidestepping
            # the mismatch rather than juggling naive/aware conversions.
            .execution_options(synchronize_session="fetch")
        )
        self.session.commit()
        return result.rowcount == 1

    def heartbeat_lease(
        self, task_id: str, *, owner: str, ttl_seconds: int, now: datetime | None = None
    ) -> bool:
        """Push the expiry out while work is still in flight.

        Guarded on ownership: a worker whose lease already expired and was taken
        over must not be able to extend it back out from under its successor.
        A False here tells the caller it has lost the task and must stop.
        """
        moment = now or datetime.now(timezone.utc)
        result = self.session.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.lease_owner == owner)
            .values(lease_expires_at=moment + timedelta(seconds=ttl_seconds))
            # Same naive/aware sqlite mismatch as claim_lease above.
            .execution_options(synchronize_session="fetch")
        )
        self.session.commit()
        return result.rowcount == 1

    def release_lease(self, task_id: str, *, owner: str) -> bool:
        """Hand the task back. Guarded on ownership for the same reason as the
        heartbeat: releasing a lease you no longer hold would hand a live task
        to a third worker."""
        result = self.session.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.lease_owner == owner)
            .values(lease_owner=None, lease_expires_at=None)
        )
        self.session.commit()
        return result.rowcount == 1

    # --- what is waiting to run --------------------------------------------

    def _runnable_where(self, moment: datetime):
        """A task is runnable when nothing else is driving it and nobody is
        waiting on a human: state is neither terminal nor human-waiting, and the
        lease is free or expired."""
        blocked = [s.value for s in WAITING | TERMINAL]
        return (
            TaskRow.state.not_in(blocked),
            or_(TaskRow.lease_owner.is_(None), TaskRow.lease_expires_at < moment),
        )

    def runnable_projects(self, now: datetime | None = None) -> list[str]:
        moment = now or datetime.now(timezone.utc)
        rows = self.session.scalars(
            select(TaskRow.project)
            .where(*self._runnable_where(moment))
            .group_by(TaskRow.project)
            .order_by(TaskRow.project)
        )
        return list(rows)

    def next_runnable(self, project: str, now: datetime | None = None) -> TaskRow | None:
        """The oldest runnable task in a project. FIFO: urgency-based reordering
        is deliberately out of scope (spec §7) because urgency lives in the spec,
        which is only known after the task has already been dequeued."""
        moment = now or datetime.now(timezone.utc)
        return self.session.scalars(
            select(TaskRow)
            .where(TaskRow.project == project, *self._runnable_where(moment))
            .order_by(TaskRow.created_at)
        ).first()
