import json

import pytest

from ley_khaa.domain.models import Message
from ley_khaa.executor.runner import ExecutionRunner
from ley_khaa.executor.sandbox import SandboxResult, SandboxUnavailable
from ley_khaa.executor.synthesizer import SynthesizedScript
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


class FakeSandbox:
    """Runs nothing. Each queued step performs the effect a real run would have.

    Runner tests are about the lane and the repair loop, so paying for a real
    interpreter start-up per attempt would buy nothing and cost the suite its
    sub-second runtime.
    """

    name = "fake"

    def __init__(self, steps):
        self.steps = list(steps)
        self.scripts = []

    def run(self, *, script, workspace, timeout_s):
        self.scripts.append(script)
        return self.steps.pop(0)(workspace)


def _crash(_workspace) -> SandboxResult:
    return SandboxResult(
        exit_code=1, stdout="", stderr="KeyError: 'ticker'", duration_ms=4, timed_out=False
    )


def _writes_csv(workspace) -> SandboxResult:
    (workspace / "deliverable" / "output.csv").write_text("ticker\nSYN0000\n")
    return SandboxResult(exit_code=0, stdout="ok", stderr="", duration_ms=7, timed_out=False)


def _boom(_workspace) -> SandboxResult:
    raise SandboxUnavailable("the daemon went away")


def _spec(inputs=None, output_format="csv", operation="set_difference") -> TaskSpec:
    return TaskSpec(
        intent="compare the universes",
        inputs=inputs if inputs is not None else ["bloomberg universe", "factset"],
        operation=operation,
        output_format=output_format,
        certainty=0.9,
    )


def _script(source="print('ok')") -> SynthesizedScript:
    return SynthesizedScript(reasoning="because", source=source)


@pytest.fixture
def task(session):
    messages = MessageRepository(session)
    row = messages.add(
        Message(
            source="slack", client="demo", conversation_id="conv-1",
            author="boss", text="compare them",
        )
    )
    created = TaskRepository(session).create(
        project="demo", title="compare", source_message_ids=[row.id]
    )
    return created, messages


def _runner(tmp_path, task, *, responses, steps):
    row, messages = task
    return row, ExecutionRunner(
        llm=FakeLLM(responses),
        messages=messages,
        sandbox=FakeSandbox(steps),
        workspace_root=tmp_path,
    )


def test_a_clean_first_attempt_is_the_whole_story(tmp_path, task):
    row, runner = _runner(tmp_path, task, responses=[_script()], steps=[_writes_csv])
    outcome = runner.run(row, _spec())
    assert outcome.verdict.ok
    assert outcome.attempts == 1
    assert (tmp_path / f"task-{row.id}" / "deliverable" / "output.csv").is_file()
    assert (tmp_path / f"task-{row.id}" / "generator" / "run.sh").is_file()


def test_a_failure_is_repaired_once_and_both_attempts_are_kept(tmp_path, task):
    """A bundle that hides its first failure is not an audit trail."""
    row, runner = _runner(
        tmp_path, task, responses=[_script("broken"), _script("fixed")],
        steps=[_crash, _writes_csv],
    )
    outcome = runner.run(row, _spec())
    assert outcome.verdict.ok
    assert outcome.attempts == 2
    generator = tmp_path / f"task-{row.id}" / "generator"
    assert (generator / "attempt_1.py").read_text() == "broken"
    assert (generator / "attempt_2.py").read_text() == "fixed"
    # run.sh points at the attempt that worked, not at the last one written.
    assert "attempt_2.py" in (generator / "run.sh").read_text()


def test_the_repair_prompt_carries_the_traceback(tmp_path, task):
    row, runner = _runner(
        tmp_path, task, responses=[_script("broken"), _script("fixed")],
        steps=[_crash, _writes_csv],
    )
    runner.run(row, _spec())
    second = runner.synthesizer.llm.calls[1].user
    assert "broken" in second
    assert "KeyError: 'ticker'" in second


