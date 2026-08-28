from ley_khaa.persistence.orm import MemoryRow, TaskRow, WorkflowRow


def test_a_workflow_row_carries_its_provenance_and_its_hash(session):
    """A promoted capability has to be traceable back to the run that proved it.

    Without promoted_from_task_id there is no way to answer "where did this
    code come from?", which is the question the whole bundle design exists to
    answer.
    """
    row = WorkflowRow(
        id="w1",
        name="set_difference",
        description="rows in A missing from B",
        operation_aliases=["set_difference"],
        output_format="csv",
        inputs=[{"role": "left", "suffixes": [".csv"]}],
        source="print('hi')",
        source_sha256="abc",
        origin="promoted",
        promoted_from_task_id="t1",
    )
    session.add(row)
    session.commit()

    stored = session.get(WorkflowRow, "w1")
    assert stored.origin == "promoted"
    assert stored.promoted_from_task_id == "t1"
    assert stored.runs_ok == 0 and stored.runs_failed == 0
    assert stored.quarantined is False


def test_a_memory_row_is_scoped_to_a_project(session):
    """Memory must never leak a spec from one project into another."""
    session.add(
        MemoryRow(
            id="m1",
            project="acme",
            fingerprint="deadbeef",
            intent="compare the universes",
            spec={"operation": "set_difference"},
            source_task_id="t1",
        )
    )
    session.commit()

    stored = session.get(MemoryRow, "m1")
    assert stored.project == "acme"
    assert stored.times_seen == 1


def test_a_task_remembers_where_its_spec_came_from(session):
    """familiarity feeds the dial; remembered_from_task_id feeds the dashboard."""
    session.add(TaskRow(id="t2", state="received"))
    session.commit()

    stored = session.get(TaskRow, "t2")
    assert stored.familiarity == 0
    assert stored.remembered_from_task_id is None
