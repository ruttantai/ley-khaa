from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AttachmentIn(BaseModel):
    kind: str
    name: str
    content: str


class MessageIn(BaseModel):
    source: str = "simulator"
    client: str = "demo"
    conversation_id: str = "conv-1"
    author: str = "user"
    # Validated here so an empty body is a 422 from the schema rather than a bare
    # ValueError out of the intake gateway, which surfaced as a 500.
    text: str = Field(min_length=1)
    attachments: list[AttachmentIn] = []
    external_id: str | None = None
    reply_to_task_id: str | None = None

    @field_validator("text")
    @classmethod
    def _reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class IntakeOut(BaseModel):
    message_id: str
    conversation_id: str
    candidate_ids: list[str]
    task_ids: list[str]


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    candidate_key: str
    title: str
    summary: str
    state: str
    message_ids: list[str]
    missing_fields: list[str]
    open_question: str | None
    task_id: str | None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    author: str
    text: str
    timestamp: datetime


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project: str
    state: str
    title: str
    source_message_ids: list[str]
    created_at: datetime
    updated_at: datetime
    candidate_id: str | None = None
    spec: dict[str, Any] | None = None
    recommended_mode: str | None = None
    mode_override: str | None = None
    # Computed on TaskRow, never stored: the override wins if set, otherwise the
    # recommendation stands.
    effective_mode: str | None = None
    confidence: float | None = None
    risk: float | None = None
    autonomy_reason: str | None = None
    open_question: str | None = None
    failure_reason: str | None = None
    # The Output Bundle root on disk (spec §5.11), and what the run came to.
    workspace_path: str | None = None
    execution_verdict: dict[str, Any] | None = None


class RejectIn(BaseModel):
    reason: str = "rejected by the human"


class ModeIn(BaseModel):
    # None clears the pin and falls back to the engine's recommendation.
    mode: Literal["suggest", "copilot", "auto"] | None = None


class SpecPatchIn(BaseModel):
    patch: dict[str, Any]


class AnswerIn(BaseModel):
    text: str = Field(min_length=1)
    author: str = "human"

    @field_validator("text")
    @classmethod
    def _reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class BundleOut(BaseModel):
    task_id: str
    root: str
    manifest: dict[str, Any]
    # Every file in the bundle, as paths relative to the root, so the dashboard
    # can hand them straight back to the file endpoint.
    files: list[str]
    deliverables: list[str]
