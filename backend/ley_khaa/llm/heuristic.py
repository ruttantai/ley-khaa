import re
from typing import TypeVar

from pydantic import BaseModel

from ..crystallizer.engine import HANDLED_HEADER, CandidateDraft, CrystallizerOutput
from ..crystallizer.relevance import RelevanceVerdict
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
