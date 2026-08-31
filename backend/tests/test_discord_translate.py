import json
from datetime import datetime
from pathlib import Path

import pytest

from ley_khaa.adapters.base import TranslationError
from ley_khaa.adapters.discord.translate import conversation_parts, translate

PAYLOADS = Path(__file__).resolve().parent / "fixtures" / "payloads"
ALLOWED = frozenset({"998877665544332211"})
BOT = "999000000000000001"


def _payload(name: str) -> dict:
    return json.loads((PAYLOADS / f"{name}.json").read_text())


def _translate(name: str, **kwargs):
    kwargs.setdefault("allowed_channels", ALLOWED)
    kwargs.setdefault("bot_user_id", BOT)
    return translate(_payload(name), **kwargs)


def test_a_channel_message_becomes_an_intake_dict():
    raw = _translate("discord_channel_message")
    assert raw["source"] == "discord"
    assert raw["client"] == "112233445566778899", "client is the guild id — ProjectRouter binds on it"
    assert raw["author"] == "555000000000000001"
    assert raw["text"].startswith("compare the Bloomberg universe")


def test_a_top_level_message_threads_under_itself():
    raw = _translate("discord_channel_message")
    assert raw["conversation_id"] == (
        "discord:112233445566778899:998877665544332211:1180000000000000002"
    )


def test_a_thread_reply_lands_in_the_same_conversation_as_its_parent():
    """A message inside a thread has channel_id == the THREAD's id, so the
    parent channel has to come from parent_id or the reply starts its own
    conversation and the clarification loop never closes."""
    parent = _translate("discord_channel_message")
    reply = _translate("discord_thread_reply")
    assert reply["conversation_id"] == parent["conversation_id"]


def test_a_thread_reply_in_an_allowlisted_channel_is_allowed():
    """The thread's OWN id is not in the allowlist and never will be — Discord
    mints one per thread. Checking channel_id alone would reject every answer
    to every clarifying question."""
    assert _translate("discord_thread_reply") is not None


def test_a_thread_in_an_unlisted_channel_is_still_dropped():
    """The other half of the same rule: widening the check to the parent must
    not turn into ignoring the allowlist."""
    payload = _payload("discord_thread_reply")
    payload["parent_id"] = "000000000000000000"
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_the_external_id_is_namespaced_like_slacks():
    raw = _translate("discord_channel_message")
    assert raw["external_id"] == "discord:998877665544332211:1180000000000000002"


def test_the_timestamp_is_iso_because_the_gateway_parses_it_that_way():
    raw = _translate("discord_channel_message")
    assert datetime.fromisoformat(raw["timestamp"]).tzinfo is not None


def test_a_message_with_no_channel_identifier_at_all_is_a_translation_error():
    """A payload carrying neither key is structurally malformed — an
    integration fault, not channel traffic. It names no channel, so recording
    it cannot leak an unlisted channel's existence. Mirrors the Slack
    translator's missing-`channel` behaviour so a caller can treat both the
    same way."""
    payload = _payload("discord_channel_message")
    del payload["channel_id"]
    del payload["parent_id"]
    with pytest.raises(TranslationError):
        translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT)


def test_a_present_but_null_channel_id_is_a_silent_drop_not_an_error():
    """The keys are PRESENT, so this is not malformed — it is simply not a
    message from an allowlisted channel, which is the normal, silent path.
    Guards the difference between checking key presence and checking
    truthiness."""
    payload = _payload("discord_channel_message")
    payload["channel_id"] = None
    payload["parent_id"] = None
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_message_from_an_unlisted_channel_is_dropped():
    payload = _payload("discord_channel_message")
    payload["channel_id"] = "000000000000000000"
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_an_empty_allowlist_drops_everything():
    assert (
        translate(
            _payload("discord_channel_message"), allowed_channels=frozenset(), bot_user_id=BOT
        )
        is None
    )


def test_a_bot_authored_message_is_dropped():
    """Load-bearing: the bot posts into the channel it reads."""
    assert _translate("discord_bot_message") is None


def test_a_bot_authored_message_is_dropped_by_bot_flag_alone():
    """Isolates the `author.bot` filter from the bot-user-id filter.

    discord_bot_message.json carries BOTH author.bot=true AND an author id
    equal to BOT, so deleting the `author.bot` check alone would not fail
    test_a_bot_authored_message_is_dropped — the id check silently covers for
    it. This uses a DIFFERENT bot's id (not our own) with bot=true, so only
    the `author.bot` check can be doing the work."""
    payload = _payload("discord_channel_message")
    payload["author"]["bot"] = True
    payload["author"]["id"] = "444000000000000009"
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_message_from_the_bots_user_id_is_dropped():
    """The second guard on the same property. Both exist because either could
    be removed silently otherwise: author.bot covers every bot, and the id
    check covers OUR bot specifically."""
    payload = _payload("discord_channel_message")
    payload["author"]["id"] = BOT
    payload["author"]["bot"] = False
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_system_message_is_dropped():
    """Type 7 is a member-join notice. Only DEFAULT (0) and REPLY (19) are
    things a person actually said."""
    payload = _payload("discord_channel_message")
    payload["type"] = 7
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_reply_type_message_is_ingested():
    payload = _payload("discord_channel_message")
    payload["type"] = 19
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is not None


def test_a_message_with_no_content_is_dropped():
    payload = _payload("discord_channel_message")
    payload["content"] = "  "
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_direct_message_is_dropped():
    """No guild id means a DM. Spec §9: threads only, no DMs in this phase."""
    payload = _payload("discord_channel_message")
    payload["guild_id"] = None
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_attachments_become_domain_attachments():
    from ley_khaa.domain.models import Attachment

    raw = _translate("discord_attachment_message")
    assert raw["attachments"] == [
        {
            "kind": "image",
            "name": "screenshot.png",
            "content": "https://cdn.example.invalid/screenshot.png",
        },
        {
            "kind": "text",
            "name": "holdings.csv",
            "content": "https://cdn.example.invalid/holdings.csv",
        },
    ]
    assert [Attachment(**a).kind.value for a in raw["attachments"]] == ["image", "text"]


def test_an_allowlisted_message_with_no_id_is_a_translation_error():
    payload = _payload("discord_channel_message")
    del payload["id"]
    with pytest.raises(TranslationError):
        translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT)


def test_an_unparsable_timestamp_is_a_translation_error():
    payload = _payload("discord_channel_message")
    payload["timestamp"] = "yesterday"
    with pytest.raises(TranslationError):
        translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT)


def test_conversation_parts_round_trips_what_translate_built():
    raw = _translate("discord_thread_reply")
    guild, channel, thread = conversation_parts(raw["conversation_id"])
    assert (guild, channel, thread) == (
        "112233445566778899",
        "998877665544332211",
        "1180000000000000002",
    )


def test_conversation_parts_refuses_a_foreign_conversation_id():
    with pytest.raises(ValueError):
        conversation_parts("slack:T:C:1.0")


def test_translate_imports_no_discord_library():
    """The pure half must stay importable with no dependency and no network —
    discord.py arrives with the connection wrapper in a later task and must
    not be a transitive import of this module. Checked in a FRESH interpreter:
    asserting on sys.modules in-process would pass or fail depending on what
    another test imported first.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ley_khaa.adapters.discord.translate, sys; "
            "print('discord' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"
