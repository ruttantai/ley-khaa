from typing import Literal

from pydantic import BaseModel

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import CandidateRow
from .candidate import CandidateState
from .relevance import RelevanceVerdict

# Above this many messages in the window, the assembly problem stops being
# routine and we escalate off the cheap model.
_HARD_WINDOW = 10

SYSTEM = """You maintain the set of task candidates forming inside one work conversation.

You receive the recent messages and the candidates you previously reported. Return the
COMPLETE current set of candidates — reuse each candidate_key exactly so the caller can
match them to what it already stored.

Rules:
- A candidate owns ONLY the message ids that genuinely belong to it. Leave chatter
  unassigned; never pad a candidate with unrelated messages.
- Several unrelated requests may be interleaved. Emit one candidate per real request.
- state: "forming" when a request is only hinted at; "crystallizing" when it is taking
  shape but details are missing; "ready" when everything needed to act is present and no
  question is open.
- missing_fields: names of what is still unknown (e.g. output_format, deadline, source).
- open_question: one plain-English question to ask the human, or null. Only set it when
  the candidate is genuinely blocked."""


class CandidateDraft(BaseModel):
    candidate_key: str
    title: str
    summary: str
    message_ids: list[str]
    state: Literal["forming", "crystallizing", "ready"]
    missing_fields: list[str] = []
    open_question: str | None = None


class CrystallizerOutput(BaseModel):
    candidates: list[CandidateDraft]


class Crystallizer:
    """Stage B: the stateful candidate engine (spec §5.3)."""

    def __init__(
        self,
        llm: LLMClient,
        messages: MessageRepository,
        candidates: CandidateRepository,
        window_size: int = 30,
    ) -> None:
        self.llm = llm
        self.messages = messages
        self.candidates = candidates
        self.window_size = window_size

    def observe(self, conversation_id: str, verdict: RelevanceVerdict) -> list[CandidateRow]:
        # Stage A already decided this message is chatter — don't pay for Stage B.
        if not verdict.relevant:
            return []

        window = self.messages.window(conversation_id, limit=self.window_size)
        existing = self.candidates.list_for_conversation(conversation_id)
        complexity = "hard" if len(window) > _HARD_WINDOW else "routine"

        output = self.llm.parse(
            choice=model_for(Stage.CRYSTALLIZER, complexity),
            system=SYSTEM,
            user=_render(window, existing),
            output_format=CrystallizerOutput,
        )

        # PROMOTED/ABANDONED are terminal: the model will keep re-reporting a
        # candidate it already emitted, and resurrecting it would both raise on
        # the transition rules and double-create the task.
        terminal = {CandidateState.PROMOTED.value, CandidateState.ABANDONED.value}
        existing_by_key = {c.candidate_key: c for c in existing}

        rows = []
        for draft in output.candidates:
            prior = existing_by_key.get(draft.candidate_key)
            if prior is not None and prior.state in terminal:
                continue
            rows.append(
                self.candidates.upsert(
                    conversation_id=conversation_id,
                    candidate_key=draft.candidate_key,
                    title=draft.title,
                    summary=draft.summary,
                    state=CandidateState(draft.state),
                    message_ids=draft.message_ids,
                    missing_fields=draft.missing_fields,
                    open_question=draft.open_question,
                )
            )
        return rows


def _render(window, existing) -> str:
    lines = ["## Recent messages"]
    for row in window:
        lines.append(f"[{row.id}] {row.author}: {row.text}")
        for a in row.attachments or []:
            lines.append(f"    attachment: {a['kind']} named {a['name']}")

    lines.append("")
    lines.append("## Candidates you reported previously")
    if not existing:
        lines.append("(none yet)")
    for row in existing:
        lines.append(
            f"- {row.candidate_key} [{row.state}] {row.title} "
            f"owns={row.message_ids} missing={row.missing_fields}"
        )
    return "\n".join(lines)
