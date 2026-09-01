"""The whole interface between ley-khaa and a chat platform (spec §5.1, §3.3).

An adapter ingests and notifies. It holds no business logic: deciding what a
message MEANS is the orchestrator's job, which is why the clarification-reply
rule lives there (§3.7) and not here.

Three implementations from the start — Slack, Discord and the Simulator — so
this protocol is shaped by more than one caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class AdapterError(Exception):
    """An adapter's connection failed. The supervisor dead-letters and restarts
    it; it never reaches the API or the dispatcher."""


class TranslationError(Exception):
    """An event that looked like work could not be turned into a message.

    Distinct from a deliberate drop, which is a None return: an unlisted
    channel, a bot's own message and a non-message event are all NORMAL and
    must not fill the dead-letter panel. This is for the abnormal case, and it
    is what gets recorded.
    """


@dataclass(frozen=True)
class Destination:
    """Where to answer. Frozen because it crosses a thread boundary.

    `external_id` is the namespaced id of the message to thread under; the
    channel and the thread anchor themselves are recovered from
    `conversation_id`, whose format is fixed by spec §3.5 and deterministic by
    construction — so no mapping table exists or is needed.
    """

    source: str
    conversation_id: str
    external_id: str | None = None


@runtime_checkable
class ChannelAdapter(Protocol):
    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def notify(self, dest: Destination, text: str) -> None: ...


def channel_set(raw: str) -> frozenset[str]:
    """Parse `LEY_KHAA_*_CHANNELS` into an allowlist.

    Empty means EMPTY, never "everything" (decision #4). An adapter with a
    token and an empty allowlist starts and ingests nothing, which is the safe
    reading of an incomplete configuration — spec §5 says so explicitly.
    """
    return frozenset(part.strip() for part in raw.split(",") if part.strip())
