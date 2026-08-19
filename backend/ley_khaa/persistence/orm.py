from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project: Mapped[str] = mapped_column(String, default="default")
    state: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, default="")
    source_message_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True, unique=True)
    source: Mapped[str] = mapped_column(String)
    client: Mapped[str] = mapped_column(String)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    author: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(String)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Stage A's verdict, persisted so stage B can actually prune known noise from
    # its window. NULL means "not judged yet" and is treated as relevant.
    relevant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    topic: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class CandidateRow(Base):
    __tablename__ = "task_candidates"
    __table_args__ = (
        UniqueConstraint("conversation_id", "candidate_key", name="uq_candidate_per_conversation"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    # Stable key the crystallizer reuses to re-identify a candidate across turns.
    candidate_key: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    summary: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String)
    message_ids: Mapped[list] = mapped_column(JSON, default=list)
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    open_question: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
