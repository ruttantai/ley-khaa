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

import pytest

from ley_khaa.executor.workspace import Workspace
from ley_khaa.persistence.repository import TaskRepository
from ley_khaa.persistence.workflow_repository import WorkflowRepository
from ley_khaa.registry.promote import NotPromotable, promote

from .test_executor_end_to_end import _run_the_golden_conversation


def _minimal_passing_bundle(session, tmp_path, *, inputs: dict[str, str]) -> tuple:
    """A hand-built bundle that promote() will accept, for tests that need to
    control exactly what the manifest/params/source say rather than driving a
    real synthesis run."""
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="hand-built bundle", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    for filename in inputs.values():
        (workspace.inputs_dir / filename).write_text("a\n1\n")
    workspace.write_generator(1, "print('ok')\n")
    workspace.write_params(inputs=inputs, output="deliverable/output.csv", seed=1)
    workspace.write_manifest(
        {
            "task_id": task.id,
            "spec": {"operation": "summary_stats", "output_format": "csv"},
            "attempts": [{"attempt": 1, "ok": True}],
            "verdict": {"ok": True, "reason": "looked fine"},
        }
    )
    repo.save_execution(
        task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "looked fine"}
    )
    return task, workspace


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


# --- Finding 1: manifest.json itself must go through containment --------------

def test_promotion_never_reads_a_symlinked_manifest(client, session, tmp_path):
    """promote.py's docstring claims 'every path read below goes through'
    contained(), but manifest.json was read via Workspace.read_manifest(),
    which does exists() + read_text() — both symlink-following. A symlinked
    manifest.json could smuggle a fabricated verdict and attempt list past
    every other check promote() makes."""
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="a poisoned manifest", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "dataset.csv").write_text("ticker\nAAA\n")
    workspace.write_params(inputs={"dataset": "dataset.csv"}, output="deliverable/output.csv", seed=1)
    workspace.write_generator(1, "print('hi')\n")

    # A forged manifest living outside the bundle entirely, claiming a passing
    # verdict this bundle never earned.
    outside = tmp_path / "outside-manifest.json"
    outside.write_text(
        json.dumps(
            {
                "task_id": task.id,
                "spec": {"operation": "summary_stats", "output_format": "csv"},
                "attempts": [{"attempt": 1, "ok": True}],
                "verdict": {"ok": True, "reason": "forged"},
            }
        )
    )
    (workspace.root / "manifest.json").symlink_to(outside)

    repo.save_execution(
        task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "forged"}
    )

    response = client.post(
        f"/tasks/{task.id}/promote", json={"name": "poisoned_manifest_workflow", "description": ""}
    )

    assert response.status_code == 409
    assert WorkflowRepository(session).get("poisoned_manifest_workflow") is None


# --- Finding 2: malformed bundle content is a 409, never a 500 ----------------

def test_a_malformed_manifest_json_is_a_409(client, session, tmp_path):
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="broken manifest", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "dataset.csv").write_text("ticker\nAAA\n")
    workspace.write_params(inputs={"dataset": "dataset.csv"}, output="deliverable/output.csv", seed=1)
    workspace.write_generator(1, "print('hi')\n")
    (workspace.root / "manifest.json").write_text("{not valid json")
    repo.save_execution(task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "x"})

    response = client.post(
        f"/tasks/{task.id}/promote", json={"name": "broken_manifest_workflow", "description": ""}
    )

    assert response.status_code == 409
    assert response.json()["detail"]
    assert WorkflowRepository(session).get("broken_manifest_workflow") is None


