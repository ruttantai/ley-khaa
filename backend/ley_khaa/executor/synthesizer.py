"""Turn a TaskSpec plus real inputs into one Python script (spec §5.10).

This is the default lane. The registry fast path is Phase 4; until then every
task that gets this far is solved by a program written for it.
"""
from __future__ import annotations

from pydantic import BaseModel

from ..interpreter.spec import TaskSpec
from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from .formats import deliverable_filename
from .resolver import ResolvedInput
from .sandbox import SandboxResult
from .validator import Verdict

# How much of each input the model sees. Enough to learn the real column names
# without pasting a 200-row universe into the prompt.
_PREVIEW_LINES = 8
# A crashing script can emit megabytes of stderr. The tail is the part that
# carries the exception.
_MAX_STDERR = 4000


class SynthesizedScript(BaseModel):
    """What the model returns. `reasoning` is kept in the manifest so a reader
    can see why the script is shaped the way it is."""

    reasoning: str
    source: str


SYSTEM = """You write a single, self-contained Python script that solves one data task.

The script runs in a locked-down sandbox:
- Working directory contains inputs/ (read these) and deliverable/ (write here).
- Available libraries: the Python 3.12 standard library, openpyxl, and python-docx.
  pandas and numpy are NOT installed. Use the csv module.
- There is no network. Do not import requests, urllib, or anything that dials out.
- Do not read or write anything outside deliverable/. Never modify inputs/.
- Be deterministic: no randomness, no timestamps in the output, no reliance on
  dict ordering that the input does not guarantee. The same inputs must produce
  the same bytes every run — the whole bundle is audited on that.
- Print one short human-readable summary line at the end.
- Write exactly one deliverable file, at the path given in the task.

Return the complete script in `source` and one or two sentences in `reasoning`
about the approach. No markdown fences, no commentary outside those fields."""


def _preview(item: ResolvedInput) -> str:
    lines = item.content.splitlines()[:_PREVIEW_LINES]
    body = "\n".join(lines)
    return f"### inputs/{item.filename}  (spec input: {item.name}, source: {item.source})\n{body}"


def _task_block(spec: TaskSpec, resolved: list[ResolvedInput]) -> str:
    target = f"deliverable/{deliverable_filename(spec.output_format)}"
    previews = "\n\n".join(_preview(item) for item in resolved)
    return (
        f"## Task\n"
        f"intent: {spec.intent}\n"
        f"operation: {spec.operation}\n"
        f"output_format: {spec.output_format}\n"
        f"write the result to: {target}\n"
        f"\n## Inputs (first {_PREVIEW_LINES} lines of each)\n{previews}\n"
    )


class Synthesizer:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def _parse(self, user: str) -> SynthesizedScript:
        return self.llm.parse(
            choice=model_for(Stage.SYNTHESIS),
            system=SYSTEM,
            user=user,
            output_format=SynthesizedScript,
        )

    def synthesize(self, spec: TaskSpec, resolved: list[ResolvedInput]) -> SynthesizedScript:
        return self._parse(_task_block(spec, resolved))

    def repair(
        self,
        spec: TaskSpec,
        resolved: list[ResolvedInput],
        *,
        previous: str,
        result: SandboxResult,
        verdict: Verdict,
    ) -> SynthesizedScript:
        """One more go, given what went wrong.

        The traceback is information the model can act on and a human cannot,
        which is why this happens before anyone is asked a question.
        """
        stderr = result.stderr[-_MAX_STDERR:]
        user = (
            f"{_task_block(spec, resolved)}\n"
            f"## Your previous attempt failed\n"
            f"verdict: {verdict.reason}\n"
            f"exit code: {result.exit_code}"
            f"{' (killed on timeout)' if result.timed_out else ''}\n"
            f"\n### previous source\n{previous}\n"
            f"\n### stderr (last {_MAX_STDERR} chars)\n{stderr}\n"
            f"\nFix the cause and return the complete corrected script."
        )
        return self._parse(user)
