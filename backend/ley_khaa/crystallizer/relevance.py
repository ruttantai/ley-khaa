from pydantic import BaseModel, Field

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.orm import MessageRow

SYSTEM = """You triage messages from a work chat.

For each message decide whether it could contribute to an actionable work request
(data pulls, reconciliations, reports, analyses) or is conversational noise
(greetings, jokes, acknowledgements, scheduling chatter).

Assign a short kebab-case topic label. Reuse the same label for the same subject.
Be generous: a fragment that only makes sense with earlier messages is still relevant."""


class RelevanceVerdict(BaseModel):
    relevant: bool
    topic: str
    confidence: float = Field(ge=0.0, le=1.0)


class RelevanceFilter:
    """Stage A: cheap per-message pruning before the expensive stateful pass."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def judge(self, row: MessageRow) -> RelevanceVerdict:
        return self.llm.parse(
            choice=model_for(Stage.RELEVANCE_FILTER),
            system=SYSTEM,
            user=_render(row),
            output_format=RelevanceVerdict,
        )


def _render(row: MessageRow) -> str:
    lines = [f"author: {row.author}", f"text: {row.text}"]
    for a in row.attachments or []:
        lines.append(f"attachment: {a['kind']} named {a['name']}")
    return "\n".join(lines)
