import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str
    client: str
    conversation_id: str
    author: str
    text: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
