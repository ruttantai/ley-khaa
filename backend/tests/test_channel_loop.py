"""The claim of spec §1, offline: no network, no tokens, real everything else."""
import json
from pathlib import Path

import pytest

from ley_khaa.adapters.notifier import RecordingNotifier
from ley_khaa.adapters.slack.translate import conversation_parts, translate
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.project_repository import ProjectRepository
from ley_khaa.persistence.repository import TaskRepository

PAYLOADS = Path(__file__).resolve().parent / "fixtures" / "payloads"
ALLOWED = frozenset({"C0ALLOWED1"})
BOT = "U0BOT0001"


class FakeAdapter:
    """A channel that exists only in memory.

    It uses the REAL translate(), so a defect in the allowlist, the
    self-message filter or thread derivation shows up here as a failure of the
    loop rather than being hidden behind a hand-written dict.
    """

    name = "slack"

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.ingested: list[dict] = []
        self.dropped: list[dict] = []

    def deliver(self, payload: dict):
        raw = translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT)
        if raw is None:
            self.dropped.append(payload)
            return None
        self.ingested.append(raw)
        return self.orchestrator.ingest(raw)


@pytest.fixture
def channel(session):
    notifier = RecordingNotifier()
    projects = ProjectRepository(session)
    projects.create("default", description="")
    projects.create("markets", display_name="Markets", description="index and universe work")
    # A binding, so routing is deterministic offline: the heuristic LLM makes no
    # stage-2 project call, and a test that depended on one would be asserting
    # on the stand-in rather than on routing.
    projects.bind("slack", "T0SYNTH01", "", "markets", stage="seed")

    orchestrator = Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
        projects=projects,
        notifier=notifier,
    )
    return FakeAdapter(orchestrator), notifier, session


def _payload(name: str) -> dict:
    return json.loads((PAYLOADS / f"{name}.json").read_text())


def test_a_channel_message_becomes_a_task_in_the_bound_project(channel):
    adapter, _notifier, session = channel

    result = adapter.deliver(_payload("slack_channel_message"))

    assert result.task_ids
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.project == "markets"


def test_the_bot_asks_its_question_in_the_originating_thread(channel):
    """The fixture's own text is a FULLY specified request, so it never parks
    and never asks anything. The text is overridden with one that omits the
    output format — the same request the reply test uses — so this test
    actually exercises the question coming back. The plan's conditional skip is
    deliberately gone: this project's bar is 0 skipped, and a skip here would
    quietly retire the assertion that the question lands in the right thread."""
    adapter, notifier, session = channel

    incomplete = _payload("slack_channel_message")
    incomplete["event"]["text"] = "compare the holdings against the portfolio"
    result = adapter.deliver(incomplete)
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.NEEDS_CLARIFICATION.value

    assert notifier.sent, "a parked task told nobody"
    dest, text = notifier.sent[-1]
    _team, channel_id, thread_ts = conversation_parts(dest.conversation_id)
    assert channel_id == "C0ALLOWED1"
    assert thread_ts == "1756600000.000100", "the answer must land in the request's own thread"
    assert task.open_question in text


def test_a_thread_reply_answers_the_question_and_the_task_resumes(channel):
    """The whole loop: a request that cannot be started, a question in the
    thread, an ordinary reply in that thread, and work that moves on."""
    adapter, _notifier, session = channel
    repo = TaskRepository(session)

    incomplete = _payload("slack_channel_message")
    incomplete["event"]["text"] = "compare the holdings against the portfolio"
    result = adapter.deliver(incomplete)
    task = repo.get(result.task_ids[0])
    assert task.state == TaskState.NEEDS_CLARIFICATION.value

    reply = adapter.deliver(_payload("slack_thread_reply"))

    assert reply.replied_to_task_id == task.id
    assert repo.get(task.id).state == TaskState.AWAITING_APPROVAL.value


def test_the_bots_own_notification_is_never_ingested(channel):
    """Without the self-message filter the bot posts into the channel it reads
    and the system feeds itself without limit. This asserts on the OUTCOME —
    no message, no candidate, no task — not merely on translate() returning
    None, which test_slack_translate.py already covers."""
    adapter, _notifier, session = channel
    adapter.deliver(_payload("slack_channel_message"))
    tasks_before = len(TaskRepository(session).list())
    messages_before = len(MessageRepository(session).list_for_conversation(
        "slack:T0SYNTH01:C0ALLOWED1:1756600000.000100"
    ))

    assert adapter.deliver(_payload("slack_bot_message")) is None

    assert len(TaskRepository(session).list()) == tasks_before
    assert (
        len(
            MessageRepository(session).list_for_conversation(
                "slack:T0SYNTH01:C0ALLOWED1:1756600000.000100"
            )
        )
        == messages_before
    ), "the bot's own message reached storage"


def test_a_message_from_an_unlisted_channel_is_provably_not_persisted(channel):
    """Spec §8. Asserted on the database, not on the return value: the
    allowlist's promise is that nothing is stored, and only the store can say."""
    adapter, _notifier, session = channel
    payload = _payload("slack_channel_message")
    payload["event"]["channel"] = "C0NOTLISTED"

    assert adapter.deliver(payload) is None

    assert MessageRepository(session).list_for_conversation(
        "slack:T0SYNTH01:C0NOTLISTED:1756600000.000100"
    ) == []
    assert TaskRepository(session).list() == []


def test_redelivery_of_the_same_message_creates_no_second_task(channel):
    """Slack and Discord both redeliver on timeout. MessageRepository.add
    dedupes on external_id, and this is the test that the adapter's namespaced
    key actually reaches it."""
    adapter, _notifier, session = channel
    payload = _payload("slack_channel_message")

    first = adapter.deliver(payload)
    second = adapter.deliver(payload)

    assert first.message_id == second.message_id
    assert len(TaskRepository(session).list()) == 1


def test_the_whole_loop_makes_no_network_call(channel, monkeypatch):
    """The offline claim, enforced rather than asserted in prose: any socket
    opened anywhere in this flow fails the test."""
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("the offline loop opened a socket")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    adapter, _notifier, _session = channel

    adapter.deliver(_payload("slack_channel_message"))
    adapter.deliver(_payload("slack_thread_reply"))
