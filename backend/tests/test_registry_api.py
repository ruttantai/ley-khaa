"""The human control surface over the registry cache (spec §5.6, task 10).

list/unquarantine/delete are the human's counterpart to the machine-only
promote() and the fast-path matcher: a workflow earns its way into the cache
automatically, but a human decides what leaves it or gets a second chance.
"""
import json

from ley_khaa.executor.runner import ExecutionRunner
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.workflow_repository import WorkflowRepository
from ley_khaa.registry.models import RegistryDecision
from ley_khaa.registry.seeds import ensure_seed_workflows

from .test_runner import FakeSandbox, _script, _spec, _writes_csv, task  # noqa: F401


def test_the_registry_lists_seeds_and_promotions_with_their_usage(client, session):
    ensure_seed_workflows(session)
    repo = WorkflowRepository(session)
    repo.create(
        name="promoted_one", description="a promoted workflow",
        operation_aliases=["promoted_op"], output_format="csv",
        inputs=[{"role": "dataset", "suffixes": [".csv"]}],
        source="print('ok')", origin="promoted", promoted_from_task_id="task-1",
    )

    body = client.get("/registry").json()

    names = {row["name"] for row in body}
    assert "summary_stats" in names
    assert "set_difference" in names
    assert "promoted_one" in names
    for row in body:
        assert "origin" in row
        assert "runs_ok" in row
        assert "runs_failed" in row
        assert "quarantined" in row
        assert "source_sha256" in row


def test_the_listing_never_leaks_the_source(client, session):
    ensure_seed_workflows(session)
    body = client.get("/registry").json()
    assert body  # the seeds are actually present, this isn't vacuous
    for row in body:
        assert "source" not in row


def test_a_quarantined_workflow_can_be_cleared_by_a_human(client, session):
    repo = WorkflowRepository(session)
    repo.create(
        name="flaky", description="fails sometimes",
        operation_aliases=["flaky_op"], output_format="csv",
        inputs=[{"role": "dataset", "suffixes": [".csv"]}],
        source="raise SystemExit(1)", origin="seed",
    )
    repo.record_failure("flaky")
    assert repo.get("flaky").quarantined is True
    assert repo.get("flaky").runs_failed == 1

    response = client.post("/registry/flaky/unquarantine")

    assert response.status_code == 200
    body = response.json()
    assert body["quarantined"] is False
    # Clearing a quarantine is not the same as forgiving the failure that
    # caused it — the run count stays as history.
    assert body["runs_failed"] == 1


def test_unquarantining_an_unknown_workflow_is_404(client):
    response = client.post("/registry/nonexistent/unquarantine")
    assert response.status_code == 404


def test_deleting_a_workflow_removes_it_from_matching(client, tmp_path, task, session):
    ensure_seed_workflows(session)

    response = client.delete("/registry/summary_stats")
    assert response.status_code == 204
    assert WorkflowRepository(session).get("summary_stats") is None

    row, messages = task
    # set_difference is still seeded and active, so the fingerprint stage
    # finds no candidate for "summary_stats" and the matcher falls through to
    # its stage-2 model call — that call is what proves the deletion actually
    # took the workflow out of matching, not just out of the fingerprint pass.
    runner = ExecutionRunner(
        llm=FakeLLM(
            responses=[
                RegistryDecision(workflow=None, confidence=0.0, reason="no match"),
                _script(),
            ]
        ),
        messages=messages,
        sandbox=FakeSandbox([_writes_csv]),
        workspace_root=tmp_path,
        workflows=WorkflowRepository(session),
    )
    outcome = runner.run(
        row, _spec(operation="summary_stats", output_format="csv", inputs=["holdings"])
    )

    assert outcome.verdict.ok, outcome.verdict.reason
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["lane"] == "synthesis"


def test_deleting_an_unknown_workflow_is_404(client):
    response = client.delete("/registry/nonexistent")
    assert response.status_code == 404
