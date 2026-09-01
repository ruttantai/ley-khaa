import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.discord.client import DiscordAdapter, flatten

PAYLOADS = Path(__file__).resolve().parent / "fixtures" / "payloads"
ALLOWED = frozenset({"998877665544332211"})


def _payload(name: str) -> dict:
    return json.loads((PAYLOADS / f"{name}.json").read_text())


def _adapter(**kwargs):
    ingested: list[dict] = []
    dead: list[dict] = []
    adapter = DiscordAdapter(
        bot_token="not-a-real-token",
        allowed_channels=kwargs.pop("allowed_channels", ALLOWED),
        ingest=kwargs.pop("ingest", None) or ingested.append,
        dead_letter=lambda **kw: dead.append(kw),
    )
    adapter.bot_user_id = kwargs.pop("bot_user_id", "999000000000000001")
    return adapter, ingested, dead


def _message(*, parent_id=None, bot=False):
    """A stand-in for discord.Message with only the attributes flatten reads."""
    return SimpleNamespace(
        id=1180000000000000002,
        content="compare the universes",
        type=SimpleNamespace(value=0),
        created_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
        guild=SimpleNamespace(id=112233445566778899),
        author=SimpleNamespace(id=555000000000000001, bot=bot),
        channel=SimpleNamespace(id=998877665544332211, parent_id=parent_id),
        attachments=[
            SimpleNamespace(
                id=700000000000000001,
                filename="a.png",
                url="https://cdn.example.invalid/a.png",
                content_type="image/png",
            )
        ],
    )


def test_flatten_renders_every_field_translate_reads():
    raw = flatten(_message())
    assert raw["id"] == "1180000000000000002"
    assert raw["channel_id"] == "998877665544332211"
    assert raw["parent_id"] is None
    assert raw["guild_id"] == "112233445566778899"
    assert raw["content"] == "compare the universes"
    assert raw["type"] == 0
    assert raw["author"] == {"id": "555000000000000001", "bot": False}
    assert datetime.fromisoformat(raw["timestamp"]).tzinfo is not None
    assert raw["attachments"] == [
        {
            "id": "700000000000000001",
            "filename": "a.png",
            "url": "https://cdn.example.invalid/a.png",
            "content_type": "image/png",
        }
    ]


def test_flatten_carries_the_parent_channel_of_a_thread():
    """The one fact only the live object knows, and the one the allowlist and
    the conversation id both depend on."""
    raw = flatten(_message(parent_id=998877665544332211))
    assert raw["parent_id"] == "998877665544332211"


def test_flatten_survives_a_message_with_no_guild():
    """A DM has guild None. flatten must render it, not raise — translate is
    what decides to drop it (§9: no DMs in this phase)."""
    message = _message()
    message.guild = None
    assert flatten(message)["guild_id"] is None


def test_flattened_output_is_what_translate_accepts():
    from ley_khaa.adapters.discord.translate import translate

    raw = translate(
        flatten(_message()), allowed_channels=ALLOWED, bot_user_id="999000000000000001"
    )
    assert raw is not None and raw["source"] == "discord"


def test_the_adapter_is_named_discord():
    adapter, _, _ = _adapter()
    assert adapter.name == "discord"


def test_a_channel_message_is_ingested():
    adapter, ingested, dead = _adapter()
    asyncio.run(adapter._handle(_payload("discord_channel_message")))
    assert len(ingested) == 1 and dead == []


def test_a_bot_message_is_neither_ingested_nor_dead_lettered():
    adapter, ingested, dead = _adapter()
    asyncio.run(adapter._handle(_payload("discord_bot_message")))
    assert (ingested, dead) == ([], [])


def test_a_malformed_message_is_dead_lettered_as_inbound():
    payload = _payload("discord_channel_message")
    payload["timestamp"] = "yesterday"
    adapter, ingested, dead = _adapter()

    asyncio.run(adapter._handle(payload))

    assert ingested == []
    assert dead[0]["kind"] == "inbound" and dead[0]["source"] == "discord"


def test_a_failing_ingest_is_dead_lettered_and_does_not_escape():
    def boom(_raw):
        raise RuntimeError("the database is down")

    adapter, _, dead = _adapter(ingest=boom)
    asyncio.run(adapter._handle(_payload("discord_channel_message")))

    assert "the database is down" in dead[0]["reason"]


def test_ingest_runs_off_the_event_loop():
    loop_thread = threading.get_ident()
    seen: list[int] = []
    adapter, _, _ = _adapter(ingest=lambda _raw: seen.append(threading.get_ident()))

    asyncio.run(adapter._handle(_payload("discord_channel_message")))

    assert seen and seen[0] != loop_thread


def test_notify_posts_into_the_thread_named_by_the_conversation_id():
    sent = {}

    class FakeChannel:
        async def send(self, text):
            sent["text"] = text

    class FakeClient:
        def get_channel(self, channel_id):
            sent["channel_id"] = channel_id
            return FakeChannel()

    adapter, _, _ = _adapter()
    adapter.client = FakeClient()
    asyncio.run(
        adapter.notify(
            Destination(
                source="discord",
                conversation_id="discord:112233445566778899:998877665544332211:1180000000000000002",
            ),
            "the question",
        )
    )

    assert sent["channel_id"] == 1180000000000000002
    assert sent["text"] == "the question"


def test_no_token_is_ever_in_the_repr():
    adapter, _, _ = _adapter()
    assert "not-a-real-token" not in repr(adapter)
