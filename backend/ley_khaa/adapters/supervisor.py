"""Keeping the adapters alive without letting them near anything else (§3.4).

Adapters run as supervised asyncio tasks in the FastAPI lifespan beside the
Phase 5 dispatcher — decision #2, which is Phase 5 decision #1 applied again: no
second process type, `docker compose up` stays one command. The cost of that is
that a crashing adapter shares a process with the API, so supervision is a named
unit with its own tests rather than an afterthought: a crash is logged,
dead-lettered and restarted with exponential backoff, and never propagates.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from contextlib import suppress

from ..config import settings
from ..persistence.dead_letter_repository import DeadLetterRepository
from .base import ChannelAdapter, channel_set
from .discord.client import DiscordAdapter
from .slack.client import SlackAdapter

logger = logging.getLogger(__name__)


class AdapterSupervisor:
    def __init__(
        self,
        adapters: Sequence[ChannelAdapter],
        *,
        session_factory: Callable[[], object],
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.adapters = list(adapters)
        self.session_factory = session_factory
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        # Keyed by name because that is exactly what ChannelNotifier routes on
        # (dest.source), so the two cannot drift.
        self.registry: dict[str, ChannelAdapter] = {a.name: a for a in self.adapters}
        # Captured at start and handed to ChannelNotifier: a driver on a worker
        # thread has no running loop of its own to hand a coroutine to.
        self.loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        for adapter in self.adapters:
            self._tasks.append(asyncio.create_task(self._supervise(adapter)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for adapter in self.adapters:
            try:
                await adapter.stop()
            except Exception:
                # Shutdown must finish. An adapter that cannot close cleanly is
                # logged, not allowed to hold the whole process open.
                logger.exception("stopping adapter %s failed", adapter.name)
        self.loop = None

    def next_backoff(self, delay: float) -> float:
        """Double, capped. A separate method so the growth can be asserted on
        the numbers rather than by timing a sleep, which would pass or fail on
        how busy the machine is."""
        return min(delay * 2, self.max_backoff)

    async def _supervise(self, adapter: ChannelAdapter) -> None:
        delay = self.base_backoff
        while True:
            try:
                await adapter.start()
                # A clean return means the adapter stopped on purpose.
                logger.info("adapter %s finished", adapter.name)
                return
            except asyncio.CancelledError:
                # Shutdown. NOT a crash — catching this as a failure would make
                # every clean shutdown dead-letter and try to restart.
                raise
            except Exception as exc:
                logger.exception("adapter %s crashed; restarting in %.3fs", adapter.name, delay)
                self._dead_letter(adapter.name, exc)
                await asyncio.sleep(delay)
                delay = self.next_backoff(delay)

    def _dead_letter(self, name: str, exc: BaseException) -> None:
        """Its own session: this runs on the event loop, not inside any
        request's or worker's unit of work.

        No payload, deliberately: an exception raised by a platform client can
        carry the request that produced it, and that request carries the bot
        token. The reason string is `type: message`, free text that an SDK
        exception routinely fills with exactly that request — so
        DeadLetterRepository.record() runs it through the same value-level
        token scrub it applies to payload strings, rather than trusting it to
        be safe by construction.
        """
        session = self.session_factory()
        try:
            DeadLetterRepository(session).record(
                source=name,
                kind="connection",
                reason=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            # Recording a failure must never become one.
            logger.exception("could not dead-letter an adapter crash")
        finally:
            with suppress(Exception):
                session.close()


def build_adapters(
    *,
    ingest: Callable[[dict], None],
    dead_letter: Callable[..., None],
) -> list[ChannelAdapter]:
    """Exactly those adapters whose tokens are present (spec §3.4).

    No tokens -> no adapters -> a fresh clone behaves precisely as it does
    today, which is what keeps `docker compose up` a zero-account demo.

    Callables rather than a session factory, so this module never imports
    api/app.py — the lifespan is what calls this, which would be circular — and
    so an adapter never sees a repository (§5.1: adapters hold no business
    logic).

    Importing SlackAdapter and DiscordAdapter here does NOT import slack_sdk or
    discord: both clients import their library inside start(), so a token-free
    process never loads either one.
    """
    adapters: list[ChannelAdapter] = []

    if settings.slack_bot_token and settings.slack_app_token:
        channels = channel_set(settings.slack_channels)
        adapters.append(
            SlackAdapter(
                bot_token=settings.slack_bot_token,
                app_token=settings.slack_app_token,
                allowed_channels=channels,
                ingest=ingest,
                dead_letter=dead_letter,
            )
        )
        _announce("slack", channels)
    elif settings.slack_bot_token or settings.slack_app_token:
        # Both or nothing (spec §5). A Socket Mode connection needs the app
        # token and the Web API needs the bot token, so starting on one would
        # crash-loop through the supervisor's backoff forever while looking
        # configured.
        logger.warning(
            "slack is half-configured (need BOTH LEY_KHAA_SLACK_BOT_TOKEN and "
            "LEY_KHAA_SLACK_APP_TOKEN); the slack adapter will not start"
        )

    if settings.discord_bot_token:
        channels = channel_set(settings.discord_channels)
        adapters.append(
            DiscordAdapter(
                bot_token=settings.discord_bot_token,
                allowed_channels=channels,
                ingest=ingest,
                dead_letter=dead_letter,
            )
        )
        _announce("discord", channels)

    if not adapters:
        logger.info("no channel tokens configured; running with no adapters")
    return adapters


def _announce(name: str, channels: frozenset[str]) -> None:
    """Spec §4: startup logs exactly which channels are live, so what the bot
    is listening to is always visible rather than inferred from config."""
    if channels:
        logger.info("%s adapter allowlist: %s", name, ", ".join(sorted(channels)))
    else:
        logger.warning(
            "%s adapter has a token but an EMPTY channel allowlist — it will start and "
            "ingest nothing. Set LEY_KHAA_%s_CHANNELS to the channel ids it should read.",
            name,
            name.upper(),
        )
