import re
from typing import TypeVar

from pydantic import BaseModel

from ..crystallizer.engine import HANDLED_HEADER, CandidateDraft, CrystallizerOutput
from ..crystallizer.relevance import RelevanceVerdict
from ..interpreter.spec import TaskSpec
from .router import ModelChoice

T = TypeVar("T", bound=BaseModel)

_REQUEST_WORDS = (
    "compare", "reconcile", "send", "pull", "report", "check", "build",
    "export", "summar", "difference", "missing", "list", "generate",
)
_NOISE_PATTERNS = (
    r"^\s*(hi|hey|hello|morning|thanks|thank you|ok|okay|cool|nice|lol|haha|sure)\b",
    r"^\s*\W*$",
)

_MESSAGE_LINE = re.compile(r"^\[(?P<id>[^\]]+)\]\s+(?P<author>[^:]+):\s*(?P<text>.*)$")
_OWNS = re.compile(r"owns=\[(?P<ids>[^\]]*)\]")

_OPERATIONS = (
    (("compare", "difference", "missing", "reconcile", "against"), "set_difference"),
    (("summar", "stats", "group by", "breakdown", "average"), "summary_stats"),
)
_FORMATS = (
    (("excel", "xlsx", "spreadsheet"), "xlsx"),
    (("csv",), "csv"),
    (("word", "docx"), "docx"),
    (("markdown", "md"), "markdown"),
)
_SOURCE_WORDS = ("bloomberg", "factset", "holdings", "universe", "portfolio", "trades")
_URGENT_WORDS = ("urgent", "asap", "right away", "eod", "immediately")
_RECIPIENT = re.compile(r"send (?:it |them |this )?to (?P<who>[a-z][\w.-]*)")

# Deliberately mediocre: a regex has not understood anything, and the autonomy
# engine must never hand Auto to keyword matching. See the threshold in
# ley_khaa/autonomy/engine.py.
_HEURISTIC_CERTAINTY = 0.55


class HeuristicLLM:
    """Deterministic, offline stand-in for a model.

    Keeps the pipeline runnable with no API key (fresh-clone demo, CI). It is
    intentionally dumb: real quality comes from AnthropicLLM.
    """

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        if output_format is RelevanceVerdict:
            return self._relevance(user)
        if output_format is CrystallizerOutput:
            return self._crystallize(user)
        if output_format is TaskSpec:
            return self._interpret(user)
        raise NotImplementedError(f"HeuristicLLM has no rule for {output_format.__name__}")

    def _relevance(self, user: str) -> RelevanceVerdict:
        text = ""
        for line in user.splitlines():
            if line.startswith("text: "):
                text = line[len("text: ") :]
        lowered = text.lower()
        if any(re.search(p, lowered) for p in _NOISE_PATTERNS):
            return RelevanceVerdict(relevant=False, topic="chatter", confidence=0.6)
        relevant = any(w in lowered for w in _REQUEST_WORDS)
        return RelevanceVerdict(
            relevant=relevant,
            topic="work-request" if relevant else "chatter",
            confidence=0.6,
        )

    def _crystallize(self, user: str) -> CrystallizerOutput:
        handled = _handled_message_ids(user)
        owned: list[str] = []
        title = "Untitled request"
        for line in user.splitlines():
            m = _MESSAGE_LINE.match(line)
            if not m:
                continue
            # Messages belonging to an already-handled candidate are retired: the
            # prompt says not to report them again, and re-reporting them is what
            # made every later request in a conversation vanish.
            if m.group("id") in handled:
                continue
            text = m.group("text")
            lowered = text.lower()
            if any(re.search(p, lowered) for p in _NOISE_PATTERNS):
                continue
            if not any(w in lowered for w in _REQUEST_WORDS):
                continue
            owned.append(m.group("id"))
            if title == "Untitled request":
                title = text[:80]
        if not owned:
            return CrystallizerOutput(candidates=[])
        return CrystallizerOutput(
            candidates=[
                CandidateDraft(
                    # Keyed off the first message the candidate owns: stable while
                    # the same request keeps accumulating follow-ups, and different
                    # once a genuinely new request starts. A constant key meant a
                    # conversation could only ever produce one task.
                    candidate_key=f"heuristic-{owned[0]}",
                    title=title,
                    summary=title,
                    message_ids=owned,
                    state="ready",
                    missing_fields=[],
                    open_question=None,
                )
            ]
        )

    def _interpret(self, user: str) -> TaskSpec:
        ids: list[str] = []
        texts: list[str] = []
        for line in user.splitlines():
            m = _MESSAGE_LINE.match(line)
            if not m:
                continue
            ids.append(m.group("id"))
            texts.append(m.group("text"))
        blob = " ".join(texts).lower()

        operation = _first_match(_OPERATIONS, blob, default="synthesize")
        output_format = _first_match(_FORMATS, blob, default="")
        inputs = [word for word in _SOURCE_WORDS if word in blob]

        recipient = None
        match = _RECIPIENT.search(blob)
        if match:
            recipient = match.group("who")
        elif "send me" in blob:
            recipient = "the requester"

        missing = []
        if not output_format:
            missing.append("output_format")
        if not inputs:
            missing.append("inputs")

        return TaskSpec(
            intent=texts[0] if texts else "unknown request",
            inputs=inputs,
            operation=operation,
            output_format=output_format or "unknown",
            recipient=recipient,
            urgency="high" if any(w in blob for w in _URGENT_WORDS) else "normal",
            missing_fields=missing,
            source_message_ids=ids,
            certainty=_HEURISTIC_CERTAINTY,
        )


def _handled_message_ids(user: str) -> set[str]:
    """Message ids the prompt marks as belonging to already-handled candidates."""
    ids: set[str] = set()
    in_handled = False
    for line in user.splitlines():
        if line.startswith("## "):
            in_handled = line.startswith(HANDLED_HEADER)
            continue
        if not in_handled:
            continue
        m = _OWNS.search(line)
        if m:
            ids.update(part.strip() for part in m.group("ids").split(",") if part.strip())
    return ids


def _first_match(table, blob: str, *, default: str) -> str:
    for words, value in table:
        if any(word in blob for word in words):
            return value
    return default
