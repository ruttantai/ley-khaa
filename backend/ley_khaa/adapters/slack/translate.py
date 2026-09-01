"""Slack event envelope -> the raw dict IntakeGateway.accept() already takes.

Pure: no network, no tokens, no I/O, no slack_sdk import. This is where every
decision that can be wrong lives (spec §3.2) — the allowlist, the self-message
filter, thread derivation, the dedupe key — so a defect in any of them is a
unit-test failure rather than something only a real workspace could reveal.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ...domain.models import AttachmentKind
from ..base import TranslationError

SOURCE = "slack"

# Slack tags every non-plain message with a subtype: an edit, a deletion, a
# channel join, a bot post. None of them is a new request, and accepting any of
# them would ingest our own notifications (subtype "bot_message") as work.
_ACCEPTED_SUBTYPE: str | None = None

# Spec §9: threads only, no DMs in this phase. "channel" and "group" are a
# public and a private channel; "im" and "mpim" are direct messages.
_INGESTED_CHANNEL_TYPES = frozenset({"channel", "group"})


def translate(
    payload: dict,
    *,
    allowed_channels: frozenset[str],
    bot_user_id: str | None,
) -> dict | None:
    """Return the raw intake dict, or None if this event is deliberately ignored.

    None and TranslationError mean different things and the caller treats them
    differently: None is a normal drop (unlisted channel, our own message, an
    edit) and is silent; TranslationError is an event that looked like work and
    could not be handled, and is dead-lettered. Conflating them would fill the
    dead-letter panel with ordinary channel traffic and bury the real drops.
    """
    event = payload.get("event") or {}

    # The allowlist is FIRST, before any parsing, so an unlisted channel cannot
    # reach storage by any path — including a TranslationError, which persists a
    # dead letter. Decision #4: being invited to a channel is not consent.
    #
    # A missing `channel` key is a different case from an unlisted one: we
    # cannot even ask "is this allowed?" without it, so it is not a silent
    # drop — it is a malformed event, surfaced via TranslationError. A present
    # channel, however malformed its type or however absent from the
    # allowlist, always returns None here and never raises.
    if "channel" not in event:
        raise TranslationError("a Slack message event has no 'channel'")
    channel = event["channel"]
    if not isinstance(channel, str) or channel not in allowed_channels:
        return None

    if event.get("type") != "message":
        return None
    if event.get("subtype") != _ACCEPTED_SUBTYPE:
        return None
    if event.get("channel_type") not in _INGESTED_CHANNEL_TYPES:
        return None

    # The self-message filter, from both directions: a bot post carries bot_id,
    # an app posting with a user token carries our own user id in `user`.
    if event.get("bot_id"):
        return None
    author = event.get("user")
    if not author or (bot_user_id is not None and author == bot_user_id):
        return None

    text = (event.get("text") or "").strip()
    if not text:
        return None

    ts = event.get("ts")
    if not isinstance(ts, str) or not ts:
        raise TranslationError("a Slack message with no ts cannot be deduplicated")

    # The workspace id is what ProjectRouter binds on. Slack puts it at the top
    # of the envelope; a Slack Connect (shared channel) event carries it on the
    # event instead. Missing entirely is survivable — the message still becomes
    # work, it just routes client-wide under "" — and dropping a real request
    # over it would be worse.
    team = payload.get("team_id") or event.get("team") or ""

    thread_ts = event.get("thread_ts") or ts

    return {
        "source": SOURCE,
        "client": team,
        "conversation_id": f"{SOURCE}:{team}:{channel}:{thread_ts}",
        "external_id": f"{SOURCE}:{channel}:{ts}",
        "author": author,
        "text": text,
        "timestamp": _timestamp(ts),
        "attachments": _attachments(event.get("files") or []),
    }


def _timestamp(ts: str) -> str:
    """Slack's epoch-with-microseconds -> ISO-8601.

    IntakeGateway.accept() parses this with datetime.fromisoformat(), so
    handing it Slack's own "1756600000.000100" would raise one layer past
    every test in this module.
    """
    try:
        seconds = float(ts)
        # INSIDE the try, deliberately: float() accepts "inf"/"nan" happily and
        # a large epoch parses fine, so the conversion is where those actually
        # blow up — with OverflowError/ValueError, one layer past a `float()`-only
        # guard. OSError joins them because the platform's time_t is what
        # ultimately rejects an out-of-range value.
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        raise TranslationError(f"unparsable Slack ts {ts!r}") from exc


def _attachments(files: list) -> list[dict]:
    """Files are carried, not understood (spec §9).

    AttachmentKind is text|table|image and has no binary member, so a non-image
    file is carried as `text` with its URL as content. The URL is not fetched
    here and the bytes are never read: vision extraction is §5.2 and Phase 7.
    """
    out: list[dict] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        mimetype = str(item.get("mimetype") or "")
        kind = AttachmentKind.IMAGE if mimetype.startswith("image/") else AttachmentKind.TEXT
        out.append(
            {
                "kind": kind.value,
                "name": str(item.get("name") or item.get("id") or "attachment"),
                "content": str(item.get("url_private") or ""),
            }
        )
    return out


def conversation_parts(conversation_id: str) -> tuple[str, str, str]:
    """(team, channel, thread_ts) from a conversation id this module built.

    The notifier needs a channel and a thread anchor to answer into, and §3.5
    fixes the conversation id format precisely so that no mapping table is
    needed. Splitting is safe: Slack team, channel and ts values contain no
    colons, so exactly four parts come back.
    """
    parts = conversation_id.split(":")
    if len(parts) != 4 or parts[0] != SOURCE:
        raise ValueError(f"not a Slack conversation id: {conversation_id!r}")
    return parts[1], parts[2], parts[3]
