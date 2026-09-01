"""The Slack Socket Mode connection. Deliberately as small as possible.

Socket Mode dials OUT, so there is no public URL, no tunnel and no inbound port
(decision #2). This module holds NO decisions — the allowlist, the self-message
filter, thread derivation and the dedupe key all live in translate.py, which CI
can exercise. What is left here is a socket, a web client, and the wiring
between them, which is the only part verified by hand against a real workspace.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from ..base import AdapterError, Destination, TranslationError
from .translate import SOURCE, conversation_parts, translate

logger = logging.getLogger(__name__)


class SlackAdapter:
    name = SOURCE

    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str,
        allowed_channels: frozenset[str],
        ingest: Callable[[dict], None],
        dead_letter: Callable[..., None],
    ) -> None:
        # Held private and never rendered: __repr__ below is the default
        # object repr precisely because a token must never reach a log line,
        # and an adapter ends up in one the moment anything goes wrong.
        self._bot_token = bot_token
        self._app_token = app_token
        self.allowed_channels = allowed_channels
        # Injected, so this module never sees a Session, an Orchestrator or a
        # repository — §5.1: adapters hold no business logic.
        self.ingest = ingest
        self.dead_letter = dead_letter
        self.web = None
        self.socket = None
        # Learned from auth.test at connect time. Until then the filter falls
        # back to the bot_id check in translate(), which catches our own posts
        # anyway.
        self.bot_user_id: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"<SlackAdapter channels={len(self.allowed_channels)}>"

    async def start(self) -> None:
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
        from slack_sdk.web.async_client import AsyncWebClient

        self.web = AsyncWebClient(token=self._bot_token)
        try:
            identity = await self.web.auth_test()
            self.bot_user_id = identity.get("user_id")
        except Exception as exc:
            raise AdapterError(f"Slack auth.test failed: {type(exc).__name__}") from exc

        self.socket = SocketModeClient(app_token=self._app_token, web_client=self.web)

        async def on_request(client: SocketModeClient, request: SocketModeRequest) -> None:
            # Acknowledge FIRST, always. Slack redelivers anything unacked
            # within 3s, and the pipeline behind _handle can take much longer
            # than that. Redelivery is survivable — MessageRepository.add
            # dedupes on external_id — but acking late turns every slow task
            # into duplicate traffic for no benefit.
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            if request.type == "events_api":
                await self._handle(request.payload)

        self.socket.socket_mode_request_listeners.append(on_request)
        await self.socket.connect()
        logger.info(
            "slack adapter listening on %d channel(s): %s",
            len(self.allowed_channels),
            ", ".join(sorted(self.allowed_channels)) or "(none — ingesting nothing)",
        )
        # Block for the life of the process; the supervisor owns cancellation.
        await asyncio.Event().wait()

    async def stop(self) -> None:
        if self.socket is not None:
            await self.socket.disconnect()
            self.socket = None

    async def _handle(self, payload: dict) -> None:
        """One inbound event. Everything it can do is testable with a dict.

        Nothing raises out of here. This coroutine runs on the loop that also
        runs every other adapter and the dispatcher; an escaping exception is
        swallowed by asyncio at best and takes the socket down at worst.
        """
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
            # A deliberate, normal drop: an unlisted channel, our own message,
            # an edit. Silent by design — see translate()'s docstring.
            return

        try:
            # The intake pipeline is synchronous SQLAlchemy and can spend an
            # LLM call inside. Running it on the loop would block every other
            # adapter and the dispatcher for that whole time.
            await asyncio.to_thread(self.ingest, raw)
        except Exception as exc:
            logger.exception("ingesting a Slack message failed")
            self.dead_letter(
                source=self.name,
                kind="inbound",
                reason=f"{type(exc).__name__}: {exc}",
                payload=payload,
            )

    async def notify(self, dest: Destination, text: str) -> None:
        if self.web is None:
            raise AdapterError("slack adapter is not connected")
        _team, channel, thread_ts = conversation_parts(dest.conversation_id)
        await self.web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
