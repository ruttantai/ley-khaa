"""Task memory short-circuits the interpreter — the same request asked twice
costs no LLM call the second time (spec §3.5)."""
import json
from pathlib import Path

from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.executor.runner import ExecutionOutcome
from ley_khaa.executor.validator import Verdict
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.memory.fingerprint import request_fingerprint
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.memory_repository import MemoryRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository

TEXT = "compare bloomberg against factset"


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two universes",
        inputs=["bloomberg", "factset"],
        operation="set_difference",
        output_format="csv",
        certainty=0.95,
    )
    return TaskSpec(**{**base, **overrides})


def _make_task(session, text, *, project="default", conversation_id="conv-1"):
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(
        Message(source="s", client="c", conversation_id=conversation_id, author="boss", text=text)
    )
    task = repo.create(project=project, title=text[:40], source_message_ids=[row.id])
    return repo, messages, task


def _driver(session, repo, messages, responses, *, with_memory=True):
    memories = MemoryRepository(session) if with_memory else None
    return TaskDriver(
        repo, llm=FakeLLM(responses), messages=messages,
        candidates=CandidateRepository(session), memories=memories,
    )


def _seed_memory(session, project, texts, spec, *, task_id="remembered-task"):
    memories = MemoryRepository(session)
    fingerprint = request_fingerprint(list(texts))
    row = memories.record(
        project=project, fingerprint=fingerprint, intent=spec.intent, spec=spec, task_id=task_id,
    )
    assert row is not None
    return row


def test_a_remembered_request_skips_the_interpreter(session):
    """The claim memory exists to make: the second identical request costs no
    interpreter call."""
    _seed_memory(session, "default", [TEXT], _spec(recipient="boss"))
    repo, messages, task = _make_task(session, TEXT)
    # An empty FakeLLM: interpretation would raise/exhaust it if the
    # interpreter ran at all.
    driver = _driver(session, repo, messages, responses=[])

    result = driver.advance(task.id)

    assert result.state == TaskState.AWAITING_APPROVAL.value
    assert TaskSpec.model_validate(result.spec).operation == "set_difference"
    assert driver.interpreter.llm.calls == []


def test_the_remembered_spec_points_at_this_task_s_messages(session):
    """A remembered id would make the bundle cite messages from another
    conversation."""
    remembered = _spec(recipient="boss", source_message_ids=["some-other-message-id"])
    _seed_memory(session, "default", [TEXT], remembered)
    repo, messages, task = _make_task(session, TEXT)
    driver = _driver(session, repo, messages, responses=[])

    result = driver.advance(task.id)

    spec = TaskSpec.model_validate(result.spec)
    assert spec.source_message_ids == list(task.source_message_ids)
    assert spec.source_message_ids != ["some-other-message-id"]
    assert driver.interpreter.llm.calls == []


def test_inputs_are_re_resolved_not_remembered(session, monkeypatch):
    """The remembered spec's inputs are names, not resolved bindings — the
    resolver re-resolves them against THIS task's own attachments/catalog at
    execution time, never the remembered task's.

    Uses the real (offline) synthesis and subprocess-sandbox lane, exactly
    like tests/test_executor_end_to_end.py, so the manifest it reads is real —
    not a stub standing in for it.
    """
    from ley_khaa.interpreter.interpreter import Interpreter
    from ley_khaa.llm.heuristic import HeuristicLLM

    _seed_memory(session, "default", [TEXT], _spec())
    repo, messages, task = _make_task(session, TEXT)
    driver = TaskDriver(
        repo, llm=HeuristicLLM(), messages=messages,
        candidates=CandidateRepository(session), memories=MemoryRepository(session),
    )

    def _must_not_run(self, row):
        raise AssertionError("the interpreter must not run on a memory hit")

    monkeypatch.setattr(Interpreter, "interpret", _must_not_run)

    seen_rows = []
    from ley_khaa.executor import resolver as resolver_module
    original = resolver_module.resolve_inputs

    def _spy(spec, row, msgs, extractor=None):
        seen_rows.append(row)
        return original(spec, row, msgs, extractor=extractor)

    monkeypatch.setattr("ley_khaa.executor.runner.resolve_inputs", _spy)

    result = driver.advance(task.id)

    assert result.state == TaskState.DONE.value
    spec = TaskSpec.model_validate(result.spec)
    # Still names, not filenames or content.
    assert spec.inputs == ["bloomberg", "factset"]
    # Resolution ran against THIS task, never the remembered one.
    assert len(seen_rows) == 1
    assert seen_rows[0].id == task.id
    assert seen_rows[0].source_message_ids == list(task.source_message_ids)

    manifest = json.loads((Path(result.workspace_path) / "manifest.json").read_text())
    assert [i["name"] for i in manifest["inputs"]] == ["bloomberg", "factset"]
    assert all(i["source"] == "catalog" for i in manifest["inputs"])


def test_a_memory_hit_is_recorded_on_the_task(session):
    remembered = _seed_memory(session, "default", [TEXT], _spec(recipient="boss"))
    repo, messages, task = _make_task(session, TEXT)
    driver = _driver(session, repo, messages, responses=[])

    result = driver.advance(task.id)

    assert result.remembered_from_task_id == remembered.source_task_id
    assert result.familiarity == remembered.times_seen
    assert driver.interpreter.llm.calls == []


