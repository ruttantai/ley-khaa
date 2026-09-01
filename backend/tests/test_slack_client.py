import asyncio
import json
import threading
from pathlib import Path

import pytest

from ley_khaa.adapters.slack.client import SlackAdapter

PAYLOADS = Path(__file__).resolve().parent / "fixtures" / "payloads"
ALLOWED = frozenset({"C0ALLOWED1"})


def _payload(name: str) -> dict:
    return json.loads((PAYLOADS / f"{name}.json").read_text())


def _adapter(**kwargs):
    ingested: list[dict] = []
    dead: list[dict] = []
    adapter = SlackAdapter(
        bot_token="xoxb-not-a-real-token",
        app_token="xapp-not-a-real-token",
        allowed_channels=kwargs.pop("allowed_channels", ALLOWED),
        ingest=kwargs.pop("ingest", None) or ingested.append,
        dead_letter=lambda **kw: dead.append(kw),
    )
    adapter.bot_user_id = kwargs.pop("bot_user_id", "U0BOT0001")
    return adapter, ingested, dead


def test_the_adapter_is_named_slack():
    adapter, _, _ = _adapter()
    assert adapter.name == "slack"


def test_a_channel_message_is_ingested():
    adapter, ingested, dead = _adapter()
    asyncio.run(adapter._handle(_payload("slack_channel_message")))

    assert len(ingested) == 1
    assert ingested[0]["source"] == "slack"
    assert dead == []


def test_the_bots_own_message_is_neither_ingested_nor_dead_lettered():
    """A normal drop is silent. If it dead-lettered, the panel would fill with
    the bot's own notifications."""
    adapter, ingested, dead = _adapter()
    asyncio.run(adapter._handle(_payload("slack_bot_message")))

    assert ingested == []
    assert dead == []


def test_an_unlisted_channel_is_neither_ingested_nor_dead_lettered():
    adapter, ingested, dead = _adapter(allowed_channels=frozenset())
    asyncio.run(adapter._handle(_payload("slack_channel_message")))

    assert (ingested, dead) == ([], [])


def test_a_malformed_event_is_dead_lettered_as_inbound():
    payload = _payload("slack_channel_message")
    payload["event"]["ts"] = "not-a-timestamp"
    adapter, ingested, dead = _adapter()

    asyncio.run(adapter._handle(payload))

    assert ingested == []
    assert len(dead) == 1
    assert dead[0]["kind"] == "inbound"
    assert dead[0]["source"] == "slack"


def test_a_failing_ingest_is_dead_lettered_and_does_not_escape():
    """The event loop runs every adapter and the dispatcher. An exception
    escaping here would be swallowed by asyncio at best and take the socket
    down at worst."""

    def boom(_raw):
        raise RuntimeError("the database is down")

    adapter, _, dead = _adapter(ingest=boom)
    asyncio.run(adapter._handle(_payload("slack_channel_message")))

    assert len(dead) == 1
    assert dead[0]["kind"] == "inbound"
    assert "the database is down" in dead[0]["reason"]


def test_ingest_runs_off_the_event_loop():
    """The intake pipeline is synchronous SQLAlchemy. Running it on the loop
    would block every other adapter and the dispatcher for the length of an LLM
    call, so it must be handed to a thread."""
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def record(_raw):
        seen.append(threading.get_ident())

    adapter, _, _ = _adapter(ingest=record)
    asyncio.run(adapter._handle(_payload("slack_channel_message")))

    assert seen and seen[0] != loop_thread


def test_notify_posts_into_the_conversations_thread():
    """The channel and the thread anchor come from the conversation id — no
    mapping table (§3.5)."""
    posted = {}

    class FakeWeb:
        async def chat_postMessage(self, **kwargs):
            posted.update(kwargs)

    from ley_khaa.adapters.base import Destination

    adapter, _, _ = _adapter()
    adapter.web = FakeWeb()
    asyncio.run(
        adapter.notify(
            Destination(
                source="slack",
                conversation_id="slack:T0SYNTH01:C0ALLOWED1:1756600000.000100",
                external_id="slack:C0ALLOWED1:1756600000.000100",
            ),
            "the question",
        )
    )

    assert posted["channel"] == "C0ALLOWED1"
    assert posted["thread_ts"] == "1756600000.000100"
    assert posted["text"] == "the question"


def test_no_token_is_ever_in_the_repr():
    """A token must never be logged, and an adapter ends up in a log line the
    moment anything goes wrong."""
    adapter, _, _ = _adapter()
    assert "xoxb-not-a-real-token" not in repr(adapter)
    assert "xapp-not-a-real-token" not in repr(adapter)


@pytest.mark.parametrize(
    "label,payload",
    [
        ("event is an int", {"event": 5}),
        ("event is a float", {"event": 1.5}),
        ("payload is None", None),
    ],
)
def test_a_structurally_broken_payload_dead_letters_rather_than_escaping(label, payload):
    """`_handle` promises nothing raises out of it, and that promise is load
    bearing in a way peculiar to Slack: `on_request` ACKS the envelope before
    calling `_handle`, so Slack never redelivers, and slack_sdk's listener
    wrapper logs an escaping exception and moves on. The message is then gone
    with no dead letter — silent loss, which is what §3.8 exists to prevent.

    Slack's `_handle` receives the RAW wire payload (Discord's receives
    flatten's output), so it cannot assume any shape at all."""
    adapter, ingested, dead = _adapter()

    asyncio.run(adapter._handle(payload))

    assert ingested == []
    assert len(dead) == 1, f"{label} escaped instead of dead-lettering"
    assert dead[0]["kind"] == "inbound"


@pytest.mark.parametrize("ts", ["inf", "nan", "99999999999999"])
def test_a_timestamp_float_accepts_but_datetime_rejects_is_dead_lettered(ts):
    """float("inf") and float("nan") succeed, and a huge epoch parses fine — so
    a `float()`-only guard lets all three through to datetime.fromtimestamp,
    which raises OverflowError/ValueError one layer past it."""
    payload = _payload("slack_channel_message")
    payload["event"]["ts"] = ts
    adapter, ingested, dead = _adapter()

    asyncio.run(adapter._handle(payload))

    assert ingested == []
    assert len(dead) == 1 and dead[0]["kind"] == "inbound"


def test_a_non_iterable_files_field_is_dead_lettered():
    payload = _payload("slack_channel_message")
    payload["event"]["files"] = 7
    adapter, ingested, dead = _adapter()

    asyncio.run(adapter._handle(payload))

    assert ingested == [] and len(dead) == 1


def test_stop_closes_the_socket_rather_than_merely_disconnecting_it():
    """slack_sdk's disconnect() closes only the current session — it leaves
    `closed` False and auto-reconnect armed, so monitor_current_session()
    re-dials within a ping interval. After shutdown the lifespan has installed
    a NullNotifier, so a re-dialled client would keep ingesting with nothing
    able to answer, and would leak three tasks plus an aiohttp session."""
    calls = []

    class FakeSocket:
        async def close(self):
            calls.append("close")

        async def disconnect(self):
            calls.append("disconnect")

    adapter, _, _ = _adapter()
    adapter.socket = FakeSocket()

    asyncio.run(adapter.stop())

    assert calls == ["close"]
    assert adapter.socket is None
