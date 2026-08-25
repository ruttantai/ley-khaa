from pydantic import ValidationError

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import MessageRow, TaskRow
from .spec import TaskSpec

SYSTEM = """You turn a crystallized work request into a precise, executable specification.

You receive the messages that make up one request. Produce a single TaskSpec.

Rules:
- intent: one sentence describing what the human actually wants.
- operation: a short verb-phrase naming the transformation (e.g. set_difference,
  summary_stats, reconcile, extract). Invent one if nothing standard fits.
- inputs: the named data sources the work needs.
- output_format: the deliverable's format (xlsx, csv, docx, markdown, ...).
- recipient: who the result goes to, or null if the request does not say.
- urgency: low, normal, or high — read it from the conversation, do not guess high.
- missing_fields: name anything you genuinely cannot determine from the messages.
  Be honest here: this is what decides whether a human is asked before work starts.
  An empty list means the request is complete enough to act on.
- source_message_ids: only ids that appear in the messages you were given.
- certainty: your own confidence in this interpretation, 0.0 to 1.0."""

_RETRY_SUFFIX = """

Your previous response could not be parsed into the required schema. Return a
single, complete TaskSpec object with every required field present and correctly
typed. Do not include any commentary."""


class MalformedSpec(Exception):
    """The model could not produce a valid TaskSpec, even after a re-prompt."""


class Interpreter:
    """Crystallized request -> validated TaskSpec (spec §5.5)."""

    def __init__(self, llm: LLMClient, messages: MessageRepository) -> None:
        self.llm = llm
        self.messages = messages

    def interpret(self, task: TaskRow) -> TaskSpec:
        rows = self.messages.get_many(list(task.source_message_ids or []))
        user = _render(task, rows)

        try:
            spec = self._call(SYSTEM, user)
        except ValidationError:
            # Bad content, not a broken connection: one re-prompt with the schema
            # spelled out, then give up and let a human rescue it (§5.5).
            try:
                spec = self._call(SYSTEM + _RETRY_SUFFIX, user)
            except ValidationError as exc:
                raise MalformedSpec(str(exc)) from exc

        # Model-supplied ids are untrusted — the same lesson the crystallizer
        # learned. A hallucinated id must never reach the executor.
        known = {row.id for row in rows}
        return spec.model_copy(
            update={"source_message_ids": [m for m in spec.source_message_ids if m in known]}
        )

    def _call(self, system: str, user: str) -> TaskSpec:
        return self.llm.parse(
            choice=model_for(Stage.INTERPRETER),
            system=system,
            user=user,
            output_format=TaskSpec,
        )


def _render(task: TaskRow, rows: list[MessageRow]) -> str:
    lines = ["## Request", f"title: {task.title}", "", "## Messages"]
    for row in rows:
        lines.append(f"[{row.id}] {row.author}: {row.text}")
        for attachment in row.attachments or []:
            lines.append(f"    attachment: {attachment['kind']} named {attachment['name']}")
    return "\n".join(lines)
