from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.persistence.memory_repository import MemoryRepository


def _spec(inputs=None, output_format="csv", operation="set_difference") -> TaskSpec:
    return TaskSpec(
        intent="compare the universes",
        inputs=inputs if inputs is not None else ["bloomberg universe", "factset"],
        operation=operation,
        output_format=output_format,
        certainty=0.9,
    )


def test_recording_the_same_request_twice_increments_rather_than_duplicates(session):
    """times_seen is what the dial reads. A duplicate row would keep every
    repeat at 1 and the dial would never learn anything."""
    repo = MemoryRepository(session)
    first = repo.record(project="default", fingerprint="abc", intent="i", spec=_spec(), task_id="t1")
    second = repo.record(project="default", fingerprint="abc", intent="i", spec=_spec(), task_id="t2")

    assert first.id == second.id
    assert second.times_seen == 2
    # The first task keeps the credit: it is the run that proved the spec.
    assert second.source_task_id == "t1"
    assert len(repo.for_project("default")) == 1


def test_memory_is_scoped_to_a_project(session):
    repo = MemoryRepository(session)
    repo.record(project="acme", fingerprint="abc", intent="i", spec=_spec(), task_id="t1")

    assert repo.by_fingerprint("acme", "abc") is not None
    assert repo.by_fingerprint("globex", "abc") is None
    assert repo.for_project("globex") == []


def test_the_spec_round_trips(session):
    repo = MemoryRepository(session)
    row = repo.record(project="default", fingerprint="abc", intent="i", spec=_spec(), task_id="t1")

    assert TaskSpec.model_validate(row.spec).operation == _spec().operation