def test_a_missing_attempt_number_is_a_409(client, session, tmp_path):
    """An attempts entry marked ok but with no "attempt" key — malformed, not
    impossible, since this bundle's own bytes are written by untrusted code
    that a compromised or buggy runner could still hand to promote()."""
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="attempt-less manifest", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "dataset.csv").write_text("ticker\nAAA\n")
    workspace.write_params(inputs={"dataset": "dataset.csv"}, output="deliverable/output.csv", seed=1)
    workspace.write_generator(1, "print('hi')\n")
    workspace.write_manifest(
        {
            "task_id": task.id,
            "spec": {"operation": "summary_stats", "output_format": "csv"},
            "attempts": [{"ok": True}],  # no "attempt" key
            "verdict": {"ok": True, "reason": "x"},
        }
    )
    repo.save_execution(task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "x"})

    response = client.post(
        f"/tasks/{task.id}/promote", json={"name": "attemptless_workflow", "description": ""}
    )

    assert response.status_code == 409
    assert response.json()["detail"]
    assert WorkflowRepository(session).get("attemptless_workflow") is None


def test_a_boolean_attempt_number_is_rejected(client, session, tmp_path):
    """bool is a subclass of int in Python, so isinstance(True, int) is True —
    a manifest whose winning "attempt" is JSON `true` must not pass the
    isinstance(attempt_number, int) guard as if it were a real attempt
    number. A file actually named attempt_True.py is planted so this proves
    the guard itself rejects the bool — without it, f"attempt_{True}.py"
    would resolve straight to this file and get promoted, a 200, not merely
    fail an incidental is_file() check on a path that happens not to exist."""
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="boolean attempt number", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "dataset.csv").write_text("ticker\nAAA\n")
    workspace.write_params(inputs={"dataset": "dataset.csv"}, output="deliverable/output.csv", seed=1)
    (workspace.generator_dir / "attempt_True.py").write_text("print('hi')\n")
    workspace.write_manifest(
        {
            "task_id": task.id,
            "spec": {"operation": "summary_stats", "output_format": "csv"},
            "attempts": [{"attempt": True, "ok": True}],
            "verdict": {"ok": True, "reason": "x"},
        }
    )
    repo.save_execution(task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "x"})

    response = client.post(
        f"/tasks/{task.id}/promote", json={"name": "boolean_attempt_workflow", "description": ""}
    )

    assert response.status_code == 409
    assert response.json()["detail"]
    assert WorkflowRepository(session).get("boolean_attempt_workflow") is None


def test_malformed_params_json_is_a_409(client, session, tmp_path):
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="broken params", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "dataset.csv").write_text("ticker\nAAA\n")
    (workspace.inputs_dir / "params.json").write_text("not json at all")
    workspace.write_generator(1, "print('hi')\n")
    workspace.write_manifest(
        {
            "task_id": task.id,
            "spec": {"operation": "summary_stats", "output_format": "csv"},
            "attempts": [{"attempt": 1, "ok": True}],
            "verdict": {"ok": True, "reason": "x"},
        }
    )
    repo.save_execution(task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "x"})

    response = client.post(
        f"/tasks/{task.id}/promote", json={"name": "broken_params_workflow", "description": ""}
    )

    assert response.status_code == 409
    assert response.json()["detail"]
    assert WorkflowRepository(session).get("broken_params_workflow") is None


def test_non_utf8_source_is_a_409(client, session, tmp_path):
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="binary garbage source", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "dataset.csv").write_text("ticker\nAAA\n")
    workspace.write_params(inputs={"dataset": "dataset.csv"}, output="deliverable/output.csv", seed=1)
    (workspace.generator_dir / "attempt_1.py").write_bytes(b"\xff\xfe\x00not utf-8 at all\x80")
    workspace.write_manifest(
        {
            "task_id": task.id,
            "spec": {"operation": "summary_stats", "output_format": "csv"},
            "attempts": [{"attempt": 1, "ok": True}],
            "verdict": {"ok": True, "reason": "x"},
        }
    )
    repo.save_execution(task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "x"})

    response = client.post(
        f"/tasks/{task.id}/promote", json={"name": "binary_source_workflow", "description": ""}
    )

    assert response.status_code == 409
    assert response.json()["detail"]
    assert WorkflowRepository(session).get("binary_source_workflow") is None


# --- Minor 8: a CRLF source must be hashed and stored byte-for-byte ----------

