"""Does a proven workflow already do this? (spec §3.3)

Two stages and a bind. The shape is the Crystallizer's: a free deterministic
filter first, one cheap model call only when that filter says nothing. No match
is always a legal answer — it costs a fall-through to synthesis, which is the
path that worked before this module existed.
"""
from __future__ import annotations

import logging

from ..executor.resolver import ResolvedInput
from ..interpreter.spec import TaskSpec
from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.workflow_repository import WorkflowRepository
from .binder import bind
from .fingerprint import fingerprint_candidates, normalize_operation
from .models import Match, RegistryDecision

logger = logging.getLogger(__name__)

# Below this, the model's answer is not evidence. Pinned by a test: loosening it
# silently is how a cache starts serving confident wrong answers.
CONFIDENCE_FLOOR = 0.8

SYSTEM = """You decide whether a data request can be served by an existing, proven workflow.

You are given one request and a list of workflows, each with a name, a description, and the
inputs and output format it expects. Answer with the name of the workflow that does EXACTLY
this job, or null.

Say null unless you are confident. A wrong match runs code that was proven for a different
job and produces a plausible, wrong answer. A null costs only that the script is written
fresh, which is the normal path. When in doubt, null."""


class RegistryMatcher:
    def __init__(self, workflows: WorkflowRepository, llm: LLMClient) -> None:
        self.workflows = workflows
        self.llm = llm

    def match(self, spec: TaskSpec, resolved: list[ResolvedInput]) -> Match | None:
        try:
            return self._match(spec, resolved)
        except Exception:
            # A cache that fails must cost only the work it was trying to save.
            logger.exception("registry matching failed; falling through to synthesis")
            return None

    def _match(self, spec: TaskSpec, resolved: list[ResolvedInput]) -> Match | None:
        active = self.workflows.active()
        if not active:
            return None

        for workflow in fingerprint_candidates(spec, active):
            binding = bind(workflow, resolved)
            if binding is not None:
                return Match(workflow=workflow, binding=binding, matched_by="fingerprint")

        decision = self.llm.parse(
            choice=model_for(Stage.REGISTRY_MATCH),
            system=SYSTEM,
            user=_prompt(spec, active),
            output_format=RegistryDecision,
        )
        if not decision.workflow or decision.confidence < CONFIDENCE_FLOOR:
            return None

        # The model names a workflow; it does not choose one. A hallucinated
        # name is the same untrusted-output problem the crystallizer already
        # learned, and active() is what keeps a quarantined row unreachable.
        chosen = next((w for w in active if w.name == decision.workflow), None)
        if chosen is None:
            logger.info("registry matcher named an unknown workflow %r", decision.workflow)
            return None

        binding = bind(chosen, resolved)
        if binding is None:
            return None
        return Match(workflow=chosen, binding=binding, matched_by="model")


def _prompt(spec: TaskSpec, workflows: list) -> str:
    lines = [
        "## Request",
        f"intent: {spec.intent}",
        f"operation: {normalize_operation(spec.operation)}",
        f"inputs: {', '.join(spec.inputs)}",
        f"output_format: {spec.output_format}",
        "",
        "## Available workflows",
    ]
    for workflow in workflows:
        roles = ", ".join(str(role.get("role")) for role in workflow.inputs or [])
        lines.append(
            f"- {workflow.name}: {workflow.description} "
            f"(inputs: {roles}; output: {workflow.output_format})"
        )
    return "\n".join(lines)
