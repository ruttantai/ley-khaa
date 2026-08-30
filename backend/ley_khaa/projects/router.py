"""Which project does this request belong to? (spec §5.4, §3.5)

Two stages, same contract as RegistryMatcher and MemoryMatcher: a free
deterministic lookup first, one cheap model call only on a miss, the model's
answer treated as untrusted. Unlike those two, a miss here is not "no match" —
every task must land somewhere, so a miss routes to the default project.
"""
from __future__ import annotations

import logging

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.orm import ProjectRow
from ..persistence.project_repository import DEFAULT_PROJECT, ProjectRepository
from .models import ProjectChoice, RoutingDecision

logger = logging.getLogger(__name__)

# Below this the model's answer is not evidence. Same value and same reasoning
# as the registry and memory matchers; pinned by a test.
ROUTING_CONFIDENCE_FLOOR = 0.8

SYSTEM = """You decide which project a work request belongs to.

You are given one request and a list of projects, each with a name and a description of the
work it covers. Answer with the name of the project this request belongs to, or null.

Say null unless you are confident. A wrong project puts one client's work in another client's
queue, where the wrong people see it. A null costs only that the request goes to the default
project, where a human sorts it — which is the normal path."""


class ProjectRouter:
    def __init__(self, projects: ProjectRepository, llm: LLMClient) -> None:
        self.projects = projects
        self.llm = llm

    def route(
        self,
        *,
        source: str,
        client: str,
        conversation_id: str,
        title: str,
        summary: str,
    ) -> RoutingDecision:
        binding = self.projects.binding_for(source, client, conversation_id)
        if binding is not None:
            return RoutingDecision(
                project=binding.project,
                stage="binding",
                confidence=1.0,
                reason=f"bound to {binding.project} by {binding.created_by_stage}",
            )
        try:
            return self._classify(source, client, conversation_id, title, summary)
        except Exception:
            # Routing must never block intake. A misrouted task is recoverable
            # by a human; a request that never became a task is not.
            logger.exception("project routing failed; using the default project")
            return _fallback("routing failed")

    def _classify(
        self, source: str, client: str, conversation_id: str, title: str, summary: str
    ) -> RoutingDecision:
        # A project with no description gives the model nothing but a slug to
        # guess from, so it is unroutable by stage 2 by construction and
        # reachable only by an explicit binding. `default` is exactly such a row.
        candidates = [p for p in self.projects.active() if p.description.strip()]
        if not candidates:
            return _fallback("no described project to route into")

        choice = self.llm.parse(
            choice=model_for(Stage.PROJECT_ROUTE),
            system=SYSTEM,
            user=_prompt(title, summary, candidates),
            output_format=ProjectChoice,
        )
        if not choice.project or choice.confidence < ROUTING_CONFIDENCE_FLOOR:
            return _fallback(choice.reason or "no confident project match")

        # The model names a project; it does not choose one. Same untrusted-output
        # discipline as the registry matcher, and `candidates` is what keeps a
        # deactivated project unreachable.
        chosen = next((p for p in candidates if p.name == choice.project), None)
        if chosen is None:
            logger.info("project router named an unknown project %r", choice.project)
            return _fallback("routed to an unknown project")

        # The learning rule (spec §3.5): a confident stage-2 match binds THIS
        # conversation, so every later message in it routes free. It updates the
        # binding for the scope rather than accumulating rows — the asymmetry
        # backlog item 1 records memory getting wrong.
        self.projects.bind(source, client, conversation_id, chosen.name, stage="model")
        return RoutingDecision(
            project=chosen.name,
            stage="model",
            confidence=choice.confidence,
            reason=choice.reason,
        )


def _fallback(reason: str) -> RoutingDecision:
    return RoutingDecision(
        project=DEFAULT_PROJECT, stage="default", confidence=0.0, reason=reason
    )


def _prompt(title: str, summary: str, projects: list[ProjectRow]) -> str:
    lines = [
        "## Request",
        f"title: {title}",
        f"summary: {summary}",
        "",
        "## Projects",
    ]
    lines.extend(f"- {p.name}: {p.description}" for p in projects)
    return "\n".join(lines)
