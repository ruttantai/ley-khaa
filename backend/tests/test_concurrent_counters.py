"""Backlog item 5: read-modify-write counters lose increments under concurrency."""
import threading

from sqlalchemy import update

from ley_khaa.persistence.orm import WorkflowRow
from ley_khaa.persistence.workflow_repository import WorkflowRepository


def test_concurrent_successes_do_not_lose_an_increment(session_factory, seed_workflow):
    """Ten threads, ten increments. A read-modify-write loses some of them; an
    atomic UPDATE does not."""
    name = seed_workflow
    barrier = threading.Barrier(10, timeout=5)

    def bump():
        local = session_factory()
        try:
            barrier.wait()
            WorkflowRepository(local).record_success(name)
        finally:
            local.close()

    threads = [threading.Thread(target=bump) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    session = session_factory()
    try:
        assert WorkflowRepository(session).get(name).runs_ok == 10
    finally:
        session.close()


def test_the_alias_compare_and_swap_matches_live_and_rejects_stale(session_factory, seed_workflow):
    """record_success's alias guard is a compare-and-swap: `WHERE
    operation_aliases == current`. This asserts that predicate's two
    behaviours directly, rather than through two threads racing
    record_success.

    A two-thread version of this test (written first, and replaced by this
    one) cannot fail: the GIL plus SQLite's write lock serialise two Python
    threads on two connections to one file completely, so the second thread
    always reads the row after the first has already committed — there is
    never a "current" value that is actually stale by the time the second
    thread's UPDATE runs. Confirmed two ways: deleting the CAS predicate
    entirely (last-writer-wins) left it green, and so did restoring the
    complete original pre-fix record_success (ORM read-modify-write, no CAS
    at all), five times in a row. A test that cannot fail under the bug it
    exists to catch is worse than no test — the brief calls this test out by
    name as the guard's own safety net.

    This is also the shape that runs meaningfully against Postgres: two real
    OS threads sharing SQLite's single-writer lock can never demonstrate a
    genuinely interleaved write, but a direct WHERE-clause check needs no
    concurrency to prove the predicate itself works — see the report for the
    live postgres:16 verification this exact shape was run against.
    """
    name = seed_workflow
    session = session_factory()
    try:
        row = WorkflowRepository(session).get(name)
        current = list(row.operation_aliases or [])

        live = session.execute(
            update(WorkflowRow)
            .where(WorkflowRow.name == name, WorkflowRow.operation_aliases == current)
            .values(operation_aliases=current + ["compare the books"])
        )
        session.commit()
        assert live.rowcount == 1, "the CAS must match when `current` reflects the live row"

        stale = session.execute(
            update(WorkflowRow)
            # `current` is now one write behind the row it's compared against.
            .where(WorkflowRow.name == name, WorkflowRow.operation_aliases == current)
            .values(operation_aliases=current + ["reconcile the books"])
        )
        session.commit()
        assert stale.rowcount == 0, "the CAS must reject a `current` the row has since moved past"

        aliases = WorkflowRepository(session).get(name).operation_aliases
        assert "compare the books" in aliases
        assert "reconcile the books" not in aliases
    finally:
        session.close()


def test_a_learned_alias_survives_a_write_injected_between_the_cas_s_read_and_its_own_update(
    session_factory, seed_workflow, monkeypatch
):
    """record_success's retry loop exists for exactly this: another writer's
    commit landing between this call's read of `current` and its own UPDATE.
    Two real threads cannot reproduce that interleaving reliably (see the
    test above), so this forces it deterministically — a second writer commits
    a different alias, through an independent session, at the one moment the
    retry loop is reading its own `current` for the first time.
    """
    name = seed_workflow
    repo_session = session_factory()
    other_session = session_factory()
    repo = WorkflowRepository(repo_session)
    real_row = WorkflowRepository._row
    calls = {"n": 0}

    def intercepting_row(self, workflow_name):
        calls["n"] += 1
        row = real_row(self, workflow_name)
        if calls["n"] == 1:
            # Land a concurrent writer's commit right after THIS call's first
            # read of `current`, before its own UPDATE runs — the exact
            # interleaving the retry loop exists to survive.
            WorkflowRepository(other_session).record_success(
                workflow_name, learned_alias="a concurrent alias"
            )
        return row

    monkeypatch.setattr(WorkflowRepository, "_row", intercepting_row)
    try:
        repo.record_success(name, learned_alias="my alias")
    finally:
        repo_session.close()
        other_session.close()

    session = session_factory()
    try:
        aliases = WorkflowRepository(session).get(name).operation_aliases
    finally:
        session.close()

    assert "a concurrent alias" in aliases, "the interleaved writer's alias must survive"
    assert "my alias" in aliases, "the retry must still land this call's own alias"
