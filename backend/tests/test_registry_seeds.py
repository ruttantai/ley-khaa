import json
import subprocess
import sys

from ley_khaa.persistence.workflow_repository import WorkflowRepository
from ley_khaa.registry.seeds import ensure_seed_workflows


def test_seeding_is_idempotent(session):
    """Startup runs on every boot; a second boot must not duplicate or raise."""
    assert ensure_seed_workflows(session) == 2
    assert ensure_seed_workflows(session) == 0
    assert len(WorkflowRepository(session).list()) == 2


def test_seeds_are_marked_as_seeds_not_promotions(session):
    ensure_seed_workflows(session)
    for row in WorkflowRepository(session).list():
        assert row.origin == "seed"
        assert row.promoted_from_task_id is None


def test_the_set_difference_seed_reads_its_binding_and_writes_the_deliverable(tmp_path):
    """The seed is a real program, run the way the sandbox runs it: cwd is the
    bundle root, paths come from inputs/params.json."""
    (tmp_path / "inputs").mkdir()
    (tmp_path / "deliverable").mkdir()
    (tmp_path / "inputs" / "left.csv").write_text("ticker,name\nAAA,Alpha\nBBB,Beta\n")
    (tmp_path / "inputs" / "right.csv").write_text("ticker,name\nBBB,Beta\n")
    (tmp_path / "inputs" / "params.json").write_text(
        json.dumps({
            "inputs": {"left": "left.csv", "right": "right.csv"},
            "output": "deliverable/output.csv",
            "seed": 1,
        })
    )
    script = tmp_path / "run.py"
    script.write_text(_source("set_difference"))

    done = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "deliverable" / "output.csv").read_text() == "ticker,name\nAAA,Alpha\n"


def test_the_summary_stats_seed_summarises_numeric_columns(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "deliverable").mkdir()
    (tmp_path / "inputs" / "data.csv").write_text("name,weight\nAAA,1\nBBB,3\n")
    (tmp_path / "inputs" / "params.json").write_text(
        json.dumps({
            "inputs": {"dataset": "data.csv"},
            "output": "deliverable/output.csv",
            "seed": 1,
        })
    )
    script = tmp_path / "run.py"
    script.write_text(_source("summary_stats"))

    done = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    rows = (tmp_path / "deliverable" / "output.csv").read_text().splitlines()
    assert rows[0] == "column,count,min,max,mean"
    assert rows[1] == "weight,2,1.0000,3.0000,2.0000"


def _source(name: str) -> str:
    from ley_khaa.registry.seeds import SEEDS

    return next(seed["source"] for seed in SEEDS if seed["name"] == name)
