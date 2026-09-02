"""The structural test the whole-branch review found missing: nothing on this
branch drives a request through the REAL machinery with an OllamaLLM. Every
other Ollama test proves one unit in isolation (factory selection, the client
adapter, config parsing) -- the defects in the whole-branch review all live in
the seams between those units, which only an end-to-end run can catch.

Follows the pattern in test_vision_loop.py / test_runner.py: a REAL
ExecutionRunner driven against a real tmp_path workspace, with only the
sandbox and the model transport faked. The "model transport" here is a
recording double standing in for ollama.Client -- never a socket.
"""
import json

from ley_khaa.domain.models import Message
from ley_khaa.executor.runner import ExecutionRunner
from ley_khaa.executor.sandbox import SandboxResult
from ley_khaa.executor.workspace import MANIFEST_NAME
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.ollama import OllamaLLM
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository

_GENERATED_SCRIPT = """
import csv, json
with open('inputs/params.json') as f:
    params = json.load(f)
with open('deliverable/output.csv', 'w', newline='') as f:
    csv.writer(f).writerow(['ticker'])
    csv.writer(f).writerow(['SYN0000'])
print('ok')
"""


class _FakeDaemon:
    """Stands in for ollama.Client.chat -- records every call, never opens a
    socket. Always returns a well-formed SynthesizedScript payload."""

    def __init__(self):
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.dumps({"reasoning": "wrote a script", "source": _GENERATED_SCRIPT})
        return type("R", (), {"message": type("M", (), {"content": payload})()})()


class _WritesDeliverable:
    """Fake sandbox: ignores the generated script, writes a passing CSV --
    same shape as FakeSandbox in test_runner.py. This test is about the
    manifest, not about really running a local model's Python."""

    name = "fake"

    def run(self, *, script, workspace, timeout_s):
        (workspace / "deliverable" / "output.csv").write_text("ticker\nSYN0000\n")
        return SandboxResult(exit_code=0, stdout="ok", stderr="", duration_ms=1, timed_out=False)


def _task(session):
    messages = MessageRepository(session)
    row = messages.add(
        Message(
            source="slack", client="demo", conversation_id="conv-1",
            author="boss", text="compare the universes",
        )
    )
    created = TaskRepository(session).create(
        project="demo", title="compare", source_message_ids=[row.id]
    )
    return created, messages


def test_a_real_run_through_ollama_names_the_local_model_in_the_manifest(tmp_path, session):
    """The minimum bar: manifest["models"]["synthesis"] must name the local
    model that actually wrote the script, and must NEVER say "no model ran"
    -- that string is reserved for the two backends that truly run no model
    (heuristic, fake). Written before the C1 fix, this fails because
    _synthesis_author's `== "anthropic"` allowlist sends everything else,
    including a real local model, down the "no model ran" branch."""
    created, messages = _task(session)
    daemon = _FakeDaemon()
    llm = OllamaLLM(model="qwen2.5", client=daemon)
    runner = ExecutionRunner(
        llm=llm,
        messages=messages,
        sandbox=_WritesDeliverable(),
        workspace_root=tmp_path,
    )
    spec = TaskSpec(
        intent="compare the universes",
        inputs=["bloomberg universe", "factset"],
        operation="set_difference",
        output_format="csv",
        certainty=0.9,
    )

    outcome = runner.run(created, spec)

    assert outcome.verdict.ok, outcome.verdict.reason
    # The fake daemon really was called -- this is genuinely the Ollama path,
    # not a run that happened to succeed some other way.
    assert len(daemon.calls) == 1
    assert daemon.calls[0]["model"] == "qwen2.5"

    bundle_root = tmp_path / f"task-{created.id}"
    manifest = json.loads((bundle_root / MANIFEST_NAME).read_text())
    author = manifest["models"]["synthesis"]
    assert author == "ollama:qwen2.5", author
    assert "no model ran" not in author
