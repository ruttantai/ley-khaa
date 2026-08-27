import pytest

from ley_khaa.persistence.workflow_repository import DuplicateWorkflow, WorkflowRepository


def _create(repo, name="set_difference", aliases=("set_difference",)):
    return repo.create(
        name=name,
        description="rows in A missing from B",
        operation_aliases=list(aliases),
        output_format="csv",
        inputs=[{"role": "left", "suffixes": [".csv"]}],
        source="print('hi')",
        origin="seed",
    )


def test_creating_a_workflow_hashes_its_source(session):
    """source_sha256 is what lets a manifest prove which code ran."""
    import hashlib

    repo = WorkflowRepository(session)
    row = _create(repo)

    assert row.source_sha256 == hashlib.sha256(b"print('hi')").hexdigest()


def test_a_taken_name_is_refused(session):
    repo = WorkflowRepository(session)
    _create(repo)
    with pytest.raises(DuplicateWorkflow):
        _create(repo)


def test_active_excludes_quarantined_workflows(session):
    repo = WorkflowRepository(session)
    _create(repo, name="good")
    _create(repo, name="bad")
    repo.record_failure("bad")

    assert [w.name for w in repo.active()] == ["good"]
    assert len(repo.list()) == 2


def test_a_failure_quarantines_immediately(session):
    """One wrong answer is enough. A workflow that just produced garbage must
    not be handed the next matching request as though nothing happened."""
    repo = WorkflowRepository(session)
    _create(repo)
    row = repo.record_failure("set_difference")

    assert row.quarantined is True
    assert row.runs_failed == 1


def test_success_records_use_and_learns_an_alias(session):
    """The learning loop: a phrasing the model matched, that then passed, is a
    free deterministic hit forever after."""
    repo = WorkflowRepository(session)
    _create(repo)
    row = repo.record_success("set_difference", learned_alias="compare_lists")

    assert row.runs_ok == 1
    assert row.last_used_at is not None
    assert set(row.operation_aliases) == {"set_difference", "compare_lists"}


def test_a_known_alias_is_not_added_twice(session):
    repo = WorkflowRepository(session)
    _create(repo)
    repo.record_success("set_difference", learned_alias="set_difference")

    assert repo.get("set_difference").operation_aliases == ["set_difference"]


def test_unquarantine_lets_a_workflow_match_again(session):
    repo = WorkflowRepository(session)
    _create(repo)
    repo.record_failure("set_difference")
    repo.unquarantine("set_difference")

    assert repo.get("set_difference").quarantined is False
    # The failure itself stays on the record.
    assert repo.get("set_difference").runs_failed == 1
