from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..domain.models import Message
from .orm import MessageRow


class MessageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, message: Message) -> MessageRow:
        # Idempotent per external id so channel retries never duplicate (spec §5.2).
        if message.external_id is not None:
            existing = self.session.scalars(
                select(MessageRow).where(MessageRow.external_id == message.external_id)
            ).first()
            if existing is not None:
                return existing
        row = MessageRow(
            id=message.id,
            external_id=message.external_id,
            source=message.source,
            client=message.client,
            conversation_id=message.conversation_id,
            author=message.author,
            text=message.text,
            attachments=[a.model_dump(mode="json") for a in message.attachments],
            timestamp=message.timestamp,
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError:
            # Race: another request inserted the same external_id after our check.
            self.session.rollback()
            if message.external_id is not None:
                existing = self.session.scalars(
                    select(MessageRow).where(MessageRow.external_id == message.external_id)
                ).first()
                if existing is not None:
                    return existing
            # If external_id was None or still not found, re-raise the integrity error
            # (should not happen in normal operation).
            raise
        self.session.refresh(row)
        return row

    def list_for_conversation(self, conversation_id: str) -> list[MessageRow]:
        return list(
            self.session.scalars(
                select(MessageRow)
                .where(MessageRow.conversation_id == conversation_id)
                .order_by(MessageRow.timestamp, MessageRow.id)
            )
        )

    def window(self, conversation_id: str, limit: int = 30) -> list[MessageRow]:
        """The most recent `limit` messages, oldest-first."""
        return self.list_for_conversation(conversation_id)[-limit:]

    def last_timestamp(self, conversation_id: str) -> datetime | None:
        rows = self.list_for_conversation(conversation_id)
        return rows[-1].timestamp if rows else None
