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
from ..persistence.workflow_repository import WorkflowRepository
from ..registry.fingerprint import normalize_operation
from ..registry.matcher import RegistryMatcher
from ..registry.models import Match
from . import catalog
from .formats import deliverable_filename
from .resolver import ResolvedInput, UnreadImage, UnresolvedInputs, resolve_inputs
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


def _attempt_record(number: int, result: SandboxResult, verdict: Verdict, reasoning: str) -> dict:
    """One attempt's manifest entry. Shared by the cached lane and the synthesis
    loop so the two cannot drift apart on which keys a consumer can rely on —
    that drift is exactly how the cached lane's SandboxUnavailable handler once
    ended up crediting a model that was never called (it copied a dict literal
    instead of this)."""
    return {
        "attempt": number,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
        "ok": verdict.ok,
        "reason": verdict.reason,
        "checks": verdict.checks,
        "reasoning": reasoning,
        "stderr_tail": result.stderr[-_STDERR_IN_MANIFEST:],
    }


class ExecutionRunner:
    def __init__(
        self,
        *,
        llm: LLMClient,
        messages: MessageRepository,
        sandbox: SandboxRunner | None = None,
        workspace_root: Path | str | None = None,
        workflows: WorkflowRepository | None = None,
        extractor=None,
    ) -> None:
        self.synthesizer = Synthesizer(llm)
        self.messages = messages
        self._sandbox = sandbox
        self.workspace_root = Path(workspace_root or settings.workspace_root)
        # None is a supported configuration: without a registry the runner is
        # exactly the Phase 3 runner, which is what every existing test builds.
        self.workflows = workflows
        self.matcher = RegistryMatcher(workflows, llm) if workflows is not None else None
        # None is a supported configuration: without an extractor an image
        # attachment is ignored exactly as it was before phase 7.
        self.extractor = extractor

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
        # Captured before a cached attempt (if any) shifts first_attempt for the
        # synthesis fallback below. Both manifest writes past this point report
        # how many generator/attempt_*.py files belong to earlier ROUNDS, not to
        # this round's own lane switch — so both must use this same value.
        earlier = first_attempt - 1

        try:
            resolved, unread_images = resolve_inputs(spec, row, self.messages, extractor=self.extractor)
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
                earlier_attempts=earlier,
                # This is exactly the round where an unread image is most
                # likely to be the REASON nothing resolved (review B1) -- the
                # manifest must say so even though `resolved` is empty here.
                unread_images=exc.unread_images,
            )
            return ExecutionOutcome(verdict, str(workspace.root), 0)

        workspace.write_inputs(resolved)
        target = f"deliverable/{deliverable_filename(spec.output_format)}"

        match = self.matcher.match(spec, resolved) if self.matcher is not None else None
        # Bind under the workflow's role names when one matched, and under the
        # spec's own input names otherwise. input_hashes is computed AFTER this,
        # and again after any rewrite below: hashing a params.json that is about
        # to change makes the validator report the script tampered with its
        # inputs, which is both false and misleading.
        input_hashes = self._bind(workspace, spec, resolved, match, target)

        attempts: list[dict] = []
        workflow_record: dict | None = None
        verdict = Verdict(ok=False, reason=_SYNTHESIS_FAILED, checks={})

        if match is not None:
            number = first_attempt
            # Built before the sandbox even runs, and with quarantined=False:
            # if SandboxUnavailable strikes below, that is ley-khaa's daemon
            # dying, not the workflow's fault, and the handler needs a real
            # workflow_record (not the None a KeyError-shaped omission would
            # leave it with) to report lane="registry" honestly rather than
            # falling back to _write_manifest's synthesis-shaped defaults.
            workflow_record = {
                "name": match.workflow.name,
                "sha256": match.workflow.source_sha256,
                "matched_by": match.matched_by,
                "binding": dict(match.binding),
                "quarantined": False,
            }
            try:
                verdict, result = self._run_workflow(
                    workspace, match, number, spec, input_hashes
                )
            except SandboxUnavailable as exc:
                # Caught HERE, at the call site, rather than inside
                # _run_workflow: this is what lets _run_workflow stay a pure
                # "run and validate" helper with no manifest-writing concerns
                # of its own, while still writing one before the exception
                # continues up to the driver. Same reasoning as the synthesis
                # loop's identical handler below: clear_deliverables() already
                # ran, so without this an earlier round's manifest would keep
                # attesting deliverables that are no longer on disk. lane and
                # workflow are passed explicitly — the defaults on
                # _write_manifest describe the SYNTHESIS lane, and this failure
                # never reached synthesis at all.
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
                    earlier_attempts=earlier,
                    lane="registry",
                    workflow=workflow_record,
                    unread_images=unread_images,
                )
                raise
            attempts.append(
                _attempt_record(
                    number, result, verdict,
                    f"cached workflow {match.workflow.name}, no model call",
                )
            )
            workflow_record["quarantined"] = not verdict.ok
            if verdict.ok:
                self.workflows.record_success(
                    match.workflow.name,
                    # Only a phrasing the model found is worth learning; a
                    # fingerprint hit already knew this operation.
                    learned_alias=(
                        normalize_operation(spec.operation)
                        if match.matched_by == "model"
                        else None
                    ),
                )
                workspace.write_run_script(number)
                self._write_manifest(
                    workspace, row, spec, resolved=resolved, attempts=attempts,
                    verdict=verdict, earlier_attempts=earlier,
                    lane="registry", workflow=workflow_record,
                    unread_images=unread_images,
                )
                return ExecutionOutcome(verdict, str(workspace.root), len(attempts))

            # Proven code that just produced a wrong answer is not proven any
            # more. Quarantine it, re-bind under the spec's names, and let
            # synthesis rescue this run with its own full attempt budget.
            self.workflows.record_failure(match.workflow.name)
            first_attempt = number + 1
            input_hashes = self._bind(workspace, spec, resolved, None, target)
            # Discards the cached lane's verdict so the synthesis loop below
            # starts from the same "nothing has run yet" state it would if the
            # registry had never matched at all — the loop's own repair logic
            # reads `verdict` to decide synthesize-vs-repair, and the cached
            # script's failure has nothing to do with what synthesis is about
            # to write.
            verdict = Verdict(ok=False, reason=_SYNTHESIS_FAILED, checks={})

        previous: SynthesizedScript | None = None
        last: SandboxResult | None = None

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
                    earlier_attempts=earlier,
                    unread_images=unread_images,
                )
                raise
            verdict = validate(spec, workspace, result, input_hashes)
            attempts.append(_attempt_record(number, result, verdict, script.reasoning))
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
            earlier_attempts=earlier,
            # Always "synthesis": the only way to reach this line is either no
            # match at all, or a match that already returned above on success.
            # A quarantined match still ends up here having fallen all the way
            # through the synthesis loop, so the lane that produced whatever
            # verdict is being written really is synthesis.
            lane="synthesis",
            workflow=workflow_record,
            unread_images=unread_images,
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

    def _bind(
        self,
        workspace: Workspace,
        spec: TaskSpec,
        resolved: list[ResolvedInput],
        match: Match | None,
        target: str,
    ) -> dict[str, str]:
        """Write params.json for whichever lane is about to run, then hash.

        Returns the input hashes, so callers cannot forget to recompute them
        after a rewrite — the failure mode is a false "the script modified its
        inputs" verdict. A cached run binds under the workflow's role names; a
        synthesis run (including one rescuing a quarantined cache) binds under
        the spec's own input names, because that is what a freshly-synthesized
        script is written to expect.
        """
        binding = (
            dict(match.binding)
            if match is not None
            else {item.name: item.filename for item in resolved}
        )
        workspace.write_params(inputs=binding, output=target, seed=catalog.CATALOG_SEED)
        return workspace.input_hashes()

    def _run_workflow(
        self,
        workspace: Workspace,
        match: Match,
        number: int,
        spec: TaskSpec,
        input_hashes: dict[str, str],
    ) -> tuple[Verdict, SandboxResult]:
        """Run frozen, proven source. No model, no repair.

        There is deliberately no repair loop here: a cached script is proven
        code, so a failure means it is wrong for THIS request, and re-running it
        unchanged would fail identically. Repairing it would also mean the
        registry's source no longer matches what ran.

        SandboxUnavailable is deliberately NOT caught here: the call site (in
        run()) catches it instead, because writing the manifest before
        re-raising needs the lane and the workflow record, and this helper has
        neither — keeping both concerns split is what lets this stay a plain
        "run and validate" function.
        """
        path = workspace.write_generator(number, match.workflow.source)
        result = self.sandbox.run(
            script=path, workspace=workspace.root, timeout_s=settings.sandbox_timeout_seconds
        )
        return validate(spec, workspace, result, input_hashes), result

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
        lane: str = "synthesis",
        workflow: dict | None = None,
        unread_images: list[UnreadImage] | None = None,
    ) -> None:
        workspace.write_manifest(
            {
                "task_id": row.id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "lane": lane,
                "workflow": workflow,
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
                # picked. See _synthesis_author. On the cached lane no model
                # wrote this script, and the manifest may not imply one did —
                # unless synthesis went on to rescue the run after a quarantine,
                # in which case a model DID write the winning script, and this
                # is the same honesty rule crediting it, not an exception to it.
                "models": {
                    Stage.SYNTHESIS.value: (
                        None if lane == "registry" else _synthesis_author(self.synthesizer.llm)
                    )
                },
                "catalog_seed": catalog.CATALOG_SEED,
                "spec": spec.model_dump(mode="json"),
                "inputs": [
                    {
                        "name": i.name,
                        "file": i.filename,
                        "source": i.source,
                        "sha256": i.sha256,
                        # None (never "") for non-vision inputs: an empty
                        # string would read as "attested nothing", when the
                        # honest statement is "this input carries no image
                        # provenance to attest".
                        "extracted_from": i.extracted_from,
                        "extracted_by": i.extracted_by,
                    }
                    for i in resolved
                ],
                # An image that was supplied but never became a ResolvedInput
                # (review B2): unlike inputs above, this is not something the
                # script computed on, so it does not get a "source" or a
                # content hash -- it is a plain record that a picture was
                # carried and NOT read, naming which one, who tried, and what
                # they said about it. Empty when no image was supplied, or
                # every supplied image was read successfully.
                "images": [
                    {
                        "name": img.name,
                        "sha256": img.image_sha256,
                        "model": img.model,
                        "summary": img.summary,
                    }
                    for img in (unread_images or [])
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
