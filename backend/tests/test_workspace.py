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


def test_a_symlink_in_the_deliverable_directory_is_never_a_deliverable(tmp_path):
    """`p.is_file()` follows links. Everything downstream reads through this
    method — the validator's verdict, the manifest's sha256 list, the API's
    deliverable route — so a link followed here becomes a bundle attesting bytes
    the run never wrote."""
    ws = Workspace.create(tmp_path, "task-1")
    outside = tmp_path / "outside.csv"
    outside.write_text("ticker\nSECRET\n")
    (ws.deliverable_dir / "linked.csv").symlink_to(outside)
    (ws.deliverable_dir / "real.csv").write_text("ticker\nSYN0000\n")

    assert [p.name for p in ws.deliverables()] == ["real.csv"]
    assert [p.name for p in ws.linked_deliverables()] == ["linked.csv"]


def test_clearing_deliverables_leaves_the_rest_of_the_bundle_alone(tmp_path):
    """A re-executed task writes into the SAME bundle. Only deliverable/ is
    emptied: generator/ is the audit trail and inputs/ are frozen evidence."""
    ws = Workspace.create(tmp_path, "task-1")
    ws.write_generator(1, "print('one')")
    (ws.inputs_dir / "a.csv").write_text("ticker\n")
    (ws.deliverable_dir / "output.csv").write_text("stale\n")
    (ws.deliverable_dir / "linked.csv").symlink_to(tmp_path / "nowhere")
    (ws.deliverable_dir / "sub").mkdir()
    (ws.deliverable_dir / "sub" / "nested.txt").write_text("also stale")

    ws.clear_deliverables()

    assert list(ws.deliverable_dir.iterdir()) == []
    assert (ws.generator_dir / "attempt_1.py").is_file()
    assert (ws.inputs_dir / "a.csv").is_file()


def test_attempt_numbering_continues_past_what_is_already_on_disk(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    assert ws.next_attempt_number() == 1
    ws.write_generator(1, "one")
    ws.write_generator(2, "two")
    ws.write_run_script(2)  # not an attempt file, and must not be counted as one
    assert ws.next_attempt_number() == 3


def test_params_json_lands_in_inputs_so_the_tamper_check_covers_it(tmp_path):
    """params.json is an input like any other.

    It sits in inputs/, so input_hashes() covers it and a script that rewrites
    its own binding mid-run is caught by the existing check rather than being a
    new hole this contract opens.
    """
    workspace = Workspace.create(tmp_path, "t1")
    path = workspace.write_params(
        inputs={"bloomberg": "bloomberg_universe.csv"},
        output="deliverable/output.csv",
        seed=20260825,
    )

    assert path == workspace.inputs_dir / "params.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "inputs": {"bloomberg": "bloomberg_universe.csv"},
        "output": "deliverable/output.csv",
        "seed": 20260825,
    }
    assert "params.json" in workspace.input_hashes()


def test_write_params_preserves_input_order_rather_than_sorting_it(tmp_path):
    """json.dumps(..., sort_keys=True) recurses into nested dicts too.

    The offline stand-in's set_difference reads INPUTS[0]/INPUTS[1] as its
    left/right operands, so if "inputs" came back alphabetized, a spec whose
    input names are not already alphabetical in the intended order would have
    its operands silently swapped. Roles below are given in reverse-alphabetical
    order specifically so an accidental sort_keys=True would be caught here.
    """
    workspace = Workspace.create(tmp_path, "t1")
    path = workspace.write_params(
        inputs={"right": "r.csv", "left": "l.csv"},
        output="deliverable/output.csv",
        seed=20260825,
    )

    assert list(json.loads(path.read_text(encoding="utf-8"))["inputs"].keys()) == ["right", "left"]
