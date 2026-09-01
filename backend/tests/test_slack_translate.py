import json
from datetime import datetime
from pathlib import Path

import pytest

from ley_khaa.adapters.base import TranslationError
from ley_khaa.adapters.slack.translate import conversation_parts, translate

PAYLOADS = Path(__file__).resolve().parent / "fixtures" / "payloads"
ALLOWED = frozenset({"C0ALLOWED1"})
BOT = "U0BOT0001"


def _payload(name: str) -> dict:
    return json.loads((PAYLOADS / f"{name}.json").read_text())


def _translate(name: str, **kwargs):
    kwargs.setdefault("allowed_channels", ALLOWED)
    kwargs.setdefault("bot_user_id", BOT)
    return translate(_payload(name), **kwargs)


def test_a_channel_message_becomes_an_intake_dict():
    raw = _translate("slack_channel_message")
    assert raw["source"] == "slack"
    assert raw["client"] == "T0SYNTH01", "client is the workspace id — ProjectRouter binds on it"
    assert raw["author"] == "U0HUMAN01"
    assert raw["text"].startswith("compare the Bloomberg universe")


def test_a_top_level_message_threads_under_itself():
    """No thread_ts yet, so the conversation is anchored on this message's own
    ts — which is exactly the thread_ts Slack will give every reply to it."""
    raw = _translate("slack_channel_message")
    assert raw["conversation_id"] == "slack:T0SYNTH01:C0ALLOWED1:1756600000.000100"


def test_a_thread_reply_lands_in_the_same_conversation_as_its_parent():
    """This is the clarification loop: the answer must join the task's own
    conversation, not start a new one."""
    parent = _translate("slack_channel_message")
    reply = _translate("slack_thread_reply")
    assert reply["conversation_id"] == parent["conversation_id"]


def test_the_external_id_is_namespaced_by_channel():
    """MessageRow.external_id is globally unique but a Slack ts is unique only
    within a channel, so a bare ts would let one channel's message silently
    dedupe away another channel's."""
    raw = _translate("slack_channel_message")
    assert raw["external_id"] == "slack:C0ALLOWED1:1756600000.000100"


def test_the_timestamp_is_iso_because_the_gateway_parses_it_that_way():
    """IntakeGateway.accept does datetime.fromisoformat(raw['timestamp']).
    A Slack epoch float would raise there, not here."""
    raw = _translate("slack_channel_message")
    parsed = datetime.fromisoformat(raw["timestamp"])
    assert parsed.year == 2025 or parsed.year == 2026
    assert parsed.tzinfo is not None


def test_a_message_from_an_unlisted_channel_is_dropped():
    payload = _payload("slack_channel_message")
    payload["event"]["channel"] = "C0NOTLISTED"
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_an_empty_allowlist_drops_everything():
    assert (
        translate(_payload("slack_channel_message"), allowed_channels=frozenset(), bot_user_id=BOT)
        is None
    )


def test_the_bots_own_message_is_dropped():
    """Load-bearing, not hygiene: the bot posts into the channel it reads, so
    without this every notification is ingested as new work and the system
    feeds itself without limit."""
    assert _translate("slack_bot_message") is None


def test_a_bot_message_is_dropped_by_bot_id_alone_even_without_the_bot_message_subtype():
    """Isolates the bot_id filter from the subtype filter.

    slack_bot_message.json carries BOTH subtype "bot_message" and bot_id, so
    deleting the bot_id check alone does not fail
    test_the_bots_own_message_is_dropped — the subtype check silently covers
    for it. A modern Slack app posting through a Bot User OAuth token often
    carries bot_id with no subtype at all, which is exactly this fixture."""
    payload = _payload("slack_channel_message")
    payload["event"]["bot_id"] = "B0SYNTH01"
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_message_from_the_bots_user_id_is_dropped():
    """The same filter from the other direction: a Slack app posting as a user
    token carries `user`, not `bot_id`."""
    payload = _payload("slack_channel_message")
    payload["event"]["user"] = BOT
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_message_with_no_author_at_all_is_dropped():
    """Without this, author=None would flow into the returned dict and raise
    an uncaught pydantic ValidationError one layer past this module, inside
    IntakeGateway/Message(author: str) — not a TranslationError this module
    raises itself."""
    payload = _payload("slack_channel_message")
    del payload["event"]["user"]
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_an_edited_message_is_dropped():
    payload = _payload("slack_channel_message")
    payload["event"]["subtype"] = "message_changed"
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_non_message_event_is_dropped():
    payload = _payload("slack_channel_message")
    payload["event"]["type"] = "reaction_added"
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_direct_message_is_dropped():
    """Spec §9: threads only, no DMs in this phase."""
    payload = _payload("slack_channel_message")
    payload["event"]["channel_type"] = "im"
    payload["event"]["channel"] = "D0SYNTH01"
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_direct_message_is_dropped_by_channel_type_alone():
    """Isolates the channel_type filter from the allowlist filter.

    The realistic DM fixture above uses an unlisted "D..." channel id, so
    deleting the channel_type check does not fail it either — the allowlist
    silently covers for it. This uses an ALLOWLISTED channel id with
    channel_type "im" so only the channel_type check can be doing the work."""
    payload = _payload("slack_channel_message")
    payload["event"]["channel_type"] = "im"
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_a_message_with_no_text_is_dropped():
    payload = _payload("slack_channel_message")
    payload["event"]["text"] = "   "
    assert translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT) is None


