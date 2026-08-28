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


def test_a_race_between_two_recordings_counts_as_a_repeat_not_an_error(session, monkeypatch):
    """The orchestrator runs concurrent per-project queues: two identical
    requests can finish at once. The loser's check-then-insert must not throw
    once the winner's row lands first — it must fall back to incrementing the
    row the winner created, the same as any other repeat."""
    repo = MemoryRepository(session)
    winner = repo.record(project="default", fingerprint="abc", intent="i", spec=_spec(), task_id="t1")

    real_by_fingerprint = repo.by_fingerprint
    calls = {"n": 0}

    def stale_then_real(project, fingerprint):
        # The loser's own pre-insert check ran before the winner's row
        # existed. Reproduce that stale read once, then behave normally.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_by_fingerprint(project, fingerprint)

    monkeypatch.setattr(repo, "by_fingerprint", stale_then_real)

    loser = repo.record(project="default", fingerprint="abc", intent="i", spec=_spec(), task_id="t2")

    assert loser.id == winner.id
    assert loser.times_seen == 2
    assert loser.source_task_id == "t1"
    assert len(repo.for_project("default")) == 1


def test_recording_with_an_empty_fingerprint_is_a_no_op_not_an_error(session):
    """An empty fingerprint can never be recalled — by_fingerprint refuses it
    by design, and MemoryMatcher.recall (Task 12) short-circuits on it before
    ever querying. Storing it anyway would just be an unrecallable row that
    collides with the next unrecallable row under the unique constraint.
    record() must refuse instead of raising."""
    repo = MemoryRepository(session)

    first = repo.record(project="default", fingerprint="", intent="i", spec=_spec(), task_id="t1")
    second = repo.record(project="default", fingerprint="", intent="i", spec=_spec(), task_id="t2")

    assert first is None
    assert second is None
    assert repo.for_project("default") == []