def test_a_task_is_remembered_only_when_it_passes(session):
    """A task that reaches DONE with ok is recorded; one that ends in
    needs_clarification or failed is not.

    The failing task runs first, while no memory exists yet for either
    fingerprint, so its own miss never has to consult the (still empty)
    memory store's model fallback.
    """
    bad_text = "reconcile portfolio against holdings"
    repo, messages, bad_task = _make_task(session, bad_text)
    bad_driver = _driver(session, repo, messages, responses=[_spec(intent=bad_text)])
    bad_driver.executor.run = lambda row, spec: ExecutionOutcome(
        verdict=Verdict(ok=False, reason="stubbed failure", checks={}),
        workspace_path="/bundles/bad", attempts=1,
    )
    bad_result = bad_driver.advance(bad_task.id)
    assert bad_result.state == TaskState.NEEDS_CLARIFICATION.value

    ok_text = "reconcile bloomberg against factset"
    repo2, messages2, ok_task = _make_task(session, ok_text)
    ok_driver = _driver(session, repo2, messages2, responses=[_spec(intent=ok_text)])
    ok_driver.executor.run = lambda row, spec: ExecutionOutcome(
        verdict=Verdict(ok=True, reason="stubbed pass", checks={}),
        workspace_path="/bundles/ok", attempts=1,
    )
    ok_result = ok_driver.advance(ok_task.id)
    assert ok_result.state == TaskState.DONE.value

    memories = MemoryRepository(session)
    assert memories.by_fingerprint("default", request_fingerprint([ok_text])) is not None
    assert memories.by_fingerprint("default", request_fingerprint([bad_text])) is None


def test_a_second_identical_request_increments_familiarity(session):
    repo, messages, task1 = _make_task(session, TEXT)
    driver1 = _driver(session, repo, messages, responses=[_spec()])
    driver1.executor.run = lambda row, spec: ExecutionOutcome(
        verdict=Verdict(ok=True, reason="stubbed pass", checks={}),
        workspace_path="/bundles/first", attempts=1,
    )
    result1 = driver1.advance(task1.id)
    assert result1.state == TaskState.DONE.value
    assert result1.familiarity == 0

    repo2, messages2, task2 = _make_task(session, TEXT)
    driver2 = _driver(session, repo2, messages2, responses=[])
    driver2.executor.run = lambda row, spec: ExecutionOutcome(
        verdict=Verdict(ok=True, reason="stubbed pass", checks={}),
        workspace_path="/bundles/second", attempts=1,
    )
    result2 = driver2.advance(task2.id)

    assert result2.state == TaskState.DONE.value
    assert result2.familiarity == 1

    memories = MemoryRepository(session)
    row = memories.by_fingerprint("default", request_fingerprint([TEXT]))
    assert row.times_seen == 2


def test_with_no_memory_repository_the_driver_behaves_exactly_as_before(session):
    repo, messages, task = _make_task(session, TEXT)
    driver = _driver(session, repo, messages, responses=[_spec()], with_memory=False)
    driver.executor.run = lambda row, spec: ExecutionOutcome(
        verdict=Verdict(ok=True, reason="stubbed pass", checks={}),
        workspace_path="/bundles/no-memory", attempts=1,
    )

    result = driver.advance(task.id)

    assert result.state == TaskState.DONE.value
    assert result.remembered_from_task_id is None
    assert result.familiarity == 0

    memories = MemoryRepository(session)
    assert memories.by_fingerprint("default", request_fingerprint([TEXT])) is None


def test_familiarity_flows_into_the_stored_recommendation(session):
    """driver.py:_gate's familiarity=row.familiarity or 0 is the entire
    production wiring this task delivers. Task 12's tests assert the column
    is persisted; this asserts it actually reaches recommend() and changes
    what gets stored, not just that the number sits on the row unused."""
    from ley_khaa.autonomy.engine import recommend

    remembered = _seed_memory(session, "default", [TEXT], _spec(certainty=0.75))
    repo, messages, task = _make_task(session, TEXT)
    driver = _driver(session, repo, messages, responses=[])

    result = driver.advance(task.id)

    assert result.familiarity == remembered.times_seen == 1
    assert "I've done this 1 times before" in result.autonomy_reason

    baseline = recommend(TaskSpec.model_validate(result.spec)).confidence
    assert result.confidence == round(baseline + 0.05, 4)


def test_a_task_whose_text_is_all_stopwords_finishes_without_being_remembered(session):
    """MemoryRepository.record() refuses to store an empty fingerprint and
    returns None rather than a row (a request that fingerprints to nothing is
    unrecallable by construction — by_fingerprint("") and MemoryMatcher.recall
    both bail on it too). _remember() must not crash when record() answers
    None for the call site the phase's own review named directly: driver.py's
    _remember, not just the matcher's read path.

    All-stopword text ("the and") fingerprints to "" (see fingerprint.py).
    """
    blank_text = "the and"
    assert request_fingerprint([blank_text]) == ""

    repo, messages, task = _make_task(session, blank_text)
    driver = _driver(session, repo, messages, responses=[_spec(intent=blank_text)])
    driver.executor.run = lambda row, spec: ExecutionOutcome(
        verdict=Verdict(ok=True, reason="stubbed pass", checks={}),
        workspace_path="/bundles/blank", attempts=1,
    )

    result = driver.advance(task.id)

    assert result.state == TaskState.DONE.value

    memories = MemoryRepository(session)
    assert memories.for_project("default") == []
