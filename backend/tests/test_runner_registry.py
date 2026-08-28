"""The registry fast path in ExecutionRunner (spec §3, task 8).

Reuses test_runner.py's fixtures and step helpers rather than inventing a
second fixture shape — a duplicate builder is exactly the drift a shared
helper prevents.
"""
import json

import pytest

from ley_khaa.executor.runner import ExecutionRunner
from ley_khaa.executor.sandbox import SandboxUnavailable, SubprocessSandbox
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.workflow_repository import WorkflowRepository
from ley_khaa.registry.models import RegistryDecision
from ley_khaa.registry.seeds import ensure_seed_workflows

from .test_runner import (  # noqa: F401
    FakeSandbox,
    _boom,
    _crash,
    _runner,
    _script,
    _spec,
    _writes_csv,
    task,
)


def _registry_runner(tmp_path, task, session, llm, *, steps=None):
    """Mirrors test_runner.py's `_runner`, plus a registry.

    steps=None means "let the seed workflow's real source actually run" — a
    fake sandbox that ignores the script content would never prove the seed
    works, so this defaults to SubprocessSandbox(). Passing `steps` opts into
    the fake, for tests where the workflow source is a deliberate poison pill
    and the point is the runner's *reaction* to a failing cached attempt, not
    Python execution.
    """
    row, messages = task
    sandbox = SubprocessSandbox() if steps is None else FakeSandbox(steps)
    return row, ExecutionRunner(
        llm=llm,
        messages=messages,
        sandbox=sandbox,
        workspace_root=tmp_path,
        workflows=WorkflowRepository(session),
    )


def test_a_matching_request_runs_the_proven_code_and_calls_no_model(tmp_path, task, session):
    """The claim the registry exists to make."""
    ensure_seed_workflows(session)
    llm = FakeLLM(responses=[])   # any synthesis call would raise
    row, runner = _registry_runner(tmp_path, task, session, llm)

    outcome = runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["holdings"]))

    assert outcome.verdict.ok, outcome.verdict.reason
    assert llm.calls == []
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["lane"] == "registry"
    assert manifest["workflow"]["name"] == "summary_stats"
    assert manifest["workflow"]["matched_by"] == "fingerprint"


def test_the_manifest_credits_no_model_on_a_cached_run(tmp_path, task, session):
    """No model wrote that script. The manifest may not imply one did — the same
    rule the sandbox field follows."""
    ensure_seed_workflows(session)
    row, runner = _registry_runner(tmp_path, task, session, FakeLLM(responses=[]))
    runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["holdings"]))

    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["models"]["synthesis"] is None


def test_the_binding_is_recorded_and_the_frozen_source_is_what_ran(tmp_path, task, session):
    ensure_seed_workflows(session)
    row, runner = _registry_runner(tmp_path, task, session, FakeLLM(responses=[]))
    runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["holdings"]))

    root = tmp_path / f"task-{row.id}"
    manifest = json.loads((root / "manifest.json").read_text())
    workflow = WorkflowRepository(session).get("summary_stats")

    assert manifest["workflow"]["sha256"] == workflow.source_sha256
    assert (root / "generator" / "attempt_1.py").read_text() == workflow.source
    # params.json carries the WORKFLOW's role name, not the spec's input name.
    params = json.loads((root / "inputs" / "params.json").read_text())
    assert list(params["inputs"]) == ["dataset"]
    assert manifest["workflow"]["binding"] == params["inputs"]


def test_a_failing_cached_workflow_quarantines_and_the_run_still_succeeds(tmp_path, task, session):
    """A cache that fails costs only the work it was trying to save.

    The cached attempt must genuinely fail here, or this test proves nothing
    about quarantine or the synthesis rescue — so the fake sandbox is given one
    step for EACH sandbox.run() call: crash for the cached attempt, then a
    working script for the synthesis rescue that follows it.
    """
    repo = WorkflowRepository(session)
    repo.create(
        name="poisoned", description="always crashes",
        operation_aliases=["summary_stats"], output_format="csv",
        inputs=[{"role": "dataset", "suffixes": [".csv"]}],
        source="raise SystemExit(1)", origin="seed",
    )
    llm = FakeLLM(responses=[_script()])   # exactly one synthesis rescue
    row, runner = _registry_runner(tmp_path, task, session, llm, steps=[_crash, _writes_csv])

    outcome = runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["holdings"]))

    assert outcome.verdict.ok, outcome.verdict.reason
    assert repo.get("poisoned").quarantined is True
    assert repo.get("poisoned").runs_failed == 1

    root = tmp_path / f"task-{row.id}"
    manifest = json.loads((root / "manifest.json").read_text())
    # The lane that produced the deliverable, plus a record that the cache was tried.
    assert manifest["lane"] == "synthesis"
    assert manifest["workflow"]["name"] == "poisoned"
    assert manifest["workflow"]["quarantined"] is True
    # Both attempts are in the bundle. The cached one did not eat a synthesis attempt.
    assert (root / "generator" / "attempt_1.py").is_file()
    assert (root / "generator" / "attempt_2.py").is_file()
    assert len(manifest["attempts"]) == 2


