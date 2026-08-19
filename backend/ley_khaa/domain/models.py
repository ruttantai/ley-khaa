import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class AttachmentKind(str, Enum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"


class Attachment(BaseModel):
    kind: AttachmentKind
    name: str
    # For TEXT/TABLE this is the literal content; for IMAGE it is a path or
    # base64 payload. Images are NOT interpreted at intake (spec §5.2).
    content: str


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str
    client: str
    conversation_id: str
    author: str
    text: str
    external_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
