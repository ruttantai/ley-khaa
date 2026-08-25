from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Urgency = Literal["low", "normal", "high"]


class TaskSpec(BaseModel):
    """The validated interpretation of a crystallized request (spec §5.5).

    `extra="forbid"` is deliberate: edit_spec merges a caller-supplied patch into
    this model, and a misspelled key must be a loud 422 rather than a silently
    discarded edit the human believes they made.
    """

    model_config = ConfigDict(extra="forbid")

    intent: str
    inputs: list[str] = Field(default_factory=list)
    operation: str
    output_format: str
    recipient: str | None = None
    urgency: Urgency = "normal"
    missing_fields: list[str] = Field(default_factory=list)
    source_message_ids: list[str] = Field(default_factory=list)
    # The model's own confidence in this interpretation. §5.7 names "interpreter
    # certainty" as an autonomy input but §5.5 gives it nowhere to live, so it
    # lives here.
    certainty: float = Field(ge=0.0, le=1.0)