def test_two_failures_escalate_instead_of_looping(tmp_path, task):
    """Decision 5: repair once, then hand it to a human. Not three times, not
    until the token budget runs out."""
    row, runner = _runner(
        tmp_path, task, responses=[_script(), _script()], steps=[_crash, _crash]
    )
    outcome = runner.run(row, _spec())
    assert not outcome.verdict.ok
    assert outcome.attempts == 2
    assert "Traceback" not in outcome.verdict.reason


def test_unresolvable_inputs_cost_nothing(tmp_path, task):
    """§6: a name that resolves to nothing becomes a question BEFORE any model
    call. Spending Opus tokens on a task we already know we cannot start is the
    waste this ordering exists to prevent."""
    row, runner = _runner(tmp_path, task, responses=[], steps=[])
    outcome = runner.run(row, _spec(inputs=["trade blotter"]))
    assert not outcome.verdict.ok
    assert "trade blotter" in outcome.verdict.reason
    assert outcome.attempts == 0
    assert runner.synthesizer.llm.calls == []


def test_a_dead_sandbox_is_not_a_question_for_a_human(tmp_path, task):
    """Infrastructure failure propagates; Task 10 turns it into FAILED. Asking
    a human to answer for a dead daemon is not a question they can answer."""
    row, runner = _runner(tmp_path, task, responses=[_script()], steps=[_boom])
    with pytest.raises(SandboxUnavailable):
        runner.run(row, _spec())


def test_a_dead_sandbox_does_not_leave_the_previous_round_s_manifest_standing(tmp_path, task):
    """A second round clears deliverable/ before it knows the sandbox is dead.

    Without a manifest written on the way out, the bundle would still list the
    first round's output.csv — with its sha256 — for a file that is no longer
    on disk.
    """
    row, runner = _runner(tmp_path, task, responses=[_script()], steps=[_writes_csv])
    runner.run(row, _spec())
    manifest_path = tmp_path / f"task-{row.id}" / "manifest.json"
    assert json.loads(manifest_path.read_text())["deliverables"], "round 1 should have one"

    # Round 2 on the same bundle, with the daemon gone.
    _, dead = _runner(tmp_path, task, responses=[_script()], steps=[_boom])
    with pytest.raises(SandboxUnavailable):
        dead.run(row, _spec())

    manifest = json.loads(manifest_path.read_text())
    assert manifest["deliverables"] == []
    assert manifest["verdict"]["ok"] is False
    assert "unavailable" in manifest["verdict"]["reason"]


def test_the_manifest_records_what_actually_happened(tmp_path, task):
    row, runner = _runner(
        tmp_path, task, responses=[_script("broken"), _script("fixed")],
        steps=[_crash, _writes_csv],
    )
    runner.run(row, _spec())
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["task_id"] == row.id
    assert manifest["lane"] == "synthesis"
    # Never "docker" when a fake ran: a bundle must not overstate its isolation.
    assert manifest["sandbox"] == "fake"
    assert [a["attempt"] for a in manifest["attempts"]] == [1, 2]
    assert manifest["attempts"][0]["ok"] is False
    assert manifest["attempts"][1]["ok"] is True
    assert len(manifest["deliverables"][0]["sha256"]) == 64
    assert {i["file"] for i in manifest["inputs"]} == {
        "bloomberg_universe.csv", "factset_universe.csv"
    }
    assert manifest["spec"]["operation"] == "set_difference"


def test_the_manifest_never_credits_a_model_that_did_not_write_the_script(tmp_path, task):
    """The same rule as "sandbox", one field over.

    With no ANTHROPIC_API_KEY the offline stand-in writes the script, but the
    router would still have chosen Opus for this stage. Naming that model here
    would tell a reader they are looking at model output, and would contradict
    the generator file's own docstring saying it was written offline.
    """
    row, runner = _runner(tmp_path, task, responses=[_script()], steps=[_writes_csv])
    runner.run(row, _spec())
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())

    assert manifest["models"]["synthesis"] == "fake (no model ran)"
    assert "claude" not in manifest["models"]["synthesis"]


def test_synthesis_blowing_up_is_a_failed_attempt_not_a_crash(tmp_path, task):
    row, runner = _runner(
        tmp_path, task, responses=[RuntimeError("connection reset"), _script()],
        steps=[_writes_csv],
    )
    outcome = runner.run(row, _spec())
    assert outcome.verdict.ok
    assert outcome.attempts == 2


