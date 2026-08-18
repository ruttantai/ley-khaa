import pytest

from ley_khaa.domain.states import TaskState, InvalidTransition
from ley_khaa.persistence.repository import TaskRepository


def test_create_starts_in_received(session):
    repo = TaskRepository(session)
    task = repo.create(project="default", title="compare universes", source_message_ids=["m1"])
    assert task.state == TaskState.RECEIVED.value
    assert task.id
    assert task.source_message_ids == ["m1"]


def test_get_and_list(session):
    repo = TaskRepository(session)
    a = repo.create(project="p", title="a", source_message_ids=[])
    b = repo.create(project="p", title="b", source_message_ids=[])
    assert repo.get(a.id).title == "a"
    assert {t.id for t in repo.list()} == {a.id, b.id}


def test_update_state_valid(session):
    repo = TaskRepository(session)
    task = repo.create(project="p", title="a", source_message_ids=[])
    updated = repo.update_state(task.id, TaskState.CLASSIFIED)
    assert updated.state == TaskState.CLASSIFIED.value


def test_update_state_invalid_raises(session):
    repo = TaskRepository(session)
    task = repo.create(project="p", title="a", source_message_ids=[])
    with pytest.raises(InvalidTransition):
        repo.update_state(task.id, TaskState.DONE)


def test_update_state_missing_raises(session):
    repo = TaskRepository(session)
    with pytest.raises(KeyError):
        repo.update_state("nope", TaskState.CLASSIFIED)
