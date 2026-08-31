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

import logging
from typing import Protocol

from ..domain.states import TaskState
from ..persistence.orm import TaskRow
from .base import Destination

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