def test_an_empty_script_is_not_run(tmp_path, task):
    row, runner = _runner(
        tmp_path, task, responses=[_script(""), _script()], steps=[_writes_csv]
    )
    outcome = runner.run(row, _spec())
    assert outcome.verdict.ok
    assert len(runner.sandbox.scripts) == 1


def test_unresolvable_inputs_never_probe_for_a_sandbox(tmp_path, task):
    """Decision 4 taken all the way: zero attempts must also mean zero sandbox
    resolution. pick_sandbox() shells out to probe the Docker daemon, and the
    whole point of the lazy `sandbox` property is that a request which executes
    nothing must not pay for that probe. Construct with no sandbox at all and
    confirm the backing field is still unset after a parked run."""
    row, messages = task
    runner = ExecutionRunner(llm=FakeLLM([]), messages=messages, workspace_root=tmp_path)
    outcome = runner.run(row, _spec(inputs=["trade blotter"]))
    assert not outcome.verdict.ok
    assert runner._sandbox is None
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["sandbox"] is None


def test_a_synthesis_failure_attempt_still_carries_an_ok_flag(tmp_path, task):
    """Every attempt record has an "ok" key, including a synthesis-failure one —
    a consumer iterating attempts and reading attempt["ok"] must never KeyError
    just because that particular attempt never reached the sandbox."""
    row, runner = _runner(
        tmp_path, task, responses=[RuntimeError("connection reset"), _script()],
        steps=[_writes_csv],
    )
    runner.run(row, _spec())
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["attempts"][0]["ok"] is False
    assert all("ok" in attempt for attempt in manifest["attempts"])


def _writes_xlsx(workspace):
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["ticker"])
    book.active.append(["SYN0000"])
    book.save(workspace / "deliverable" / "output.xlsx")
    return SandboxResult(exit_code=0, stdout="ok", stderr="", duration_ms=7, timed_out=False)


def test_a_second_round_is_not_judged_on_the_first_round_s_deliverable(tmp_path, task):
    """The escalate → answer → re-run loop re-executes into the SAME bundle.

    A csv from round one still sorts first in deliverable/, so without clearing
    it the validator measures a spec that now says "excel" against a file this
    run never wrote — and rejects a run that got it right, with a reason naming
    a filename the human's answer already superseded.
    """
    row, runner = _runner(tmp_path, task, responses=[_script()], steps=[_writes_csv])
    assert runner.run(row, _spec(output_format="csv")).verdict.ok

    row, second = _runner(tmp_path, task, responses=[_script()], steps=[_writes_xlsx])
    outcome = second.run(row, _spec(output_format="excel"))

    assert outcome.verdict.ok, outcome.verdict.reason
    deliverable = tmp_path / f"task-{row.id}" / "deliverable"
    assert [p.name for p in sorted(deliverable.iterdir())] == ["output.xlsx"]


def test_a_second_round_does_not_overwrite_the_first_round_s_attempts(tmp_path, task):
    """Spec §5 says every failed attempt stays in generator/. That has to hold
    across rounds, not only within one, or the audit trail loses the round the
    human was actually shown."""
    row, runner = _runner(tmp_path, task, responses=[_script("round one")], steps=[_writes_csv])
    runner.run(row, _spec(output_format="csv"))

    row, second = _runner(tmp_path, task, responses=[_script("round two")], steps=[_writes_xlsx])
    second.run(row, _spec(output_format="excel"))

    generator = tmp_path / f"task-{row.id}" / "generator"
    assert (generator / "attempt_1.py").read_text() == "round one"
    assert (generator / "attempt_2.py").read_text() == "round two"
    assert "attempt_2.py" in (generator / "run.sh").read_text()

    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    # The manifest lists only this round's attempts, and says how many belong to
    # earlier ones, so a reader is not left wondering why the list starts at 2.
    assert [a["attempt"] for a in manifest["attempts"]] == [2]
    assert manifest["earlier_attempts"] == 1
