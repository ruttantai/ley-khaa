"""Have I been asked this before? (spec §3.5)

Same two stages and same contract as the registry matcher: deterministic first,
one cheap call on a miss, no match always legal. A miss costs one interpreter
call — the path that worked before this module existed.
"""
from __future__ import annotations

import logging

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.memory_repository import MemoryRepository
from ..persistence.orm import MemoryRow
from .fingerprint import request_fingerprint
from .models import MemoryDecision

logger = logging.getLogger(__name__)

# Below this, the model's answer is not evidence. Pinned by a test: loosening it
# silently is how a cache starts serving confident wrong answers.
CONFIDENCE_FLOOR = 0.8

SYSTEM = """You decide whether a new request is a repeat of one already handled.

You are given the new request and a list of past requests with their ids. Answer with the id
of the past request that is THE SAME standing request — same work, same shape, differing only
in wording or in which day it is being run for. Otherwise answer null.

Say null unless you are confident. A wrong match reuses another request's specification and
the work is done to the wrong shape. A null costs only that the request is read fresh, which
is the normal path."""


class MemoryMatcher:
    def __init__(self, memories: MemoryRepository, llm: LLMClient) -> None:
        self.memories = memories
        self.llm = llm

    def recall(self, project: str, texts: list[str]) -> MemoryRow | None:
        try:
            return self._recall(project, texts)
        except Exception:
            logger.exception("memory recall failed; interpreting from scratch")
            return None

    def _recall(self, project: str, texts: list[str]) -> MemoryRow | None:
        fingerprint = request_fingerprint(texts)
        if not fingerprint:
            return None

        exact = self.memories.by_fingerprint(project, fingerprint)
        if exact is not None:
            return exact

        # Scoped to the project, always. A spec remembered for one client must
        # never be reachable from another's conversation.
        known = self.memories.for_project(project)
        if not known:
            return None

        decision = self.llm.parse(
            choice=model_for(Stage.MEMORY_MATCH),
            system=SYSTEM,
            user=_prompt(texts, known),
            output_format=MemoryDecision,
        )
        if not decision.memory_id or decision.confidence < CONFIDENCE_FLOOR:
            return None
        # Untrusted output: the id must be one we actually showed it.
        return next((row for row in known if row.id == decision.memory_id), None)


def _prompt(texts: list[str], known: list[MemoryRow]) -> str:
    lines = ["## New request", *texts, "", "## Past requests"]
    lines.extend(f"- [{row.id}] {row.intent} (seen {row.times_seen}x)" for row in known)
    return "\n".join(lines)
