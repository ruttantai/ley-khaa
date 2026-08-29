from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, UniqueConstraint, text
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
    # Set when the task came from a crystallized candidate. The back-link exists
    # because the driver needs to read the candidate's readiness when scoring,
    # and CandidateRow.task_id only points the other way.
    candidate_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recommended_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    mode_override: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    autonomy_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    open_question: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    interpret_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    clarification_rounds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # The Output Bundle root (spec §5.11), surfaced by the dashboard.
    workspace_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # The serialized Verdict _execute produced and _validate acts on.
    execution_verdict: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Set when a spec came from memory rather than the interpreter. familiarity
    # is the remembered times_seen and feeds the autonomy dial; the task id is
    # what the dashboard links back to.
    remembered_from_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    familiarity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    @property
    def effective_mode(self) -> str | None:
        """The mode actually in force. Computed, never stored, so a human's
        override cannot go stale against a later re-score."""
        return self.mode_override or self.recommended_mode


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
    # Set when this message is a reply to an existing task rather than raw intake.
    # Intake routes such a message straight to that task and skips candidate
    # formation, so it can never spawn a duplicate candidate (spec §5.8).
    reply_to_task_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)


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


class WorkflowRow(Base):
    """A promoted, proven workflow — the registry's learned cache (spec §5.6).

    `source` is frozen: it is byte-for-byte the script that passed validation in
    the bundle named by promoted_from_task_id. Nothing rewrites it, which is why
    source_sha256 is meaningful.
    """

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    # server_default is text("''"), not the plain string "": Alembic's
    # autogenerate comparator renders a bare Python string default as
    # unquoted literal SQL, which for a non-empty string still ends up
    # matching the (quoted) reflected default after its own quote-stripping —
    # but for an empty string there is nothing left to strip, so raw ""
    # compares unequal to the DB's reflected `''` and false-positives as
    # drift. A pre-quoted text() default sidesteps that; the emitted DDL
    # (`DEFAULT ''`) is identical either way.
    description: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    # Normalized operation strings that match this workflow. Grows by one every
    # time the model matcher finds a phrasing that then passes validation.
    operation_aliases: Mapped[list] = mapped_column(JSON, default=list)
    output_format: Mapped[str] = mapped_column(String)
    # [{"role": "left", "suffixes": [".csv"]}], in the order the script expects.
    inputs: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String)
    source_sha256: Mapped[str] = mapped_column(String)
    # seed | promoted
    origin: Mapped[str] = mapped_column(String, default="promoted", server_default="promoted")
    promoted_from_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    runs_ok: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    runs_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set by a failed cached run. Blocks matching until a human clears it: a
    # workflow that just produced a wrong answer must not be handed the next
    # matching request as though nothing happened.
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class MemoryRow(Base):
    """A remembered request → the spec that satisfied it (spec §5.14).

    Written only for tasks that reached DONE with a passing verdict: the same
    "proven before it is cached" rule promotion follows.
    """

    __tablename__ = "task_memory"
    __table_args__ = (
        # record() is check-then-insert, and real concurrency exists today
        # even with a single project: FastAPI runs sync endpoints in a
        # threadpool, and the periodic sweeper runs alongside it — either
        # pairing can finish two identical requests' recordings at once.
        # Without this, that races two rows into existence, and every later
        # lookup for that (project, fingerprint) raises MultipleResultsFound
        # instead of returning the memory.
        UniqueConstraint("project", "fingerprint", name="uq_memory_per_project_fingerprint"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project: Mapped[str] = mapped_column(
        String, index=True, default="default", server_default="default"
    )
    fingerprint: Mapped[str] = mapped_column(String, index=True)
    # See WorkflowRow.description above for why this is text("''") and not "".
    intent: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    spec: Mapped[dict] = mapped_column(JSON)
    source_task_id: Mapped[str] = mapped_column(String)
    times_seen: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
