"""A poisoned task tells someone (item 16, spec §3.2/§3.6).

_fail_poison (dispatcher.py) is the one path to FAILED that had no notifier at
all before this: a task died and the only way a human found out was looking at
the dashboard. This pins the fix the same way test_notifier_wiring.py pins
TaskDriver's: a RecordingNotifier must actually receive something naming the
task when the poison ceiling trips.
"""
from dataclasses import replace

from ley_khaa.adapters.notifier import RecordingNotifier
from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.orchestrator import dispatcher as dispatcher_module
from ley_khaa.orchestrator.dispatcher import Dispatcher
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _poisoned_task(session, *, project="acme"):
    """A task one channel message deep, already over the lease-attempt cap —
    the same shape _fail_poison acts on in test_dispatcher.py's own poison test,
    but with a real source message so there is somewhere to answer into."""
    message = MessageRepository(session).add(
        Message(
            source="slack",
            client="T1",
            conversation_id="slack:T1:C1:100.1",
            author="U1",
            text="do the thing",
            external_id="slack:C1:100.1",
        )
    )
    repo = TaskRepository(session)
    row = repo.create(project=project, title="poison", source_message_ids=[message.id])
    row.lease_attempts = 99
    session.commit()
    return row.id


def test_a_poisoned_task_notifies_its_channel(session_factory, monkeypatch):
    from ley_khaa.config import settings as real_settings

    monkeypatch.setattr(
        dispatcher_module, "settings", replace(real_settings, max_lease_attempts=3)
    )

    with session_factory() as session:
        task_id = _poisoned_task(session)

    notifier = RecordingNotifier()
    dispatcher = Dispatcher(session_factory, drive=lambda s, t: None, notifier=notifier)
    dispatcher_module.asyncio.run(dispatcher.tick())

    with session_factory() as session:
        row = TaskRepository(session).get(task_id)
        assert TaskState(row.state) is TaskState.FAILED

    assert len(notifier.sent) == 1
    dest, text = notifier.sent[0]
    assert dest.source == "slack"
    assert dest.conversation_id == "slack:T1:C1:100.1"
    assert "poison" in text
    assert "failed" in text


def test_a_none_notifier_is_a_no_op_not_a_crash(session_factory, monkeypatch):
    """Every existing construction site omits notifier=; a poisoned task there
    must still fail cleanly rather than raising on a missing notifier."""
    from ley_khaa.config import settings as real_settings

    monkeypatch.setattr(
        dispatcher_module, "settings", replace(real_settings, max_lease_attempts=3)
    )

    with session_factory() as session:
        task_id = _poisoned_task(session)

    dispatcher = Dispatcher(session_factory, drive=lambda s, t: None)
    dispatcher_module.asyncio.run(dispatcher.tick())

    with session_factory() as session:
        row = TaskRepository(session).get(task_id)
        assert TaskState(row.state) is TaskState.FAILED
