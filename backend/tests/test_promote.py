"""Promotion: a proven bundle becomes a permanent workflow (spec §5.6, task 9).

Promotion is a pure copy. These tests pin that the source frozen into a
WorkflowRow is byte-for-byte the winning attempt, that its roles come from the
params.json binding the run actually used (never invented from the spec), and
that every path promotion reads is contained inside the bundle — a synthesized
script's own generator/ directory is untrusted, and a symlink planted there
must never be promoted into code that runs on every future match.
"""
import json
from pathlib import Path

from ley_khaa.executor.workspace import Workspace
from ley_khaa.persistence.repository import TaskRepository
from ley_khaa.persistence.workflow_repository import WorkflowRepository

from .test_executor_end_to_end import _run_the_golden_conversation


def _run_the_golden_conversation_again(client) -> dict:
    """A second, independent run of the same fixture.

    Same operation, output format and input arity as the first run — what
    test 7 needs to prove the promoted workflow serves the next request — but
    its own messages and its own task id, since replaying the fixture posts
    fresh messages rather than reusing the first run's.
    """
    before = {t["id"] for t in client.get("/tasks").json()}
    client.post("/simulate/messy_universe_check")
    task = next(t for t in client.get("/tasks").json() if t["id"] not in before)
    return client.post(f"/tasks/{task['id']}/mode", json={"mode": "auto"}).json()


def _manifest_and_params(task: dict) -> tuple[dict, dict]:
    root = Path(task["workspace_path"])
    manifest = json.loads((root / "manifest.json").read_text())
    params = json.loads((root / "inputs" / "params.json").read_text())
    return manifest, params


def test_promoting_a_passing_task_freezes_the_winning_source(client, session):
    task = _run_the_golden_conversation(client)
    manifest, params = _manifest_and_params(task)
    winning_source = (Path(task["workspace_path"]) / "generator" / "attempt_1.py").read_text()

    response = client.post(
        f"/tasks/{task['id']}/promote",
        json={"name": "universe_check", "description": "bloomberg vs factset universe diff"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "universe_check"
    assert body["origin"] == "promoted"
    assert body["promoted_from_task_id"] == task["id"]

    row = WorkflowRepository(session).get("universe_check")
    assert row is not None
    # The claim promotion exists to make: byte-for-byte the winning attempt,
    # not a rewritten or reformatted copy of it.
    assert row.source == winning_source
    assert row.origin == "promoted"
    assert row.promoted_from_task_id == task["id"]
    assert {r["role"] for r in row.inputs} == set(params["inputs"])


def test_the_roles_come_from_the_binding_that_actually_ran(client, session):
    task = _run_the_golden_conversation(client)
    _, params = _manifest_and_params(task)

    client.post(
        f"/tasks/{task['id']}/promote",
        json={"name": "universe_check", "description": "bloomberg vs factset universe diff"},
    )

    row = WorkflowRepository(session).get("universe_check")
    # Order, not just membership: roles are positional (binder.py), so the
    # frozen script's INPUTS[0]/[1] must land on the same files this run gave
    # them, in the same order params.json bound them.
    assert [r["role"] for r in row.inputs] == list(params["inputs"])
    for declared in row.inputs:
        bound_filename = params["inputs"][declared["role"]]
        assert declared["suffixes"] == [Path(bound_filename).suffix.lower()]


def test_a_failed_task_cannot_be_promoted(client, session, tmp_path):
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="a run that failed", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "dataset.csv").write_text("ticker\nAAA\n")
    workspace.write_generator(1, "raise SystemExit(1)\n")
    workspace.write_params(inputs={"dataset": "dataset.csv"}, output="deliverable/output.csv", seed=1)
    workspace.write_manifest(
        {
            "task_id": task.id,
            "spec": {"operation": "summary_stats", "output_format": "csv"},
            "attempts": [{"attempt": 1, "ok": False}],
            "verdict": {"ok": False, "reason": "the script crashed"},
        }
    )
    repo.save_execution(
        task.id, workspace_path=str(workspace.root), verdict={"ok": False, "reason": "the script crashed"}
    )

    response = client.post(
        f"/tasks/{task.id}/promote", json={"name": "should_never_exist", "description": ""}
    )

    assert response.status_code == 409
    assert WorkflowRepository(session).get("should_never_exist") is None


def test_a_task_with_no_bundle_cannot_be_promoted(client, session):
    task = TaskRepository(session).create(project="demo", title="never run", source_message_ids=[])

    response = client.post(f"/tasks/{task.id}/promote", json={"name": "ghost", "description": ""})

    assert response.status_code == 404
    assert WorkflowRepository(session).get("ghost") is None


def test_a_duplicate_name_is_refused(client, session):
    task = _run_the_golden_conversation(client)

    first = client.post(
        f"/tasks/{task['id']}/promote", json={"name": "universe_check", "description": "first"}
    )
    assert first.status_code == 200

    second = client.post(
        f"/tasks/{task['id']}/promote", json={"name": "universe_check", "description": "second"}
    )

    assert second.status_code == 409
    # Refused, not silently versioned or overwritten: exactly one row, holding
    # the first promotion's description.
    assert len(WorkflowRepository(session).list()) == 1
    assert WorkflowRepository(session).get("universe_check").description == "first"


def test_promotion_never_reads_outside_the_bundle(client, session, tmp_path):
    """The Task 11 (Phase 3) ruling applied to a new reader of bundle contents:
    a symlink planted by untrusted generator code must not be promoted."""
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="a poisoned bundle", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "dataset.csv").write_text("ticker\nAAA\n")
    workspace.write_params(inputs={"dataset": "dataset.csv"}, output="deliverable/output.csv", seed=1)

    # A file outside the bundle root entirely — a sibling of task-<id>/, the
    # same shape test_bundle_api.py's escaping-symlink tests use.
    secret = tmp_path / "outside-secret.py"
    secret.write_text("import os\nos.system('this must never run')\n")
    # The manifest's winning attempt IS this symlink: attempt 9, marked ok, so
    # promote() reads exactly the path this plants rather than some other file
    # that happens to also be named attempt_9.py.
    (workspace.generator_dir / "attempt_9.py").symlink_to(secret)

    workspace.write_manifest(
        {
            "task_id": task.id,
            "spec": {"operation": "summary_stats", "output_format": "csv"},
            "attempts": [{"attempt": 9, "ok": True}],
            "verdict": {"ok": True, "reason": "looked fine"},
        }
    )
    repo.save_execution(
        task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "looked fine"}
    )

    response = client.post(
        f"/tasks/{task.id}/promote", json={"name": "poisoned_workflow", "description": ""}
    )

    assert response.status_code == 409
    assert WorkflowRepository(session).get("poisoned_workflow") is None


def test_the_promoted_workflow_serves_the_next_matching_request(client, session):
    first = _run_the_golden_conversation(client)
    promoted = client.post(
        f"/tasks/{first['id']}/promote",
        json={"name": "universe_check", "description": "bloomberg vs factset universe diff"},
    )
    assert promoted.status_code == 200

    second = _run_the_golden_conversation_again(client)

    assert second["state"] == "done"
    manifest, _ = _manifest_and_params(second)
    assert manifest["lane"] == "registry"
    assert manifest["workflow"]["name"] == "universe_check"
