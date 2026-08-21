from datetime import datetime, timezone
from typing import Any

from ..domain.models import Attachment, Message
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import MessageRow


class IntakeGateway:
    """Normalizes any inbound payload to a canonical Message and persists it.

    Adapters (simulator now; Slack/Discord later) hand raw dicts to this one
    door. Images are stored, never interpreted here (spec §5.2).
    """

    def __init__(self, repo: MessageRepository) -> None:
        self.repo = repo

    def accept(self, raw: dict[str, Any]) -> MessageRow:
        text = raw.get("text")
        if not text:
            raise ValueError("text is required")

        conversation_id = raw.get("conversation_id") or raw.get("thread_id") or "conv-1"
        attachments = [Attachment(**a) for a in raw.get("attachments", [])]

        raw_ts = raw.get("timestamp")
        timestamp = datetime.fromisoformat(raw_ts) if raw_ts else datetime.now(timezone.utc)

        message = Message(
            source=raw.get("source", "simulator"),
            client=raw.get("client", "demo"),
            conversation_id=conversation_id,
            author=raw.get("author", "user"),
            text=text,
            external_id=raw.get("external_id"),
            attachments=attachments,
            timestamp=timestamp,
            reply_to_task_id=raw.get("reply_to_task_id"),
        )
        return self.repo.add(message)
