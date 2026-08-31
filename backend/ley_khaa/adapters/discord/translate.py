"""Discord message dict -> the raw dict IntakeGateway.accept() already takes.

Pure: no network, no tokens, no `discord` import. client.py flattens a live
discord.Message into the dict this takes, and that flattening is the ONLY thing
client.py decides — see its docstring for the exact shape.
"""
from __future__ import annotations

from datetime import datetime

from ...domain.models import AttachmentKind
from ..base import TranslationError

SOURCE = "discord"

# DEFAULT and REPLY are the two types a person actually typed. Everything else
# (joins, pins, boosts, thread-created notices) is Discord narrating itself.
_INGESTED_TYPES = frozenset({0, 19})


def translate(
    payload: dict,
    *,
    allowed_channels: frozenset[str],
    bot_user_id: str | None,
) -> dict | None:
    """Return the raw intake dict, or None if this message is deliberately ignored.

    Same two-shape contract as the Slack translator: None is a normal, silent
    drop; TranslationError is a message that looked like work and could not be
    handled, and the caller dead-letters it.
    """
    # Distinguish "no channel identifier at all" (a malformed event — worth
    # seeing in the dead-letter panel, and safe to record because it names no
    # channel) from "a channel that is simply not allowlisted" (a normal,
    # silent drop). The Slack translator draws the same line; a later task
    # calls both through one code path, so the return contracts must match.
    if "channel_id" not in payload and "parent_id" not in payload:
        raise TranslationError("a Discord message with no channel id cannot be routed")

    channel_id = payload.get("channel_id")
    parent_id = payload.get("parent_id")

    # A message inside a thread has channel_id == the THREAD's id, so the
    # allowlisted channel is the parent when there is one. Checking channel_id
    # alone rejects every threaded reply in an allowlisted channel — which is
    # the clarification loop's own path, and Discord mints a fresh thread id
    # nobody can put in an allowlist in advance.
    channel = parent_id or channel_id
    if not isinstance(channel, str) or channel not in allowed_channels:
        return None

    # No guild means a DM. Spec §9: threads only, no DMs in this phase.
    guild = payload.get("guild_id")
    if not guild:
        return None

    if payload.get("type") not in _INGESTED_TYPES:
        return None

    author = payload.get("author") or {}
    # Two guards on one property, deliberately: `bot` covers every bot in the
    # channel, and the id check covers OUR bot specifically (a self-hosted app
    # posting under an unusual identity). Each is pinned by its own test so
    # neither can be deleted silently.
    if author.get("bot"):
        return None
    author_id = author.get("id")
    if not author_id or (bot_user_id is not None and str(author_id) == bot_user_id):
        return None

    text = (payload.get("content") or "").strip()
    if not text:
        return None

    message_id = payload.get("id")
    if not message_id:
        raise TranslationError("a Discord message with no id cannot be deduplicated")
    message_id = str(message_id)

    # In a thread, the thread's own id IS the anchor; at top level the message
    # anchors the thread a reply would create. Spec §3.5's "thread_id or
    # message_id".
    thread = channel_id if parent_id else message_id

    return {
        "source": SOURCE,
        "client": str(guild),
        "conversation_id": f"{SOURCE}:{guild}:{channel}:{thread}",
        "external_id": f"{SOURCE}:{channel}:{message_id}",
        "author": str(author_id),
        "text": text,
        "timestamp": _timestamp(payload.get("timestamp")),
        "attachments": _attachments(payload.get("attachments") or []),
    }


def _timestamp(raw: object) -> str:
    """Discord already sends ISO-8601. Validated rather than trusted: an
    unparsable value would otherwise raise inside IntakeGateway.accept(), one
    layer past every test in this module."""
    if not isinstance(raw, str):
        raise TranslationError(f"missing Discord timestamp: {raw!r}")
    try:
        datetime.fromisoformat(raw)
    except ValueError as exc:
        raise TranslationError(f"unparsable Discord timestamp {raw!r}") from exc
    return raw


def _attachments(attachments: list) -> list[dict]:
    """Carried, not understood (spec §9). AttachmentKind has no binary member,
    so a non-image file is `text` holding its URL."""
    out: list[dict] = []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        content_type = str(item.get("content_type") or "")
        kind = AttachmentKind.IMAGE if content_type.startswith("image/") else AttachmentKind.TEXT
        out.append(
            {
                "kind": kind.value,
                "name": str(item.get("filename") or item.get("id") or "attachment"),
                "content": str(item.get("url") or ""),
            }
        )
    return out


def conversation_parts(conversation_id: str) -> tuple[str, str, str]:
    """(guild, channel, thread) from a conversation id this module built.

    Snowflakes contain no colons, so exactly four parts come back. The notifier
    posts into `thread` — which for a top-level message is the message id, and
    is the id `client.py` starts a thread from.
    """
    parts = conversation_id.split(":")
    if len(parts) != 4 or parts[0] != SOURCE:
        raise ValueError(f"not a Discord conversation id: {conversation_id!r}")
    return parts[1], parts[2], parts[3]
