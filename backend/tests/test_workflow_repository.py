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
    free deterministic hit forever after.

    expire_all() forces the assertions below to hit the database rather than
    the in-memory object that record_success already mutated: this is what
    catches a regression from reassigning operation_aliases back to an
    in-place `.append()`, which SQLAlchemy never flushes for a JSON column.
    """
    repo = WorkflowRepository(session)
    _create(repo)
    row = repo.record_success("set_difference", learned_alias="compare_lists")

    assert row.runs_ok == 1
    assert row.last_used_at is not None

    session.expire_all()
    persisted = repo.get("set_difference")
    assert set(persisted.operation_aliases) == {"set_difference", "compare_lists"}


def test_record_success_updates_an_already_loaded_reference(session):
    """record_success's bulk UPDATE must not leave a WorkflowRow a caller
    already holds (loaded before the call) showing pre-update values —
    without needing repo.get()/session.expire() afterward to force it fresh.

    SQLAlchemy's default synchronize_session="evaluate" strategy keeps a
    bulk UPDATE's WHERE-matched, already-loaded identity-map objects in sync
    as part of the UPDATE itself, so no follow-up read-then-expire is needed.
    """
    repo = WorkflowRepository(session)
    _create(repo)
    stale = repo.get("set_difference")
    assert stale.runs_ok == 0

    repo.record_success("set_difference")

    assert stale.runs_ok == 1, "an already-loaded reference must reflect the increment"
    assert stale.last_used_at is not None


def test_record_failure_updates_an_already_loaded_reference(session):
    """Same invariant as above, for record_failure's bulk UPDATE."""
    repo = WorkflowRepository(session)
    _create(repo)
    stale = repo.get("set_difference")
    assert stale.runs_failed == 0
    assert stale.quarantined is False

    repo.record_failure("set_difference")

    assert stale.runs_failed == 1, "an already-loaded reference must reflect the increment"
    assert stale.quarantined is True, "an already-loaded reference must reflect the quarantine"


def test_a_known_alias_is_not_added_twice(session):
    repo = WorkflowRepository(session)
    _create(repo)
    repo.record_success("set_difference", learned_alias="set_difference")

    session.expire_all()
    assert repo.get("set_difference").operation_aliases == ["set_difference"]


def test_unquarantine_lets_a_workflow_match_again(session):
    repo = WorkflowRepository(session)
    _create(repo)
    repo.record_failure("set_difference")
    repo.unquarantine("set_difference")

    assert repo.get("set_difference").quarantined is False
    # The failure itself stays on the record.
    assert repo.get("set_difference").runs_failed == 1


def test_deleting_a_workflow_removes_it(session):
    repo = WorkflowRepository(session)
    _create(repo)
    repo.delete("set_difference")

    assert repo.get("set_difference") is None
    assert repo.list() == []


@pytest.mark.parametrize(
    "call",
    [
        lambda repo: repo.record_success("no_such_workflow"),
        lambda repo: repo.record_failure("no_such_workflow"),
        lambda repo: repo.unquarantine("no_such_workflow"),
        lambda repo: repo.delete("no_such_workflow"),
    ],
    ids=["record_success", "record_failure", "unquarantine", "delete"],
)
def test_an_unknown_name_raises_key_error(session, call):
    """Every name-taking method routes through _row, and api/app.py's
    KeyError handler is what turns that into a 404 for the registry routes a
    later task wires up — a typo in any one call site must not go unnoticed."""
    repo = WorkflowRepository(session)
    with pytest.raises(KeyError):
        call(repo)
