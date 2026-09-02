"""Telling a human, in the channel they asked in (spec §3.6).

The seam is the same shape as LLMClient and SandboxRunner: a protocol with a
`name`, a real implementation, and an offline one that keeps CI and a
token-free `docker compose up` green.

notify() is SYNCHRONOUS on purpose. TaskDriver.advance() is synchronous and, in
workers mode, already runs inside asyncio.to_thread on a dispatcher worker, so a
notifier is called from a worker thread. Making this seam async would force
advance() async and rewrite the driver — the opposite of §3.1's "the pipeline
downstream of the gateway is untouched". ChannelNotifier (below, Task 8) is
where the sync call is handed to the event loop.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from typing import Protocol

from sqlalchemy.orm import Session

from ..config import settings
from ..domain.states import TaskState
from ..persistence.dead_letter_repository import DeadLetterRepository
from ..persistence.orm import TaskRow
from .base import ChannelAdapter, Destination

logger = logging.getLogger(__name__)

# Exactly the states in §3.6's table: a human is needed, or the work is
# finished. Nothing else — a bot narrating every transition is noise, and noise
# gets muted. Adding a member here without a branch in message_for() would
# notify nothing; test_every_notify_state_produces_a_message pins the pair.
NOTIFY_STATES: frozenset[TaskState] = frozenset(
    {
        TaskState.NEEDS_CLARIFICATION,
        TaskState.AWAITING_APPROVAL,
        TaskState.DONE,
        TaskState.FAILED,
    }
)


class Notifier(Protocol):
    name: str

    def notify(self, dest: Destination, text: str) -> None: ...


class NullNotifier:
    """The default. No tokens, no adapters, nothing to say to anyone.

    Every existing test and every token-free run keeps exactly the behaviour it
    had before this phase because this is what the driver gets unless a
    lifespan installs something else.
    """

    name = "null"

    def notify(self, dest: Destination, text: str) -> None:
        return None


class RecordingNotifier:
    """Keeps what it was asked to send. The offline stand-in for tests that
    assert a notification happened, in the same spirit as HeuristicLLM."""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[tuple[Destination, str]] = []

    def notify(self, dest: Destination, text: str) -> None:
        self.sent.append((dest, text))


def message_for(row: TaskRow) -> str | None:
    """What to say about this task, or None to stay quiet.

    Pure: it reads the row and returns text. Deciding WHETHER to send (the
    last_notified_state guard) and HOW (the adapter) are the driver's and the
    ChannelNotifier's jobs respectively, so this can be table-tested on its own.
    """
    state = TaskState(row.state)
    if state not in NOTIFY_STATES:
        return None

    title = row.title or "your request"

    if state is TaskState.NEEDS_CLARIFICATION:
        question = (row.open_question or "").strip()
        if not question:
            # A task can arrive here with the question cleared (the validate
            # path does exactly that). Posting an empty message into a channel
            # is worse than asking generically.
            question = "I need a bit more before I can start. What should I do?"
        return f"“{title}” — {question}"

    if state is TaskState.AWAITING_APPROVAL:
        # effective_mode, not recommended_mode: a human who pinned a mode must
        # be told what is actually in force, which is the same field the
        # dashboard shows and the driver acts on.
        mode = row.effective_mode or "suggest"
        reason = (row.autonomy_reason or "").strip()
        tail = f" ({reason})" if reason else ""
        return f"“{title}” is ready and waiting for you — recommended mode: {mode}{tail}."

    if state is TaskState.DONE:
        # The bundle is named by PATH. ley-khaa is local-first and
        # single-operator, and §5.11 surfaces bundles by path everywhere else
        # (see api/app.py's BundleOut.root, kept deliberately). Revisit this
        # line if the project ever becomes hosted or multi-tenant.
        where = (row.workspace_path or "").strip()
        tail = f" The bundle is at {where}." if where else ""
        return f"“{title}” is done.{tail}"

    if state is TaskState.FAILED:
        reason = (row.failure_reason or "").strip() or "no reason was recorded"
        return f"“{title}” failed: {reason}"

    # Unreachable while NOTIFY_STATES matches the branches above exactly. Kept
    # explicit (not an unconditional trailing `else`) so a state added to
    # NOTIFY_STATES without a branch here genuinely renders nothing instead of
    # silently falling into the FAILED message — which is what this module's
    # own docstring promises and what
    # test_every_notify_state_produces_a_message depends on to catch drift.
    return None  # pragma: no cover


# --- the process-wide notifier -------------------------------------------
# build_orchestrator(session) is module-level and the dispatcher reaches it
# through _drive_task(session, task_id), which has no `app` handle and never
# will — the dispatcher is deliberately ignorant of FastAPI. So the notifier
# cannot ride on app.state. One holder, set by the lifespan and reset by it on
# shutdown, defaulted to NullNotifier so nothing that forgets to set it can
# misbehave.
_notifier: Notifier = NullNotifier()


def set_notifier(notifier: Notifier) -> None:
    global _notifier
    _notifier = notifier
    logger.info("notifications go through %s", notifier.name)


def current_notifier() -> Notifier:
    return _notifier


class ChannelNotifier:
    """Hands a notification to the adapter that owns its conversation.

    This is the one place the design meets Phase 5's threading model head-on.
    TaskDriver.advance() is SYNCHRONOUS and, in workers mode, runs inside
    asyncio.to_thread on a dispatcher worker; the Slack and Discord clients are
    ASYNC and live on the main event loop. So notify() is called from a worker
    thread and hands its coroutine across with
    asyncio.run_coroutine_threadsafe(coro, loop), where `loop` was captured at
    lifespan start and is held by the supervisor.

    Fire-and-forget: the driver does not wait on the future, so a slow or wedged
    platform API cannot extend a task's execution time. The future's exception
    is consumed by a done-callback that writes the dead letter — without one,
    asyncio swallows it and a failed notification leaves no trace at all, which
    is the failure §3.8 exists to prevent.
    """

    name = "channel"

    def __init__(
        self,
        adapters: dict[str, ChannelAdapter],
        *,
        loop: asyncio.AbstractEventLoop | None,
        session_factory: Callable[[], Session],
    ) -> None:
        self.adapters = adapters
        self.loop = loop
        self.session_factory = session_factory

    def notify(self, dest: Destination, text: str) -> None:
        adapter = self.adapters.get(dest.source)
        if adapter is None:
            # NOT a dead letter. A task's source can be "simulator" (a fresh
            # clone's demo task) or "dashboard" (every /tasks/{id}/answer
            # message); there is no channel to answer into and nothing failed.
            # Recording those would bury the real drops under a clone's own
            # traffic and make the panel useless.
            logger.debug("no adapter for %s; nothing to notify", dest.source)
            return

        if settings.dispatch_mode == "inline":
            # Spec §3.6: inline is the single-operator dashboard mode. A task
            # driven on a request thread has no channel to answer into, so the
            # notification is recorded rather than delivered.
            self._dead_letter(dest, "inline dispatch mode does not deliver notifications", text)
            return

        if self.loop is None or not self.loop.is_running():
            self._dead_letter(dest, "no running event loop to deliver on", text)
            return

        try:
            future = asyncio.run_coroutine_threadsafe(adapter.notify(dest, text), self.loop)
        except Exception as exc:  # the loop closed between the check and here
            self._dead_letter(dest, f"could not schedule delivery: {exc}", text)
            return
        future.add_done_callback(lambda done: self._record_result(dest, text, done))

    def _record_result(self, dest: Destination, text: str, future: Future) -> None:
        try:
            future.result()
        except Exception as exc:
            self._dead_letter(dest, f"delivery failed: {exc}", text)

    def _dead_letter(self, dest: Destination, reason: str, text: str) -> None:
        """Its own session, always.

        notify() runs on a dispatcher worker thread that already holds a session
        for the task it is driving. Writing through that one would interleave an
        unrelated commit into the middle of the driver's transaction; this is
        the same per-unit-of-work session discipline Dispatcher._drive follows.
        """
        # INSIDE the try: session_factory() itself can raise (pool exhausted,
        # database down), and that is exactly when a dead letter is most likely
        # to be attempted. Outside, the failure escapes the very handler whose
        # job is to make sure recording a failure never becomes one.
        session = None
        try:
            session = self.session_factory()
            DeadLetterRepository(session).record(
                source=dest.source,
                kind="outbound",
                reason=reason,
                payload={
                    "conversation_id": dest.conversation_id,
                    "external_id": dest.external_id,
                    "text": text,
                },
            )
        except Exception:
            # Last resort. A dead letter records a failure and must not become
            # one — least of all inside a done-callback, where the exception has
            # nowhere to go.
            logger.exception("could not dead-letter a failed notification")
        finally:
            if session is not None:
                with suppress(Exception):
                    session.close()