def test_a_crlf_source_is_promoted_byte_for_byte(client, session, tmp_path):
    """read_text() applies universal-newline translation: a CRLF source read
    that way is silently promoted, hashed and replayed as LF, so
    source_sha256 would attest bytes that never existed on disk — against
    WorkflowRow's own docstring promising byte-for-byte source."""
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="crlf source", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "dataset.csv").write_text("ticker\nAAA\n")
    workspace.write_params(inputs={"dataset": "dataset.csv"}, output="deliverable/output.csv", seed=1)
    crlf_source = b"import sys\r\nprint('hi')\r\n"
    (workspace.generator_dir / "attempt_1.py").write_bytes(crlf_source)
    workspace.write_manifest(
        {
            "task_id": task.id,
            "spec": {"operation": "summary_stats", "output_format": "csv"},
            "attempts": [{"attempt": 1, "ok": True}],
            "verdict": {"ok": True, "reason": "x"},
        }
    )
    repo.save_execution(task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "x"})

    response = client.post(
        f"/tasks/{task.id}/promote", json={"name": "crlf_source_workflow", "description": ""}
    )

    assert response.status_code == 200
    row = WorkflowRepository(session).get("crlf_source_workflow")
    # The stored source keeps its \r\n line endings...
    assert row.source == crlf_source.decode("utf-8")
    assert "\r\n" in row.source
    # ...and the hash attests those exact bytes, not an LF-normalized copy.
    import hashlib

    assert row.source_sha256 == hashlib.sha256(crlf_source).hexdigest()


# --- Minor 3: role order must be pinned by a NON-alphabetical fixture ---------

def test_role_order_pins_insertion_order_not_alphabetical(session, tmp_path):
    """The golden conversation's roles ("bloomberg universe", "factset") are
    already alphabetical, so test_the_roles_come_from_the_binding_that_actually_ran
    cannot fail under a `sorted()` regression in promote(). This fixture's roles
    are deliberately out of alphabetical order ("zulu" before "alpha"), so only
    genuine insertion-order preservation makes it pass."""
    task, workspace = _minimal_passing_bundle(session, tmp_path, inputs={"zulu": "z.csv", "alpha": "a.csv"})

    from ley_khaa.api.app import _contained

    row = promote(
        session,
        task_id=task.id,
        name="reverse_order_roles",
        description="",
        root=workspace.root,
        contained=_contained,
    )

    assert [r["role"] for r in row.inputs] == ["zulu", "alpha"]


# --- Minor 6: the name regex must not accept a trailing newline --------------

def test_a_trailing_newline_in_the_name_is_rejected_by_the_promote_guard(session, tmp_path):
    """`.match()` anchored with a trailing `$` matches just before a trailing
    newline in Python's `re` module, so 'universe_check\\n' was previously
    accepted. This exercises promote()'s own guard directly — the path a
    caller that bypasses the API schema (Finding 7) still hits — so name
    validation runs before any file is touched."""
    from ley_khaa.api.app import _contained

    # Matched on the message, not just the exception type: without this, a
    # buggy regex that lets the newline through would still raise
    # NotPromotable further down (no bundle at `tmp_path`) for an unrelated
    # reason, and the test would pass without ever exercising the name check.
    with pytest.raises(NotPromotable, match="workflow name"):
        promote(
            session,
            task_id="irrelevant",
            name="universe_check\n",
            description="",
            root=tmp_path,
            contained=_contained,
        )


# --- Finding 7: a malformed NAME is 422 (schema), never 409 ------------------

def test_a_malformed_workflow_name_is_a_422_not_a_409(client, session):
    task = _run_the_golden_conversation(client)

    response = client.post(f"/tasks/{task['id']}/promote", json={"name": "Not Valid!", "description": ""})

    assert response.status_code == 422
    assert WorkflowRepository(session).get("Not Valid!") is None


def test_a_trailing_newline_workflow_name_is_a_422_via_the_api(client, session):
    task = _run_the_golden_conversation(client)

    response = client.post(
        f"/tasks/{task['id']}/promote", json={"name": "universe_check\n", "description": ""}
    )

    assert response.status_code == 422
