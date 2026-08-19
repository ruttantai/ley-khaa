from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AttachmentIn(BaseModel):
    kind: str
    name: str
    content: str


class MessageIn(BaseModel):
    source: str = "simulator"
    client: str = "demo"
    conversation_id: str = "conv-1"
    author: str = "user"
    text: str
    attachments: list[AttachmentIn] = []
    external_id: str | None = None


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
