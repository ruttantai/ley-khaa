import pytest

from ley_khaa.domain.states import TaskState, InvalidTransition
from ley_khaa.interpreter.spec import TaskSpec
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


def test_claim_moves_a_task_to_a_valid_target(session):
    repo = TaskRepository(session)
    task = repo.create(project="p", title="a", source_message_ids=[])
    assert repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    assert repo.get(task.id).state == TaskState.CLASSIFIED.value


def test_claim_to_an_invalid_target_raises(session):
    repo = TaskRepository(session)
    task = repo.create(project="p", title="a", source_message_ids=[])
    with pytest.raises(InvalidTransition):
        repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.DONE)


def test_claim_on_a_missing_task_does_not_raise_and_reports_no_win(session):
    """claim()'s WHERE guard just matches nothing for an unknown id — unlike the
    now-deleted update_state(), it never raised KeyError here, it simply lost."""
    repo = TaskRepository(session)
    assert repo.claim("nope", expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED) is False


# --- Autonomy foundation: claim, spec, recommendation/override, counters ---


def _task(repo):
    return repo.create(project="default", title="t", source_message_ids=["m1"])


def test_claim_wins_once_and_only_once(session):
    repo = TaskRepository(session)
    task = _task(repo)
    assert repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    # The loser of the race must get False, not an exception: two concurrent
    # sweeps advancing the same task is normal, not an error.
    assert not repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    assert repo.get(task.id).state == TaskState.CLASSIFIED.value


def test_save_spec_round_trips(session):
    repo = TaskRepository(session)
    task = _task(repo)
    spec = TaskSpec(intent="i", operation="o", output_format="f", certainty=0.7)
    repo.save_spec(task.id, spec)
    assert TaskSpec.model_validate(repo.get(task.id).spec).intent == "i"


def test_effective_mode_prefers_the_human_override(session):
    repo = TaskRepository(session)
    task = _task(repo)
    repo.save_recommendation(task.id, mode="suggest", confidence=0.4, risk=0.7, reason="r")
    assert repo.get(task.id).effective_mode == "suggest"
    repo.set_override(task.id, "auto")
    assert repo.get(task.id).effective_mode == "auto"
    # Clearing the override falls back to the recommendation rather than sticking.
    repo.set_override(task.id, None)
    assert repo.get(task.id).effective_mode == "suggest"


def test_append_source_messages_does_not_duplicate(session):
    repo = TaskRepository(session)
    task = _task(repo)
    repo.append_source_messages(task.id, ["m2", "m1"])
    assert repo.get(task.id).source_message_ids == ["m1", "m2"]


def test_counters_increment_and_return_the_new_value(session):
    repo = TaskRepository(session)
    task = _task(repo)
    assert repo.increment_interpret_attempts(task.id) == 1
    assert repo.increment_interpret_attempts(task.id) == 2
    assert repo.increment_clarification_rounds(task.id) == 1


def test_list_by_state_filters(session):
    repo = TaskRepository(session)
    a, b = _task(repo), _task(repo)
    repo.claim(a.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    ids = [t.id for t in repo.list_by_state(TaskState.CLASSIFIED)]
    assert ids == [a.id]
    assert b.id not in ids
