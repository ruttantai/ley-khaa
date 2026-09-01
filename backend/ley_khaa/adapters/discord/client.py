"""The Discord Gateway connection. Deliberately as small as possible.

The Gateway dials OUT, like Slack's Socket Mode, so decision #2 holds for both
platforms: no public URL, no tunnel, no inbound port.

discord.py hands the callback a discord.Message OBJECT while translate() takes a
dict, so `flatten` sits between them. That function is the only decision this
module makes — it is where message.channel.parent_id is reachable, which the
allowlist and the conversation id both depend on — and it is pure, so it has
real tests.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..base import AdapterError, Destination, TranslationError
from .translate import SOURCE, conversation_parts, translate

logger = logging.getLogger(__name__)


def flatten(message) -> dict:
    """discord.Message -> the dict translate() takes.

    Ids are rendered as strings because Discord snowflakes are 64-bit integers
    in the library and strings everywhere else in this codebase — conversation
    ids, external ids and MessageRow.client are all text, and mixing the two
    would produce a conversation id that never matches the one the notifier
    parses back.

    A DM has `guild is None`; that is rendered rather than raised on, because
    deciding to drop a DM is translate()'s job (§9), not this function's.
    """
    channel = message.channel
    guild = getattr(message, "guild", None)
    return {
        "id": str(message.id),
        "channel_id": str(channel.id),
        # None for a top-level channel; the parent channel for a thread. The one
        # fact only the live object knows.
        "parent_id": str(channel.parent_id) if getattr(channel, "parent_id", None) else None,
        "guild_id": str(guild.id) if guild is not None else None,
        "content": message.content or "",
        "timestamp": message.created_at.isoformat(),
        "type": getattr(message.type, "value", message.type),
        "author": {"id": str(message.author.id), "bot": bool(message.author.bot)},
        "attachments": [
            {
                "id": str(a.id),
                "filename": a.filename,
                "url": a.url,
                "content_type": getattr(a, "content_type", None),
            }
            for a in getattr(message, "attachments", [])
        ],
    }


class DiscordAdapter:
    name = SOURCE

    def __init__(
        self,
        *,
        bot_token: str,
        allowed_channels: frozenset[str],
        ingest: Callable[[dict], None],
        dead_letter: Callable[..., None],
    ) -> None:
        self._bot_token = bot_token
        self.allowed_channels = allowed_channels
        self.ingest = ingest
        self.dead_letter = dead_letter
        self.client = None
        self.bot_user_id: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"<DiscordAdapter channels={len(self.allowed_channels)}>"

    async def start(self) -> None:
        import discord

        # message_content is a PRIVILEGED intent and must be enabled in the
        # Discord developer portal as well as here. Without it every message
        # arrives with content == "" and translate() drops all of them as
        # empty — the bot looks connected and silently ingests nothing, which
        # is exactly the failure mode §4's "startup logs what is live" exists
        # to make visible. GETTING_STARTED says so too.
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)

        @self.client.event
        async def on_ready() -> None:
            self.bot_user_id = str(self.client.user.id)
            logger.info(
                "discord adapter listening on %d channel(s): %s",
                len(self.allowed_channels),
                ", ".join(sorted(self.allowed_channels)) or "(none — ingesting nothing)",
            )

        @self.client.event
        async def on_message(message) -> None:
            await self._handle(flatten(message))

        try:
            await self.client.start(self._bot_token)
        except Exception as exc:
            raise AdapterError(f"Discord gateway failed: {type(exc).__name__}") from exc

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def _handle(self, payload: dict) -> None:
        """One inbound message. Nothing raises out of here — see the Slack
        adapter's `_handle` for the full reasoning; the loop that runs this also
        runs every other adapter and the dispatcher."""
        try:
            raw = translate(
                payload,
                allowed_channels=self.allowed_channels,
                bot_user_id=self.bot_user_id,
            )
        except TranslationError as exc:
            self.dead_letter(source=self.name, kind="inbound", reason=str(exc), payload=payload)
            return
        if raw is None:
            return

        try:
            await asyncio.to_thread(self.ingest, raw)
        except Exception as exc:
            logger.exception("ingesting a Discord message failed")
            self.dead_letter(
                source=self.name,
                kind="inbound",
                reason=f"{type(exc).__name__}: {exc}",
                payload=payload,
            )

    async def notify(self, dest: Destination, text: str) -> None:
        if self.client is None:
            raise AdapterError("discord adapter is not connected")
        _guild, _channel, thread = conversation_parts(dest.conversation_id)
        # The conversation's anchor IS the channel to post into: inside a thread
        # it is the thread's id, and at top level it is the message id, which
        # Discord resolves to the thread started from it. get_channel takes an
        # int — the flatten docstring explains why everything else is a string.
        target = self.client.get_channel(int(thread))
        if target is None:
            raise AdapterError(f"discord channel {thread} is not visible to this bot")
        await target.send(text)
