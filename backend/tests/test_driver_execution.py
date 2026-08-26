"""What _execute and _validate now do, and what they refuse to do."""
import pytest

from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.executor.runner import ExecutionOutcome
from ley_khaa.executor.sandbox import SandboxUnavailable
from ley_khaa.executor.validator import Verdict
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


class StubRunner:
    """Stands in for ExecutionRunner so the driver's own branches are the
    subject. The runner has its own suite."""

    def __init__(self, outcome=None, raises=None):
        self.outcome = outcome
        self.raises = raises
        self.calls = 0

    def run(self, row, spec):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.outcome


def _spec() -> TaskSpec:
    return TaskSpec(
        intent="compare", inputs=["bloomberg universe", "factset"],
        operation="set_difference", output_format="xlsx", certainty=0.95,
    )


@pytest.fixture
def executing(session):
    """A task claimed into EXECUTING, which is where _execute picks it up."""
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(
        Message(source="s", client="c", conversation_id="conv-1", author="boss", text="compare")
    )
    task = repo.create(project="default", title="t", source_message_ids=[row.id])
    repo.save_spec(task.id, _spec())
    repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(task.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    repo.claim(task.id, expected=TaskState.INTERPRETED, target=TaskState.EXECUTING)
    driver = TaskDriver(
        repo, llm=FakeLLM([]), messages=messages, candidates=CandidateRepository(session)
    )
    return repo, driver, task


def test_a_good_run_lands_in_done_with_its_bundle_recorded(executing):
    repo, driver, task = executing
    driver.executor = StubRunner(
        ExecutionOutcome(Verdict(True, "Produced output.xlsx in 12 ms.", {"has_rows": True}),
                         "/bundles/task-1", 1)
    )
    result = driver.advance(task.id)
    assert result.state == TaskState.DONE.value
    assert result.workspace_path == "/bundles/task-1"
    assert result.execution_verdict["ok"] is True
    assert result.execution_verdict["attempts"] == 1


def test_a_failed_run_becomes_a_question_not_a_dead_end(executing):
    """VALIDATING -> NEEDS_CLARIFICATION is a legal edge and EXECUTING ->
    NEEDS_CLARIFICATION is not, which is the whole reason the verdict is
    persisted rather than acted on in place."""
    repo, driver, task = executing
    driver.executor = StubRunner(
        ExecutionOutcome(
            Verdict(False, "The script finished but produced no output file.", {}),
            "/bundles/task-1", 2,
        )
    )
    result = driver.advance(task.id)
    assert result.state == TaskState.NEEDS_CLARIFICATION.value
    assert result.open_question == "The script finished but produced no output file."
    # The bundle is still recorded: a failure a human cannot inspect is not one
    # they can act on.
    assert result.workspace_path == "/bundles/task-1"


def test_a_dead_sandbox_fails_the_task_rather_than_asking_a_human(executing):
    """§6: infrastructure failure is ley-khaa's problem. Asking someone to
    answer for a daemon that died is not a question."""
    repo, driver, task = executing
    driver.executor = StubRunner(raises=SandboxUnavailable("the daemon went away"))
    result = driver.advance(task.id)
    assert result.state == TaskState.FAILED.value
    assert "sandbox" in result.failure_reason
    assert result.open_question is None


def test_execution_runs_exactly_once_per_pass(executing):
    """advance() is re-entrant and the sweeper runs concurrently with HTTP
    handlers. Executing twice would mean paying Opus twice and racing two
    sandboxes over one workspace."""
    repo, driver, task = executing
    runner = StubRunner(ExecutionOutcome(Verdict(True, "ok", {}), "/bundles/task-1", 1))
    driver.executor = runner
    driver.advance(task.id)
    driver.advance(task.id)
    assert runner.calls == 1


def test_a_task_with_no_spec_never_reaches_execution(session):
    """_execute validates row.spec, so a task that somehow arrived without one
    must not blow up inside the executor."""
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(
        Message(source="s", client="c", conversation_id="conv-1", author="boss", text="compare")
    )
    task = repo.create(project="default", title="t", source_message_ids=[row.id])
    repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(task.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    repo.claim(task.id, expected=TaskState.INTERPRETED, target=TaskState.EXECUTING)
    driver = TaskDriver(
        repo, llm=FakeLLM([]), messages=messages, candidates=CandidateRepository(session)
    )
    driver.executor = StubRunner(ExecutionOutcome(Verdict(True, "ok", {}), "/b", 1))
    result = driver.advance(task.id)
    assert result.state == TaskState.FAILED.value
    assert "specification" in result.failure_reason
