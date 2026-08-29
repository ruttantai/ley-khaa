from datetime import datetime, timedelta, timezone

from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.repository import TaskRepository


# Repository.claim() only performs single, legal transitions (it delegates to
# ensure_transition), so a task destined for e.g. EXECUTING or DONE has to be
# walked there through every intermediate state RECEIVED actually permits —
# a direct RECEIVED -> EXECUTING claim raises InvalidTransition.
_PATH: dict[TaskState, list[TaskState]] = {
    TaskState.CLASSIFIED: [TaskState.CLASSIFIED],
    TaskState.INTERPRETED: [TaskState.CLASSIFIED, TaskState.INTERPRETED],
    TaskState.AWAITING_APPROVAL: [
        TaskState.CLASSIFIED,
        TaskState.INTERPRETED,
        TaskState.AWAITING_APPROVAL,
    ],
    TaskState.EXECUTING: [TaskState.CLASSIFIED, TaskState.INTERPRETED, TaskState.EXECUTING],
    TaskState.VALIDATING: [
        TaskState.CLASSIFIED,
        TaskState.INTERPRETED,
        TaskState.EXECUTING,
        TaskState.VALIDATING,
    ],
    TaskState.DONE: [
        TaskState.CLASSIFIED,
        TaskState.INTERPRETED,
        TaskState.EXECUTING,
        TaskState.VALIDATING,
        TaskState.DONE,
    ],
    TaskState.NEEDS_CLARIFICATION: [TaskState.CLASSIFIED, TaskState.NEEDS_CLARIFICATION],
    TaskState.FAILED: [TaskState.FAILED],
}


def _task(repo, *, project="default", state=TaskState.CLASSIFIED):
    row = repo.create(project=project, title="t", source_message_ids=[])
    current = TaskState.RECEIVED
    for target in _PATH[state]:
        assert repo.claim(row.id, expected=current, target=target) is True
        current = target
    return repo.get(row.id)


def test_a_free_task_can_be_claimed(session):
    repo = TaskRepository(session)
    task = _task(repo)
    assert repo.claim_lease(task.id, owner="w1", ttl_seconds=30) is True
    assert repo.get(task.id).lease_owner == "w1"


def test_a_live_lease_cannot_be_stolen(session):
    repo = TaskRepository(session)
    task = _task(repo)
    assert repo.claim_lease(task.id, owner="w1", ttl_seconds=30) is True
    assert repo.claim_lease(task.id, owner="w2", ttl_seconds=30) is False
    assert repo.get(task.id).lease_owner == "w1"


def test_an_expired_lease_can_be_reclaimed_once(session):
    repo = TaskRepository(session)
    task = _task(repo)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert repo.claim_lease(task.id, owner="w1", ttl_seconds=30, now=past) is True
    assert repo.claim_lease(task.id, owner="w2", ttl_seconds=30) is True
    assert repo.get(task.id).lease_owner == "w2"


def test_an_ordinary_claim_does_not_count_as_an_attempt(session):
    """lease_attempts counts RECLAIMS, not claims.

    Incrementing on every claim would count every ordinary hand-off between
    states, so a healthy task that simply passed through several steps would
    trip the attempt cap and fail for no reason at all.
    """
    repo = TaskRepository(session)
    task = _task(repo)
    for _ in range(5):
        assert repo.claim_lease(task.id, owner="w1", ttl_seconds=30) is True
        assert repo.release_lease(task.id, owner="w1") is True
    assert repo.get(task.id).lease_attempts == 0


def test_reclaiming_an_expired_lease_does_count(session):
    repo = TaskRepository(session)
    task = _task(repo)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    repo.claim_lease(task.id, owner="w1", ttl_seconds=30, now=past)
    repo.claim_lease(task.id, owner="w2", ttl_seconds=30)
    assert repo.get(task.id).lease_attempts == 1


def test_a_worker_cannot_release_a_lease_it_no_longer_holds(session):
    """A worker whose lease expired must not be able to clear its successor's —
    that would hand the same live task to a third worker."""
    repo = TaskRepository(session)
    task = _task(repo)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    repo.claim_lease(task.id, owner="w1", ttl_seconds=30, now=past)
    repo.claim_lease(task.id, owner="w2", ttl_seconds=30)

    assert repo.release_lease(task.id, owner="w1") is False
    assert repo.get(task.id).lease_owner == "w2"


def test_a_heartbeat_extends_only_the_holder_s_lease(session):
    repo = TaskRepository(session)
    task = _task(repo)
    repo.claim_lease(task.id, owner="w1", ttl_seconds=1)
    before = repo.get(task.id).lease_expires_at

    assert repo.heartbeat_lease(task.id, owner="w1", ttl_seconds=600) is True
    assert repo.get(task.id).lease_expires_at > before
    assert repo.heartbeat_lease(task.id, owner="w2", ttl_seconds=600) is False


def test_runnable_projects_lists_only_projects_with_work_to_do(session):
    repo = TaskRepository(session)
    _task(repo, project="acme", state=TaskState.CLASSIFIED)
    _task(repo, project="globex", state=TaskState.AWAITING_APPROVAL)  # waiting on a human
    _task(repo, project="initech", state=TaskState.DONE)  # terminal
    assert repo.runnable_projects() == ["acme"]


def test_a_leased_task_is_not_runnable_again_while_the_lease_is_live(session):
    repo = TaskRepository(session)
    task = _task(repo, project="acme")
    repo.claim_lease(task.id, owner="w1", ttl_seconds=30)
    assert repo.runnable_projects() == []
    assert repo.next_runnable("acme") is None


def test_an_expired_lease_makes_the_task_runnable_again(session):
    """This is what makes EXECUTING recoverable — the reason advance_stalled
    could never touch it before."""
    repo = TaskRepository(session)
    task = _task(repo, project="acme", state=TaskState.EXECUTING)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    repo.claim_lease(task.id, owner="dead-worker", ttl_seconds=30, now=past)
    assert repo.runnable_projects() == ["acme"]
    assert repo.next_runnable("acme").id == task.id


def test_next_runnable_is_fifo_within_a_project(session):
    repo = TaskRepository(session)
    first = _task(repo, project="acme")
    _task(repo, project="acme")
    assert repo.next_runnable("acme").id == first.id