def test_files_become_attachments_with_the_kinds_the_domain_model_allows():
    """AttachmentKind is text|table|image only — there is no binary kind — so a
    non-image file is carried as `text` with its URL as content. Spec §9:
    attachments are carried, not understood."""
    raw = _translate("slack_file_share")
    assert raw["attachments"] == [
        {
            "kind": "image",
            "name": "screenshot.png",
            "content": "https://files.example.invalid/screenshot.png",
        },
        {
            "kind": "text",
            "name": "holdings.csv",
            "content": "https://files.example.invalid/holdings.csv",
        },
    ]


def test_the_attachments_a_translation_produces_are_valid_domain_attachments():
    """A dict that Attachment(**a) rejects would 500 inside IntakeGateway, one
    layer past where any of these tests look."""
    from ley_khaa.domain.models import Attachment

    raw = _translate("slack_file_share")
    assert [Attachment(**a).kind.value for a in raw["attachments"]] == ["image", "text"]


def test_an_allowlisted_message_with_no_channel_id_is_a_translation_error():
    payload = _payload("slack_channel_message")
    del payload["event"]["channel"]
    with pytest.raises(TranslationError):
        translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT)


def test_an_unparsable_timestamp_is_a_translation_error():
    payload = _payload("slack_channel_message")
    payload["event"]["ts"] = "not-a-timestamp"
    with pytest.raises(TranslationError):
        translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT)


def test_a_message_with_no_ts_is_a_translation_error():
    """A missing ts can't be deduplicated and must not raise some other,
    uncaught exception past this module's boundary. Distinct from the
    unparsable-string case above: that one is caught inside _timestamp()'s
    own except ValueError and would still pass even without the explicit
    isinstance/empty guard in translate() — ts=None reaches float(None),
    which raises TypeError, not ValueError, and would otherwise escape
    uncaught."""
    payload = _payload("slack_channel_message")
    del payload["event"]["ts"]
    with pytest.raises(TranslationError):
        translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT)


def test_conversation_parts_round_trips_what_translate_built():
    """The notifier reconstructs the channel and thread anchor from the
    conversation id — no mapping table (§3.5) — so the two halves of that
    contract are asserted against each other rather than against a literal."""
    raw = _translate("slack_thread_reply")
    team, channel, thread_ts = conversation_parts(raw["conversation_id"])
    assert (team, channel, thread_ts) == ("T0SYNTH01", "C0ALLOWED1", "1756600000.000100")


def test_conversation_parts_refuses_a_foreign_conversation_id():
    with pytest.raises(ValueError):
        conversation_parts("discord:G:C:1")


def test_translate_imports_no_slack_sdk():
    """The pure half must stay importable with no dependency and no network —
    it is the half CI can actually exercise (§3.2). Checked in a FRESH
    interpreter: asserting on sys.modules in-process would pass or fail
    depending on what another test imported first.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import ley_khaa.adapters.slack.translate, sys; "
            "print('slack_sdk' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize("ts", ["inf", "-inf", "nan", "99999999999999"])
def test_a_ts_that_floats_but_will_not_convert_raises_translation_error(ts):
    """Asserted at the TRANSLATE level on purpose. `client._handle` now has a
    blanket `except Exception` that dead-letters, so this fix is invisible from
    there — the outer net would catch an OverflowError just as happily and the
    test would pass with the bug still present.

    float() accepts "inf"/"nan" and a 14-digit epoch, so a float()-only guard
    lets all of them reach datetime.fromtimestamp, which raises
    OverflowError/ValueError one layer past it. This module's contract is that
    it raises TranslationError and nothing else."""
    payload = _payload("slack_channel_message")
    payload["event"]["ts"] = ts

    with pytest.raises(TranslationError):
        translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT)
