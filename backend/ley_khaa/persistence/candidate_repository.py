import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..crystallizer.candidate import CandidateState, ensure_transition
from .orm import CandidateRow


class CandidateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        *,
        conversation_id: str,
        candidate_key: str,
        title: str,
        summary: str,
        state: CandidateState,
        message_ids: list[str],
        missing_fields: list[str],
        open_question: str | None,
    ) -> CandidateRow:
        row = self.get_by_key(conversation_id, candidate_key)
        if row is None:
            row = CandidateRow(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                candidate_key=candidate_key,
                state=state.value,
            )
            self.session.add(row)
        else:
            ensure_transition(CandidateState(row.state), state)
            row.state = state.value
        row.title = title
        row.summary = summary
        row.message_ids = message_ids
        row.missing_fields = missing_fields
        row.open_question = open_question
        try:
            self.session.commit()
        except IntegrityError:
            # Race: another request inserted the same (conversation_id, candidate_key) after our check.
            self.session.rollback()
            row = self.get_by_key(conversation_id, candidate_key)
            if row is None:
                # Integrity error was not the duplicate key (should not happen in normal operation).
                raise
            # Apply the requested update to the row that won the race.
            ensure_transition(CandidateState(row.state), state)
            row.state = state.value
            row.title = title
            row.summary = summary
            row.message_ids = message_ids
            row.missing_fields = missing_fields
            row.open_question = open_question
            self.session.commit()
        self.session.refresh(row)
        return row

    def get(self, candidate_id: str) -> CandidateRow | None:
        return self.session.get(CandidateRow, candidate_id)

    def get_by_key(self, conversation_id: str, candidate_key: str) -> CandidateRow | None:
        return self.session.scalars(
            select(CandidateRow).where(
                CandidateRow.conversation_id == conversation_id,
                CandidateRow.candidate_key == candidate_key,
            )
        ).first()

    def list_for_conversation(self, conversation_id: str) -> list[CandidateRow]:
        return list(
            self.session.scalars(
                select(CandidateRow)
                .where(CandidateRow.conversation_id == conversation_id)
                .order_by(CandidateRow.created_at)
            )
        )

    def list_by_state(self, state: CandidateState) -> list[CandidateRow]:
        return list(
            self.session.scalars(
                select(CandidateRow)
                .where(CandidateRow.state == state.value)
                .order_by(CandidateRow.created_at)
            )
        )

    def list_all(self) -> list[CandidateRow]:
        return list(self.session.scalars(select(CandidateRow).order_by(CandidateRow.created_at)))

    def claim_for_promotion(self, candidate_id: str) -> bool:
        """Atomically take a candidate out of READY. Returns True if we won it.

        FastAPI runs sync endpoints in a threadpool, so two concurrent sweeps can
        both read the same candidate as READY. The WHERE state='ready' guard means
        exactly one of them wins the row; the loser simply gets False and must not
        promote. Claiming happens BEFORE the task is created, so a loser never
        creates a duplicate task.
        """
        result = self.session.execute(
            update(CandidateRow)
            .where(
                CandidateRow.id == candidate_id,
                CandidateRow.state == CandidateState.READY.value,
            )
            .values(state=CandidateState.PROMOTED.value, updated_at=datetime.now(timezone.utc))
        )
        self.session.commit()
        return result.rowcount == 1

    def claim_for_triage(
        self, candidate_id: str, *, task_id: str, reason: str, confidence: float
    ) -> bool:
        """Park a candidate on an amendment decision. True if we won it.

        Same conditional-update discipline as claim_for_promotion, and for the
        same reason: two workers can read the same candidate as READY, and only
        one of them may attach a proposal to it.
        """
        result = self.session.execute(
            update(CandidateRow)
            .where(
                CandidateRow.id == candidate_id,
                CandidateRow.state == CandidateState.READY.value,
            )
            .values(
                state=CandidateState.AWAITING_TRIAGE.value,
                amends_task_id=task_id,
                amendment_reason=reason,
                amendment_confidence=confidence,
                updated_at=datetime.now(timezone.utc),
            )
        )
        self.session.commit()
        return result.rowcount == 1

    def claim_for_fold(self, candidate_id: str) -> bool:
        """Take a triaged candidate out of AWAITING_TRIAGE.

        It goes to PROMOTED, not to a state of its own: PROMOTED means "this
        candidate's request is now carried by a task", which is exactly true of
        a folded candidate — the task is simply one that already existed.
        """
        result = self.session.execute(
            update(CandidateRow)
            .where(
                CandidateRow.id == candidate_id,
                CandidateRow.state == CandidateState.AWAITING_TRIAGE.value,
            )
            .values(
                state=CandidateState.PROMOTED.value, updated_at=datetime.now(timezone.utc)
            )
        )
        self.session.commit()
        return result.rowcount == 1

    def attach_task(self, candidate_id: str, task_id: str) -> CandidateRow:
        """Record the task a claimed candidate produced."""
        row = self.session.get(CandidateRow, candidate_id)
        if row is None:
            raise KeyError(candidate_id)
        row.task_id = task_id
        self.session.commit()
        self.session.refresh(row)
        return row

    def return_to_triage(
        self,
        candidate_id: str,
        *,
        task_id: str | None = None,
        reason: str | None = None,
        confidence: float | None = None,
    ) -> bool:
        """Undo a claim whose fold then lost the race on the target task.

        PROMOTED is terminal for a candidate, so this is written as a direct
        state write rather than through ensure_transition: the candidate never
        actually became a task, and leaving it PROMOTED would hide a request
        nobody ever ran.

        The proposal fields are stamped when given, because the AUTOMATIC fold
        path reaches here through claim_for_promotion, which writes none of them.
        Without them the candidate landed back in the tray with amends_task_id
        NULL, which GET /triage renders as "an amendment to (task not found)
        (0% sure) — " and whose "Fold in" button 409s forever (orchestrator.fold
        returns None with no amends_task_id). Spec §3.8 says it returns to
        triage WITH a fresh proposal. The HUMAN fold path already carries these
        fields from claim_for_triage and passes nothing, leaving them untouched.
        """
        values: dict = {
            "state": CandidateState.AWAITING_TRIAGE.value,
            "updated_at": datetime.now(timezone.utc),
        }
        if task_id is not None:
            values["amends_task_id"] = task_id
        if reason is not None:
            values["amendment_reason"] = reason
        if confidence is not None:
            values["amendment_confidence"] = confidence
        result = self.session.execute(
            update(CandidateRow)
            .where(
                CandidateRow.id == candidate_id,
                CandidateRow.state == CandidateState.PROMOTED.value,
                CandidateRow.task_id.is_(None),
            )
            .values(**values)
        )
        self.session.commit()
        return result.rowcount == 1
