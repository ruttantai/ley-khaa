from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, or_, select, update
from sqlalchemy.orm import Session

from ..domain.states import TERMINAL, WAITING, TaskState, can_transition, ensure_transition
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

    def set_override(self, task_id: str, mode: str | None, *, expected: TaskState) -> bool:
        """Pin (or clear) the human's mode, conditional on the task still being
        in `expected`. True if we won it.

        Same compare-and-set discipline as claim() and fold_into(), and for a
        sharper reason than either: mode_override is a human's explicit
        instruction about whether work may run unattended, so losing the write
        is worse than losing a state transition. As a plain attribute set it was
        a read-modify-write over the WHOLE row with no WHERE, which cost both
        directions of that instruction — an upgrade landed on tasks that had
        already finished, and a downgrade ("do not run this unattended") could
        commit while the scoring pass that had already read the old value went
        on to run the task anyway. The row afterwards showed the override in
        force, so the audit trail claimed the instruction was active at the
        moment it was ignored.

        `expected` rather than a hardcoded set of states: the policy about WHICH
        states a human may change the mode in belongs to TaskDriver (_ACTIONABLE),
        and the caller passes the state it actually observed, so the guard also
        catches a move BETWEEN two actionable states.
        """
        result = self.session.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.state == expected.value)
            .values(mode_override=mode, updated_at=datetime.now(timezone.utc))
        )
        self.session.commit()
        return result.rowcount == 1

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

    def fold_into(
        self,
        task_id: str,
        *,
        message_ids: list[str],
        expected: TaskState,
        now: datetime | None = None,
    ) -> bool:
        """Merge an amendment's messages into a task and send it back to be
        re-interpreted. True if we won the race.

        Conditional on `expected` — the state observed when the fold was decided
        — because the target can move on in between. A loser must change NOTHING:
        the claim therefore comes first, and the messages are appended only after
        it wins. Appending first would leave a foreign message on a task that is
        already executing, where nothing will ever re-read it.

        This is the same shape as _route_reply's answered-clarification path: the
        amendment is re-INTERPRETED over the enlarged message set, never stapled
        onto the old spec.

        False therefore covers two losses, and the caller treats them alike
        because the recovery is the same: the target is no longer in `expected`,
        or `expected` is itself a state no fold may touch.

        `expected is CLASSIFIED` is its own case, not covered by can_transition:
        a task still in CLASSIFIED has not been interpreted yet, so there is
        nothing to re-trigger — appending the message is enough, and the row
        stays exactly where it is. CLASSIFIED -> CLASSIFIED is deliberately
        absent from the domain transition table (it is not a transition at
        all), so claim()'s ensure_transition would raise on it; _claim_same_state
        below is the CAS this case needs instead.

        That branch changes no state, which matters: every OTHER branch here
        wins mutual exclusion against a worker mid-`_interpret` by winning the
        state change itself — the worker's own CLASSIFIED -> INTERPRETED claim
        then loses to the fold's claim(expected=X, target=CLASSIFIED), or vice
        versa. A same-state CAS has no state change to race on, so it would win
        against a worker that already read CLASSIFIED and is seconds into an
        LLM call, silently folding a message the in-flight interpretation will
        never see — the candidate is PROMOTED and terminal, so nothing returns
        it to triage once that happens. _claim_same_state's lease check is what
        stands in for the state change here: a task a worker holds is excluded
        the same way _runnable_where excludes it from being picked up twice.

        This still leaves a narrow window under INLINE dispatch, which never
        takes a lease at all: two HTTP threads can each observe
        CLASSIFIED-and-unleased and both proceed. That gap is not closed by
        this method — closing it needs a lease taken even in inline mode,
        which is out of scope here.
        """
        moment = now or datetime.now(timezone.utc)
        if expected is TaskState.CLASSIFIED:
            won = self._claim_same_state(task_id, TaskState.CLASSIFIED, moment)
        elif not can_transition(expected, TaskState.CLASSIFIED):
            # EXECUTING and VALIDATING have no edge back to CLASSIFIED: a task
            # with a live sandbox workspace is past the point where its inputs
            # can change. Reaching one is a lost race, not a programmer error —
            # the human fold path reads the target's state long after it was
            # parked, so it can legitimately observe one — and claim() would
            # raise InvalidTransition rather than report the loss.
            #
            # The check must live in front of the claim and cannot be hoisted to
            # the caller: the row IS in `expected` here, so the claim's WHERE
            # clause would match and fold a running task back to CLASSIFIED. And
            # only a check this close to the claim closes the window, since the
            # target can enter EXECUTING after any caller-side look.
            return False
        else:
            won = self.claim(task_id, expected=expected, target=TaskState.CLASSIFIED)
        if not won:
            return False
        self.append_source_messages(task_id, message_ids)
        self.set_open_question(task_id, None)
        return True

    def _claim_same_state(self, task_id: str, state: TaskState, moment: datetime) -> bool:
        """Atomic no-op claim: win only if the row is still in `state` AND
        nobody holds a live lease on it.

        Unlike claim(), this asserts nothing about the domain transition
        table — CLASSIFIED -> CLASSIFIED is not a transition, it is
        confirmation that nothing moved the row out from under a caller
        between its decision and now. The lease check is not optional: with
        no state change here to make a fold and an in-flight interpretation
        mutually exclusive (see fold_into's docstring), the lease is what does
        that job instead — reusing the same predicate _runnable_where uses,
        rather than a second hand-written copy that could drift from it.
        fold_into is the only caller today, for a target that has not been
        interpreted yet and so has no transition for a fold to trigger.
        """
        result = self.session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                TaskRow.state == state.value,
                self._lease_free(moment),
            )
            .values(updated_at=datetime.now(timezone.utc))
            # Same naive/aware sqlite mismatch as claim_lease above: the WHERE
            # clause now compares lease_expires_at against `moment`, so the
            # default "evaluate" sync strategy would try that comparison in
            # Python against an already-loaded row and can raise. "fetch"
            # keeps the identity map in sync without re-evaluating in Python.
            .execution_options(synchronize_session="fetch")
        )
        self.session.commit()
        return result.rowcount == 1

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

    def active_in_project(self, project: str) -> list[TaskRow]:
        """Tasks in this project that are not finished.

        Deliberately includes AWAITING_APPROVAL and NEEDS_CLARIFICATION: a task
        parked in front of a person is exactly the one a follow-up message is
        most likely to be amending.
        """
        finished = [s.value for s in TERMINAL]
        return list(
            self.session.scalars(
                select(TaskRow)
                .where(TaskRow.project == project, TaskRow.state.not_in(finished))
                .order_by(TaskRow.created_at)
            )
        )

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

    def _lease_free(self, moment: datetime):
        """True when nobody holds a live lease on the row — free, or expired.

        The single source of truth for this predicate: every caller that needs
        "is anyone driving this row right now" — _runnable_where, and
        _claim_same_state's fold-vs-in-flight-interpretation guard — reuses
        this rather than hand-writing a second copy that could drift from it.
        """
        return or_(TaskRow.lease_owner.is_(None), TaskRow.lease_expires_at < moment)

    def _runnable_where(self, moment: datetime):
        """A task is runnable when nothing else is driving it and nobody is
        waiting on a human: state is neither terminal nor human-waiting, and the
        lease is free or expired."""
        blocked = [s.value for s in WAITING | TERMINAL]
        return (
            TaskRow.state.not_in(blocked),
            self._lease_free(moment),
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

    def runnable_count(self, project: str, now: datetime | None = None) -> int:
        """How many tasks in this project are waiting for a worker.

        A leased task is excluded: it is being worked on, and the dashboard
        reports it as in-flight instead. Counting it in both places would make
        one task look like two.
        """
        moment = now or datetime.now(timezone.utc)
        return len(
            list(
                self.session.scalars(
                    select(TaskRow.id).where(
                        TaskRow.project == project, *self._runnable_where(moment)
                    )
                )
            )
        )

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

    def leased_task_id(self, project: str, now: datetime | None = None) -> str | None:
        """The task in this project currently held by a live lease, if any.

        The comparison against `now` must stay in SQL rather than be done on
        already-loaded Python attributes: DateTime(timezone=True) round-trips
        NAIVE through SQLite (same hazard claim_lease's docstring covers), so
        `row.lease_expires_at > now` raises TypeError in Python the moment
        lease_expires_at was read fresh from the database rather than pulled
        from a session's identity map. A plain SELECT has SQLite do the
        comparison itself and never hits that mismatch.
        """
        moment = now or datetime.now(timezone.utc)
        return self.session.scalars(
            select(TaskRow.id).where(
                TaskRow.project == project,
                TaskRow.lease_owner.isnot(None),
                TaskRow.lease_expires_at > moment,
            )
        ).first()
