import io
import json
import zipfile

import pytest

from ley_khaa.executor.workspace import Workspace
from ley_khaa.persistence.repository import TaskRepository


@pytest.fixture
def bundled(session, tmp_path):
    """A task with a bundle on disk, as Task 10 would have left it."""
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="compare", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "bloomberg_universe.csv").write_text("ticker\nSYN0000\n")
    workspace.write_generator(1, "print('hello')")
    workspace.write_run_script(1)
    (workspace.deliverable_dir / "output.xlsx").write_bytes(b"PK\x03\x04\xff\xfe\x00 not really a zip")
    workspace.write_manifest({"task_id": task.id, "lane": "synthesis", "sandbox": "subprocess"})
    repo.save_execution(
        task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "done"}
    )
    return task, workspace


def test_the_bundle_endpoint_returns_the_manifest_and_a_file_listing(client, bundled):
    task, _ = bundled
    body = client.get(f"/tasks/{task.id}/bundle").json()
    assert body["manifest"]["lane"] == "synthesis"
    assert set(body["files"]) >= {
        "manifest.json",
        "inputs/bloomberg_universe.csv",
        "generator/attempt_1.py",
        "generator/run.sh",
        "deliverable/output.xlsx",
    }
    assert body["deliverables"] == ["deliverable/output.xlsx"]


def test_a_task_with_no_bundle_is_a_404(client, session):
    task = TaskRepository(session).create(project="demo", title="t", source_message_ids=[])
    assert client.get(f"/tasks/{task.id}/bundle").status_code == 404


def test_the_generator_source_can_be_read(client, bundled):
    task, _ = bundled
    body = client.get(
        f"/tasks/{task.id}/bundle/file", params={"path": "generator/attempt_1.py"}
    ).json()
    assert body["content"] == "print('hello')"


@pytest.mark.parametrize(
    "path", ["../../../etc/passwd", "/etc/passwd", "generator/../../../../etc/passwd"]
)
def test_the_file_endpoint_refuses_to_escape_the_bundle(client, bundled, path):
    """A viewer that can be talked into reading /etc/passwd is not a viewer."""
    task, _ = bundled
    assert client.get(f"/tasks/{task.id}/bundle/file", params={"path": path}).status_code == 400


def test_a_missing_file_inside_the_bundle_is_a_404(client, bundled):
    task, _ = bundled
    response = client.get(f"/tasks/{task.id}/bundle/file", params={"path": "generator/nope.py"})
    assert response.status_code == 404


def test_a_binary_file_is_not_returned_as_text(client, bundled):
    """The .xlsx has a download endpoint; the text viewer must not mangle it."""
    task, _ = bundled
    response = client.get(
        f"/tasks/{task.id}/bundle/file", params={"path": "deliverable/output.xlsx"}
    )
    assert response.status_code == 415


def test_the_deliverable_downloads(client, bundled):
    task, _ = bundled
    response = client.get(f"/tasks/{task.id}/bundle/deliverable")
    assert response.status_code == 200
    assert response.content.startswith(b"PK")
    assert "output.xlsx" in response.headers["content-disposition"]


def test_the_whole_bundle_downloads_as_a_zip(client, bundled):
    task, _ = bundled
    response = client.get(f"/tasks/{task.id}/bundle/download")
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert "generator/attempt_1.py" in archive.namelist()
    assert json.loads(archive.read("manifest.json"))["lane"] == "synthesis"


def test_the_task_payload_carries_the_bundle_path_and_verdict(client, bundled):
    task, workspace = bundled
    body = client.get(f"/tasks/{task.id}").json()
    assert body["workspace_path"] == str(workspace.root)
    assert body["execution_verdict"]["ok"] is True
