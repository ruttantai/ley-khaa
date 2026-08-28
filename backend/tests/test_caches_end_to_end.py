"""§9: the same request, asked twice, is free the second time.

Nothing is mocked except the count of model calls. Memory skips the interpreter,
the registry skips synthesis, and the two together mean the second run makes no
model call at all. If this test ever needs a mock to pass, the caches are not
actually chaining.
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ley_khaa.domain.states import TaskState
from ley_khaa.llm.client import LLMClient
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.memory_repository import MemoryRepository
from ley_khaa.persistence.workflow_repository import WorkflowRepository

from .test_driver_memory import _make_task

# Load-bearing wording, not decorative: "summar" reaches summary_stats before
# "compare/difference/missing/reconcile/against" (set_difference) ever gets a
# look, "csv" is unambiguous, and "holdings" is a real, unique catalog dataset
# — so the resolver binds it instead of asking a clarifying question. csv, not
# xlsx: an .xlsx is a zip embedding a timestamp and cannot be byte-reproducible.
TEXT = "summarise the holdings as csv"


@dataclass
class CountingLLM:
    """A real LLM client wrapped to count calls, never to change an answer.

    FakeLLM (used elsewhere) is a queue of canned responses — using it here
    would mean *we* decided what the interpreter "found," not the interpreter.
    This instead runs the real, offline HeuristicLLM and only watches how many
    times it was asked anything. A zero-calls assertion built on this means the
    interpreter/registry/synthesis machinery genuinely never ran — not that a
    stub reported zero regardless of what happened.
    """

    inner: LLMClient
    calls: list[Any] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.inner.name

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.inner.parse(**kwargs)


def _driver(session, repo, messages, llm: LLMClient) -> TaskDriver:
    """The full chain wired together: registry AND memory on the same driver —
    exactly how app.py's build_orchestrator wires the real one."""
    return TaskDriver(
        repo,
        llm=llm,
        messages=messages,
        candidates=CandidateRepository(session),
        workflows=WorkflowRepository(session),
        memories=MemoryRepository(session),
    )


def _run_to_done(session, text, conversation_id):
    """Post one request (a message + task, mirroring _make_task's helper for
    the memory tests) and drive it all the way to done."""
    repo, messages, task = _make_task(session, text, conversation_id=conversation_id)
    llm = CountingLLM(HeuristicLLM())
    driver = _driver(session, repo, messages, llm)
    driver.advance(task.id)
    result = driver.approve(task.id)
    return result, llm


def test_the_second_identical_request_makes_no_model_calls(seeded_registry):
    session = seeded_registry

    first, first_llm = _run_to_done(session, TEXT, conversation_id="conv-1")
    assert first.state == TaskState.DONE.value, first.execution_verdict
    assert first_llm.calls, "the first run must actually call the model at least once"

    # A brand new conversation: a different message, a different task, its own
    # fresh CountingLLM — so its (empty) .calls list can only be explained by
    # this run genuinely making no call, not by carrying over the first run's.
    second, second_llm = _run_to_done(session, TEXT, conversation_id="conv-2")

    assert second.state == TaskState.DONE.value, second.execution_verdict
    assert second_llm.calls == []

    manifest = json.loads((Path(second.workspace_path) / "manifest.json").read_text())
    assert manifest["lane"] == "registry"
    assert second.remembered_from_task_id == first.id


def test_the_second_run_produces_byte_identical_output(seeded_registry):
    session = seeded_registry

    first, _ = _run_to_done(session, TEXT, conversation_id="conv-1")
    second, second_llm = _run_to_done(session, TEXT, conversation_id="conv-2")
    assert first.state == second.state == TaskState.DONE.value
    assert second_llm.calls == []  # the chain must actually be active for this run

    first_bytes = (Path(first.workspace_path) / "deliverable" / "output.csv").read_bytes()
    second_bytes = (Path(second.workspace_path) / "deliverable" / "output.csv").read_bytes()
    assert len(first_bytes) > 0
    assert second_bytes == first_bytes


def test_a_request_with_no_match_still_synthesizes(seeded_registry):
    """The fast path must not have become the only path: an operation no seed
    knows ("export", not "summar" or "compare/difference/...") takes the
    synthesis lane and still succeeds."""
    session = seeded_registry

    result, _llm = _run_to_done(session, "export the holdings as csv", conversation_id="conv-3")

    assert result.state == TaskState.DONE.value, result.execution_verdict
    manifest = json.loads((Path(result.workspace_path) / "manifest.json").read_text())
    assert manifest["lane"] == "synthesis"
    # llm.calls alone would pass on the interpreter call too; the synthesis
    # credit is what actually distinguishes this from the registry lane.
    assert manifest["models"]["synthesis"] is not None


def test_the_seeded_demo_conversation_takes_the_registry_fast_path(seeded_registry, client):
    """Pins the README claim: the seeds are installed before the demo
    conversation is replayed (app.py's lifespan does the same, in the same
    order), and the demo's request — compare Bloomberg against FactSet, as
    Excel — is exactly set_difference's shape. Mirrors
    test_executor_end_to_end.py's golden-conversation helper."""
    client.post("/simulate/messy_universe_check")
    task = client.get("/tasks").json()[0]
    done = client.post(f"/tasks/{task['id']}/mode", json={"mode": "auto"}).json()

    assert done["state"] == "done", done
    manifest = json.loads((Path(done["workspace_path"]) / "manifest.json").read_text())
    assert manifest["lane"] == "registry"
