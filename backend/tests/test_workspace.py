import json

import pytest

from ley_khaa.executor.resolver import ResolvedInput
from ley_khaa.executor.workspace import Workspace, sha256_file


def _input(name="holdings", content="ticker\nAAA\n") -> ResolvedInput:
    return ResolvedInput(name=name, filename=f"{name}.csv", content=content, source="catalog")


def test_create_lays_out_the_bundle_directories(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    assert ws.root == tmp_path / "task-task-1"
    assert ws.inputs_dir.is_dir()
    assert ws.generator_dir.is_dir()
    assert ws.deliverable_dir.is_dir()


def test_inputs_are_frozen_into_the_bundle(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    ws.write_inputs([_input()])
    assert (ws.inputs_dir / "holdings.csv").read_text() == "ticker\nAAA\n"


def test_each_attempt_is_kept(tmp_path):
    """A bundle that hides its first failure is not an audit trail."""
    ws = Workspace.create(tmp_path, "task-1")
    first = ws.write_generator(1, "print('one')")
    second = ws.write_generator(2, "print('two')")
    assert first.name == "attempt_1.py"
    assert second.name == "attempt_2.py"
    assert first.read_text() == "print('one')"
    assert sorted(p.name for p in ws.generator_dir.iterdir()) == ["attempt_1.py", "attempt_2.py"]


def test_deliverables_lists_only_produced_files(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    assert ws.deliverables() == []
    (ws.deliverable_dir / "out.xlsx").write_text("x")
    assert [p.name for p in ws.deliverables()] == ["out.xlsx"]


def test_input_hashes_detect_a_script_that_rewrote_its_inputs(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    ws.write_inputs([_input()])
    before = ws.input_hashes()
    (ws.inputs_dir / "holdings.csv").write_text("tampered\n")
    assert ws.input_hashes() != before


def test_manifest_round_trips(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    path = ws.write_manifest({"task_id": "task-1", "lane": "synthesis"})
    assert json.loads(path.read_text())["lane"] == "synthesis"
    assert ws.read_manifest()["task_id"] == "task-1"


def test_read_manifest_is_empty_before_one_is_written(tmp_path):
    assert Workspace.create(tmp_path, "task-1").read_manifest() == {}


def test_create_is_idempotent(tmp_path):
    """The sweeper can re-enter a task; re-creating must not wipe evidence."""
    ws = Workspace.create(tmp_path, "task-1")
    ws.write_generator(1, "print('one')")
    again = Workspace.create(tmp_path, "task-1")
    assert (again.generator_dir / "attempt_1.py").exists()


def test_sha256_file_is_content_addressed(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("hello")
    assert sha256_file(target) == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_write_inputs_rejects_absolute_paths(tmp_path):
    """write_inputs must reject absolute paths to prevent arbitrary file write."""
    ws = Workspace.create(tmp_path, "task-1")
    with pytest.raises(ValueError, match="cannot be absolute"):
        ws.write_inputs([ResolvedInput(name="evil", filename="/etc/passwd", content="x", source="test")])
    # File was not written outside the workspace
    assert not (tmp_path / "etc" / "passwd").exists()


def test_write_inputs_rejects_paths_with_traversal(tmp_path):
    """write_inputs must reject .. to prevent escaping the inputs directory."""
    ws = Workspace.create(tmp_path, "task-1")
    with pytest.raises(ValueError, match="cannot contain path separators"):
        ws.write_inputs([ResolvedInput(name="evil", filename="../../escape.csv", content="x", source="test")])
    # File was not written outside the workspace
    assert not (tmp_path / "escape.csv").exists()


def test_write_inputs_rejects_paths_with_forward_slash(tmp_path):
    """write_inputs must reject forward slash to enforce single-component filenames."""
    ws = Workspace.create(tmp_path, "task-1")
    with pytest.raises(ValueError, match="cannot contain path separators"):
        ws.write_inputs([ResolvedInput(name="evil", filename="subdir/file.csv", content="x", source="test")])
    # File was not written anywhere in the workspace
    assert not (ws.inputs_dir / "subdir" / "file.csv").exists()


def test_write_inputs_rejects_dot_and_dot_dot(tmp_path):
    """write_inputs must reject . and .. directory references."""
    ws = Workspace.create(tmp_path, "task-1")
    with pytest.raises(ValueError, match="cannot be a directory reference"):
        ws.write_inputs([ResolvedInput(name="evil", filename="..", content="x", source="test")])
    with pytest.raises(ValueError, match="cannot be a directory reference"):
        ws.write_inputs([ResolvedInput(name="evil", filename=".", content="x", source="test")])


def test_the_bundle_carries_a_way_to_re_run_it(tmp_path):
    """A bundle a human cannot re-run is a claim, not an audit trail."""
    ws = Workspace.create(tmp_path, "task-1")
    ws.write_generator(2, "print('two')")
    path = ws.write_run_script(2)
    assert path.name == "run.sh"
    assert "generator/attempt_2.py" in path.read_text()
