import uuid

from sqlalchemy import select
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
        self.session.commit()
        self.session.refresh(row)
        return row

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

    def mark_promoted(self, candidate_id: str, task_id: str) -> CandidateRow:
        row = self.session.get(CandidateRow, candidate_id)
        if row is None:
            raise KeyError(candidate_id)
        ensure_transition(CandidateState(row.state), CandidateState.PROMOTED)
        row.state = CandidateState.PROMOTED.value
        row.task_id = task_id
        self.session.commit()
        self.session.refresh(row)
        return row