def test_the_inputs_are_not_reported_as_tampered_after_a_fallback(tmp_path, task, session):
    """params.json is rewritten between the lanes. Hashing before the last write
    makes the validator accuse the script of rewriting its own inputs.

    As above, the cached attempt must actually fail to exercise the fallback
    this test is named for.
    """
    repo = WorkflowRepository(session)
    repo.create(
        name="poisoned", description="always crashes",
        operation_aliases=["summary_stats"], output_format="csv",
        inputs=[{"role": "dataset", "suffixes": [".csv"]}],
        source="raise SystemExit(1)", origin="seed",
    )
    row, runner = _registry_runner(
        tmp_path, task, session, FakeLLM(responses=[_script()]), steps=[_crash, _writes_csv]
    )

    outcome = runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["holdings"]))

    assert outcome.verdict.ok
    assert outcome.verdict.checks.get("inputs_unmodified") is not False


def test_a_dead_sandbox_on_the_cached_lane_propagates_and_updates_the_manifest(
    tmp_path, task, session
):
    """The trap the synthesis loop's own SandboxUnavailable handler exists to
    avoid applies just as much to the cached lane: clear_deliverables() already
    ran, so a manifest left untouched would still describe a previous round's
    deliverable that is no longer on disk. This is not a business failure —
    Task 10 turns SandboxUnavailable into FAILED — so it must propagate, not
    become a Verdict."""
    ensure_seed_workflows(session)
    row, first = _registry_runner(tmp_path, task, session, FakeLLM(responses=[]))
    first.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["holdings"]))
    manifest_path = tmp_path / f"task-{row.id}" / "manifest.json"
    assert json.loads(manifest_path.read_text())["deliverables"], "round 1 should have one"

    _, dead = _registry_runner(
        tmp_path, task, session, FakeLLM(responses=[]), steps=[_boom]
    )
    with pytest.raises(SandboxUnavailable):
        dead.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["holdings"]))

    manifest = json.loads(manifest_path.read_text())
    assert manifest["deliverables"] == []
    assert manifest["verdict"]["ok"] is False
    assert "unavailable" in manifest["verdict"]["reason"]
    # The bug this handler was added to close: a dead daemon on the cached
    # lane must not be reported as a synthesis run that credits a model no
    # one called. lane="registry" and workflow=... on this write are what
    # keep _write_manifest's synthesis-shaped defaults from leaking in.
    assert manifest["lane"] == "registry"
    assert manifest["models"]["synthesis"] is None
    assert manifest["workflow"]["name"] == "summary_stats"
    assert manifest["workflow"]["quarantined"] is False


def test_a_model_matched_success_learns_the_alias_it_found(tmp_path, task, session):
    """The model-matched branch of the learning loop, exercised through the
    runner rather than just at the matcher level: a phrasing the model found,
    once it passes, becomes a free deterministic hit next time.

    "stats_summary" is a paraphrase of the seed's own alias ("summary_stats"),
    so the fingerprint stage (an exact, normalized string match) cannot find
    it — only the model call can, which is what makes this a matched_by ==
    "model" case rather than "fingerprint".
    """
    ensure_seed_workflows(session)
    llm = FakeLLM(responses=[RegistryDecision(workflow="summary_stats", confidence=0.95, reason="same job")])
    row, runner = _registry_runner(tmp_path, task, session, llm)

    outcome = runner.run(row, _spec(operation="stats_summary", output_format="csv", inputs=["holdings"]))

    assert outcome.verdict.ok, outcome.verdict.reason
    assert len(llm.calls) == 1
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["workflow"]["matched_by"] == "model"

    # expire_all() forces this to hit the database rather than the in-memory
    # object record_success already mutated — the same reason test_workflow_
    # repository.py does it for record_success directly.
    session.expire_all()
    persisted = WorkflowRepository(session).get("summary_stats")
    assert "stats_summary" in persisted.operation_aliases


def test_with_no_registry_the_runner_behaves_exactly_as_before(tmp_path, task):
    """workflows=None is a supported configuration, not a broken one."""
    row, runner = _runner(tmp_path, task, responses=[_script()], steps=[_writes_csv])
    outcome = runner.run(row, _spec())

    assert outcome.verdict.ok
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["lane"] == "synthesis"
    assert "workflow" not in manifest or manifest["workflow"] is None
