from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MessageIn(BaseModel):
    source: str = "simulator"
    client: str = "demo"
    conversation_id: str = "conv-1"
    author: str = "user"
    text: str


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project: str
    state: str
    title: str
    source_message_ids: list[str]
    created_at: datetime
    updated_at: datetime
