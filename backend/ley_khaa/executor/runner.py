"""The whole execution lane, in one place (spec §3, decision 5).

    resolve inputs -> build workspace -> synthesize -> sandbox run -> validate
                                              ^______ repair once ______|

run() never raises for a business failure. It returns a Verdict, and the driver
turns that into a state. The one exception is SandboxUnavailable: a dead daemon
is ley-khaa failing, not the request failing, and the two must not be confused.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..interpreter.spec import TaskSpec
from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import TaskRow
from . import catalog
from .formats import deliverable_filename
from .resolver import ResolvedInput, UnresolvedInputs, resolve_inputs
from .sandbox import SandboxRunner, SandboxResult, SandboxUnavailable, pick_sandbox
from .synthesizer import SynthesizedScript, Synthesizer
from .validator import Verdict, validate
from .workspace import Workspace, sha256_file

logger = logging.getLogger(__name__)

# One synthesis, one repair. Decision 5: then a human, who can see the whole
# bundle, decides — rather than a loop that keeps paying Opus to guess.
_MAX_ATTEMPTS = 2

# Kept out of the escalation text a human reads; the full stderr is in the bundle.
_STDERR_IN_MANIFEST = 2000

_SYNTHESIS_FAILED = "I could not produce a working script for this request."


@dataclass(frozen=True)
class ExecutionOutcome:
    verdict: Verdict
    workspace_path: str
    attempts: int


def _synthesis_author(llm: LLMClient) -> str:
    """Who actually wrote the script, for the manifest.

    The router's model id is the truth only when a real model ran. With no
    ANTHROPIC_API_KEY the offline stand-in writes a canned script, and stamping
    "claude-opus-5" on that tells a reader they are looking at model output —
    the exact confusion llm/factory.py logs a warning about when it falls back.
    The generator's own docstring says it was written offline, so a manifest
    naming a model would also contradict the very file it describes.
    """
    if llm.name == "anthropic":
        return model_for(Stage.SYNTHESIS).model
    return f"{llm.name} (no model ran)"


class ExecutionRunner:
    def __init__(
        self,
        *,
        llm: LLMClient,
        messages: MessageRepository,
        sandbox: SandboxRunner | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.synthesizer = Synthesizer(llm)
        self.messages = messages
        self._sandbox = sandbox
        self.workspace_root = Path(workspace_root or settings.workspace_root)

    @property
    def sandbox(self) -> SandboxRunner:
        """Resolved on first use, never in __init__.

        A TaskDriver is built per HTTP request, and pick_sandbox() shells out to
        probe the Docker daemon — doing that for every request, including the
        many that execute nothing, would be a subprocess per page load.
        """
        if self._sandbox is None:
            self._sandbox = pick_sandbox()
        return self._sandbox

    def run(self, row: TaskRow, spec: TaskSpec) -> ExecutionOutcome:
        workspace = Workspace.create(self.workspace_root, row.id)
        # A task can reach here more than once: a failed validation escalates,
        # the human answers, and the task is driven back through CLASSIFIED into
        # this SAME bundle. Anything the previous round left in deliverable/ is
        # not this run's output, and the validator judges the alphabetically
        # first file it finds — so an old output.csv would be measured against a
        # spec that now asks for excel and reject a run that got it right.
        # Generator attempts are NOT cleared: they are the audit trail, and
        # next_attempt_number() keeps this round from overwriting the last one's.
        workspace.clear_deliverables()
        first_attempt = workspace.next_attempt_number()

        try:
            resolved = resolve_inputs(spec, row, self.messages)
        except UnresolvedInputs as exc:
            # Returned, not raised: EXECUTING -> NEEDS_CLARIFICATION is not a
            # legal edge, so the question reaches the human through _validate.
            verdict = Verdict(
                ok=False,
                reason=(
                    "I could not find the data for: "
                    + ", ".join(exc.names)
                    + ". Can you attach it, or tell me which dataset to use?"
                ),
                checks={"inputs_resolved": False},
            )
            self._write_manifest(
                workspace,
                row,
                spec,
                resolved=[],
                attempts=[],
                verdict=verdict,
                earlier_attempts=first_attempt - 1,
            )
            return ExecutionOutcome(verdict, str(workspace.root), 0)

        workspace.write_inputs(resolved)
        workspace.write_params(
            inputs={item.name: item.filename for item in resolved},
            output=f"deliverable/{deliverable_filename(spec.output_format)}",
            seed=catalog.CATALOG_SEED,
        )
        input_hashes = workspace.input_hashes()

        attempts: list[dict] = []
        previous: SynthesizedScript | None = None
        last: SandboxResult | None = None
        verdict = Verdict(ok=False, reason=_SYNTHESIS_FAILED, checks={})

        for number in range(first_attempt, first_attempt + _MAX_ATTEMPTS):
            try:
                script = self._write_attempt(spec, resolved, previous, last, verdict)
            except _NoScript as exc:
                # previous/last stay as they were, so the next pass is a plain
                # retry when nothing has run yet and a repair when something has.
                # "ok" is always present, same as a real attempt record — a
                # consumer reading attempt["ok"] must never KeyError on this.
                attempts.append({"attempt": number, "ok": False, "error": str(exc)})
                verdict = Verdict(
                    ok=False, reason=_SYNTHESIS_FAILED, checks={"synthesis_produced_a_script": False}
                )
                continue

            path = workspace.write_generator(number, script.source)
            try:
                result = self.sandbox.run(
                    script=path,
                    workspace=workspace.root,
                    timeout_s=settings.sandbox_timeout_seconds,
                )
            except SandboxUnavailable as exc:
                # clear_deliverables() already ran, so an earlier round's
                # manifest is now describing a deliverable that is no longer on
                # disk. Record the infrastructure failure before letting this
                # propagate, or the bundle keeps attesting bytes it no longer
                # holds — the one thing a manifest may never do.
                self._write_manifest(
                    workspace,
                    row,
                    spec,
                    resolved=resolved,
                    attempts=attempts,
                    verdict=Verdict(
                        ok=False,
                        reason=f"The sandbox was unavailable: {exc}",
                        checks={"sandbox_available": False},
                    ),
                    earlier_attempts=first_attempt - 1,
                )
                raise
            verdict = validate(spec, workspace, result, input_hashes)
            attempts.append(
                {
                    "attempt": number,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "timed_out": result.timed_out,
                    "ok": verdict.ok,
                    "reason": verdict.reason,
                    "checks": verdict.checks,
                    "reasoning": script.reasoning,
                    "stderr_tail": result.stderr[-_STDERR_IN_MANIFEST:],
                }
            )
            previous, last = script, result
            if verdict.ok:
                workspace.write_run_script(number)
                break

        self._write_manifest(
            workspace,
            row,
            spec,
            resolved=resolved,
            attempts=attempts,
            verdict=verdict,
            earlier_attempts=first_attempt - 1,
        )
        return ExecutionOutcome(verdict, str(workspace.root), len(attempts))

    def _write_attempt(
        self,
        spec: TaskSpec,
        resolved: list[ResolvedInput],
        previous: SynthesizedScript | None,
        last: SandboxResult | None,
        verdict: Verdict,
    ) -> SynthesizedScript:
        try:
            script = (
                self.synthesizer.synthesize(spec, resolved)
                if previous is None or last is None
                else self.synthesizer.repair(
                    spec, resolved, previous=previous.source, result=last, verdict=verdict
                )
            )
        except Exception as exc:  # transport, refusal, malformed output
            logger.exception("synthesis call failed")
            raise _NoScript(f"{type(exc).__name__}: {exc}") from exc
        if not script.source.strip():
            raise _NoScript("the model returned an empty script")
        return script

    def _write_manifest(
        self,
        workspace: Workspace,
        row: TaskRow,
        spec: TaskSpec,
        *,
        resolved: list[ResolvedInput],
        attempts: list[dict],
        verdict: Verdict,
        earlier_attempts: int = 0,
    ) -> None:
        workspace.write_manifest(
            {
                "task_id": row.id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "lane": "synthesis",
                # The sandbox that ACTUALLY ran, never the one we hoped for —
                # and never resolved just to fill in this field. Reads the
                # backing field, not the lazy property: the unresolved-inputs
                # path calls this with attempts == [] and no sandbox touched,
                # and probing pick_sandbox() here would defeat the property's
                # whole reason for being lazy. An attempts list that is all
                # _NoScript entries (synthesis failed every time) also leaves
                # _sandbox unset, so this must not assume "attempts" implies
                # "a sandbox ran".
                "sandbox": self._sandbox.name if self._sandbox is not None else None,
                # Same rule as "sandbox" above, for the same reason: what
                # actually wrote the script, never what the router would have
                # picked. See _synthesis_author.
                "models": {Stage.SYNTHESIS.value: _synthesis_author(self.synthesizer.llm)},
                "catalog_seed": catalog.CATALOG_SEED,
                "spec": spec.model_dump(mode="json"),
                "inputs": [
                    {"name": i.name, "file": i.filename, "source": i.source, "sha256": i.sha256}
                    for i in resolved
                ],
                "attempts": attempts,
                # Only THIS round's attempts are listed above, and their numbers
                # continue from where the last round stopped. This says how many
                # generator/attempt_*.py files belong to earlier rounds, so a
                # reader is not left wondering why the list starts at 3.
                "earlier_attempts": earlier_attempts,
                "verdict": {
                    "ok": verdict.ok,
                    "reason": verdict.reason,
                    "checks": verdict.checks,
                },
                "deliverables": [
                    {"file": p.name, "sha256": sha256_file(p)} for p in workspace.deliverables()
                ],
                # An .xlsx is a zip that embeds timestamps, so re-running the
                # generator reproduces the VALUES, not the bytes. Saying so here
                # keeps the bundle from implying a claim it cannot support.
                "reproducibility": "cell values for .xlsx; bytes for csv, json and text",
            }
        )


class _NoScript(Exception):
    """This attempt produced nothing runnable. Internal to the loop above."""
