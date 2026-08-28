from pydantic import BaseModel


class MemoryDecision(BaseModel):
    """Stage 2's answer. `memory_id` is an id from the list the model was shown,
    or null — null is a first-class answer."""

    memory_id: str | None
    confidence: float
    reason: str
