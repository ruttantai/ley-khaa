"""Backlog item 5: read-modify-write counters lose increments under concurrency."""
import threading

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


def test_a_learned_alias_is_not_lost_to_a_concurrent_success(session_factory, seed_workflow):
    name = seed_workflow
    barrier = threading.Barrier(2, timeout=5)

    def bump(alias):
        local = session_factory()
        try:
            barrier.wait()
            WorkflowRepository(local).record_success(name, learned_alias=alias)
        finally:
            local.close()

    threads = [
        threading.Thread(target=bump, args=("compare the books",)),
        threading.Thread(target=bump, args=("reconcile the books",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    session = session_factory()
    try:
        aliases = WorkflowRepository(session).get(name).operation_aliases
        assert "compare the books" in aliases
        assert "reconcile the books" in aliases
    finally:
        session.close()
