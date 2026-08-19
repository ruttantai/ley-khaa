import re
from typing import TypeVar

from pydantic import BaseModel

from ..crystallizer.engine import CandidateDraft, CrystallizerOutput
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
        owned: list[str] = []
        title = "Untitled request"
        for line in user.splitlines():
            m = _MESSAGE_LINE.match(line)
            if not m:
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
                    candidate_key="heuristic-1",
                    title=title,
                    summary=title,
                    message_ids=owned,
                    state="ready",
                    missing_fields=[],
                    open_question=None,
                )
            ]
        )
