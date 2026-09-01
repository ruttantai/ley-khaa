# Phase 6 (v0.7.0) — Real Slack and Discord Channel Adapters — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make ley-khaa reachable from real Slack and Discord channels in both directions — a
message in an allowlisted channel becomes a task in the right project, the bot asks its clarifying
question back in that thread, a reply in the thread answers it, and the bot never ingests its own
messages.

**Architecture:** Each adapter is split in two. `translate.py` is a **pure function** (platform
event dict → the raw dict `IntakeGateway.accept()` already takes) and holds every decision that can
be wrong: the allowlist, the self-message filter, thread derivation, the dedupe key, attachment
mapping. `client.py` is a thin outbound-WebSocket wrapper (Slack Socket Mode / Discord Gateway) that
holds no decisions, because it is the only half CI can never exercise. An `AdapterSupervisor` starts
— in the FastAPI lifespan, beside the Phase 5 dispatcher — exactly those adapters whose tokens are
present, so a token-free clone behaves precisely as it does today. Outbound notification is a
`Notifier` seam injected into `TaskDriver`, the same shape as `LLMClient` and `SandboxRunner`, with
`NullNotifier` as the default. Nothing downstream of `IntakeGateway.accept()` changes.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, Pydantic v2, `slack_sdk` (Socket
Mode), `discord.py` (Gateway), React/Vite/Tailwind, vitest.

**Spec:** `docs/superpowers/specs/2026-08-30-phase-6-channel-adapters-design.md` — read it before
Task 1 and keep it open. Every decision in §2 is settled and not open for re-litigation.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **Baseline to keep green:** 639 backend tests / 49 frontend tests, **0 skipped, 0 warnings**,
  `npx tsc --noEmit` clean. Every task ends with the full suite green, not just its own file.
- **CI needs no tokens and no network.** No test may import `slack_sdk` or `discord` outside
  `client.py`, and no test may open a socket.
- **`Settings` is a frozen dataclass** (`config.py`, a Phase 0 invariant, commit `399033e`). Tests
  must NEVER unfreeze it. Pin settings with
  `dataclasses.replace(real_settings, field=value)` and then
  `monkeypatch.setattr(<each consuming module>, "settings", patched)` — every module that did
  `from ..config import settings` bound the object at import time, so rebinding
  `ley_khaa.config.settings` alone pins nothing. See `tests/test_dispatch_modes.py:11-27` for the
  established idiom.
- **New enum members / new set members:** when you add a value to one collection, grep for every
  other collection that enumerates the same thing. THE durable lesson of Phase 5 was
  `CandidateState.AWAITING_TRIAGE` added to `_ALLOWED` but not to `TERMINAL_STATES` four lines
  above it — a merge blocker a fully green 639-test suite said nothing about.
- **Every new assertion is mutation-tested:** delete the behaviour the assertion guards, run the
  test, watch it fail **for the right reason**, restore. Report the observed failure message in your
  task report. Phase 4 produced EIGHT findings of the "test that passes for the wrong reason" class;
  self-reported "I mutation-tested it" was wrong more than once. When a fix adds TWO guards, each
  needs its OWN test, or either can be deleted silently later.
- **Claim before you record.** The ordering rule commit `c043c46` established and every phase since
  has followed: win the state transition first, write the consequence second. In this phase it also
  covers sending: `mark_notified` is a compare-and-swap that must succeed BEFORE a notification is
  handed to an adapter.
- **Never log, store, or return a token.** Dead-letter payloads are redacted before storage. This
  is the first phase that handles credentials.
- **No secret, no real workspace id, no real channel id in a committed fixture.** Fixtures are
  synthetic payloads shaped exactly like the real ones.
- **`conftest.py` pins `LEY_KHAA_DISPATCH=inline`**, `LEY_KHAA_LLM=heuristic`,
  `LEY_KHAA_DEBOUNCE_SECONDS=0`, `LEY_KHAA_SANDBOX=subprocess`. Do not change these.
- **`conftest`'s `session` fixture shares ONE connection** (StaticPool, in-memory). Any test about
  concurrency, a real second connection, or naive/aware datetimes must use the file-backed
  `session_factory` fixture instead — three separate Phase 5 defects passed vacuously on the shared
  session.
- **Purge `__pycache__` between mutations.** A same-byte-length edit after `git checkout` leaves a
  valid-looking stale `.pyc` and you are measuring nothing.
- **Local Docker gotcha (this machine runs Colima):** before running the 9 `[docker]` params,
  `mkdir -p "$HOME/tmp"` and `export TMPDIR="$HOME/tmp"`. If that directory does not exist pytest
  silently falls back to `/private/tmp`, which Colima does not mount, and the tests fail with a
  misleading "can't open file .../generator/attempt_1.py".
- **Commit style:** conventional commits, one commit per task step group, ending with
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## What the pre-scan found (read before Task 1)

The practice that has caught plan defects in every phase since Phase 3: the real code was read
before this plan's reference code was written. Six things it turned up, all of which this plan is
built around.

1. **`IntakeGateway.accept()` parses `timestamp` with `datetime.fromisoformat(raw_ts)`**
   (`intake/gateway.py:28`). A translator must therefore emit an **ISO-8601 string**, not a
   `datetime` and not a Slack epoch float. Slack gives `"1699999999.000100"`; convert it.
2. **`MessageRow.external_id` is globally unique** (`persistence/orm.py:71`), but a Slack `ts` is
   only unique *within a channel*. A bare `ts` as the dedupe key is a cross-channel collision
   waiting to happen, and a collision here silently drops a real message as a duplicate. Every
   `external_id` this phase writes is namespaced: `slack:{channel}:{ts}`,
   `discord:{channel_id}:{message_id}`.
3. **A Discord message inside a thread has `channel_id` == the thread's id**, not the parent
   channel's. Checking the allowlist against `channel_id` alone would reject every threaded reply
   in an allowlisted channel — which is exactly the path the clarification loop runs on. The
   allowlist is checked against the **parent** channel (`parent_id or channel_id`). Slack has no
   equivalent problem: a threaded reply's `channel` is already the parent.
4. **`AttachmentKind` is only `text | table | image`** (`domain/models.py:8`). There is no binary
   kind, so a non-image file maps to `text` with its URL as `content`. That is consistent with §9
   ("attachments are carried, not understood") but must be stated, not silently done.
5. **A task's `source` can be `simulator` or `dashboard`.** A fresh clone's demo task and every
   `/tasks/{id}/answer` message are such. If `ChannelNotifier` dead-lettered "no adapter for this
   source", every fresh clone would fill its dead-letter panel with its own demo task and the panel
   that exists to show real drops would be pure noise. **Ordering rule: no adapter for the source →
   skip silently (not applicable). Adapter exists but cannot be delivered on → dead-letter.**
6. **§3.7 changes existing intake behaviour on the HTTP path too**, and the spec says so. The
   existing reply tests (`tests/test_task_replies.py`) all pass `reply_to_task_id` explicitly and
   are unaffected; `test_simulator.py` ingests with `promote=False` so no task exists in
   `needs_clarification` during its ingests. But run the FULL suite after Task 12. **If a test
   breaks, re-read its intent: a test that meant to send a second independent request gets its own
   `conversation_id`. Never weaken the new rule to keep an old test green.**

---

## File Structure

**New — backend:**

| File | Responsibility |
|---|---|
| `backend/ley_khaa/adapters/__init__.py` | empty package marker |
| `backend/ley_khaa/adapters/base.py` | `ChannelAdapter` protocol, `Destination`, `AdapterError`, `TranslationError`, `channel_set()` |
| `backend/ley_khaa/adapters/notifier.py` | `Notifier` protocol, `NullNotifier`, `ChannelNotifier`, `message_for()`, the process-wide notifier holder |
| `backend/ley_khaa/adapters/supervisor.py` | `AdapterSupervisor`, `build_adapters()` |
| `backend/ley_khaa/adapters/slack/translate.py` | pure: Slack event envelope → raw intake dict |
| `backend/ley_khaa/adapters/slack/client.py` | Socket Mode wrapper |
| `backend/ley_khaa/adapters/discord/translate.py` | pure: Discord message dict → raw intake dict |
| `backend/ley_khaa/adapters/discord/client.py` | Gateway wrapper |
| `backend/ley_khaa/persistence/dead_letter_repository.py` | `DeadLetterRepository`, `redact()` |
| `backend/ley_khaa/alembic/versions/0007_channels.py` | `dead_letters` table + `tasks.last_notified_state` |

**Modified — backend:** `persistence/orm.py` (`DeadLetterRow`, `TaskRow.last_notified_state`),
`persistence/repository.py` (`mark_notified`), `persistence/message_repository.py`
(`set_reply_target`), `orchestrator/driver.py` (notifier + `_announce`),
`orchestrator/orchestrator.py` (notifier passthrough, §3.7 clarification routing),
`intake/simulator.py` (retrofit to `ChannelAdapter`), `intake/gateway.py` (a docstring this
phase makes false), `config.py` (five env vars),
`api/app.py` (lifespan, `GET /dead-letters`), `api/schemas.py` (`DeadLetterOut`),
`pyproject.toml` (two pinned dependencies).

**New — frontend:** `src/DeadLetters.tsx`, `src/DeadLetters.test.tsx`.
**Modified — frontend:** `src/api.ts`, `src/App.tsx`, `src/App.test.tsx`.

**New — tests:** `test_orm_phase6.py`, `test_dead_letters.py`, `test_adapter_base.py`,
`test_slack_translate.py`, `test_discord_translate.py`, `test_notification_policy.py`,
`test_notifier_wiring.py`, `test_channel_notifier.py`, `test_supervisor.py`,
`test_slack_client.py`, `test_discord_client.py`, `test_clarification_routing.py`,
`test_adapter_startup.py`, `test_channel_loop.py`, `test_dead_letters_api.py`, plus payload
fixtures under `tests/fixtures/payloads/`.

**Modified — docs:** `README.md`, `CHANGELOG.md`, `docs/GETTING_STARTED.md`,
`docs/superpowers/specs/2026-08-28-phase-5-backlog.md`, `docker-compose.yml`.

---

## Task 1: Schema — the `dead_letters` table and `tasks.last_notified_state`

**Files:**
- Modify: `backend/ley_khaa/persistence/orm.py` (add `DeadLetterRow`; add one column to `TaskRow`)
- Create: `backend/ley_khaa/alembic/versions/0007_channels.py`
- Test: `backend/tests/test_orm_phase6.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DeadLetterRow` with columns `id: str`, `source: str`, `kind: str`, `reason: str`,
  `payload: str`, `created_at: datetime`; `TaskRow.last_notified_state: str | None`;
  alembic head `0007_channels`.

**Why `payload` is a `String` and not `JSON`:** it is written once and only ever displayed. A JSON
column would invite an equality comparison, and Phase 5 shipped a Postgres-only
`operator does not exist: json = json` bug that a SQLite-only suite could not see. Text has no such
edge, and the redactor already produces a string.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_orm_phase6.py`:

```python
from datetime import datetime, timezone

from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.orm import DeadLetterRow, TaskRow
from ley_khaa.persistence.repository import TaskRepository


def test_a_dead_letter_row_round_trips(session):
    row = DeadLetterRow(
        id="dl-1",
        source="slack",
        kind="inbound",
        reason="no text in the event",
        payload='{"event": {"type": "message"}}',
    )
    session.add(row)
    session.commit()

    stored = session.get(DeadLetterRow, "dl-1")
    assert stored.source == "slack"
    assert stored.kind == "inbound"
    assert stored.reason == "no text in the event"
    assert stored.payload == '{"event": {"type": "message"}}'
    assert stored.created_at.tzinfo is not None or isinstance(stored.created_at, datetime)


def test_a_new_task_has_never_been_notified(session):
    """The column must default to NULL, not to a state — a fresh task has
    announced nothing, and a non-null default would suppress its first
    notification."""
    row = TaskRepository(session).create(project="default", title="t", source_message_ids=[])
    assert row.last_notified_state is None


def test_last_notified_state_persists(session):
    repo = TaskRepository(session)
    row = repo.create(project="default", title="t", source_message_ids=[])
    stored = session.get(TaskRow, row.id)
    stored.last_notified_state = TaskState.DONE.value
    session.commit()
    session.refresh(stored)
    assert stored.last_notified_state == "done"
    assert datetime.now(timezone.utc) is not None  # sanity: the import is used
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd backend && python -m pytest tests/test_orm_phase6.py -v
```

Expected: FAIL — `ImportError: cannot import name 'DeadLetterRow'`.

- [ ] **Step 3: Add the ORM model and the column**

In `backend/ley_khaa/persistence/orm.py`, add to `TaskRow`, immediately after the
`lease_attempts` column and before the `effective_mode` property:

```python
    # --- outbound notification (spec §3.6) ----------------------------------
    # The state this task last ANNOUNCED to its channel. advance() is
    # re-entrant, so without this a task re-driven in the same state would
    # repeat its question every pass. NULL means nothing has been announced
    # yet, which is why it must not default to a state.
    last_notified_state: Mapped[str | None] = mapped_column(String, nullable=True)
```

And append a new model at the end of the file:

```python
class DeadLetterRow(Base):
    """An inbound message, an outbound notification, or a connection that was
    lost (spec §3.8, §7).

    A dropped message that leaves no trace is the worst failure mode an intake
    system can have, so this table exists to make every drop visible at
    GET /dead-letters.

    `payload` is TEXT, not JSON: it is written once by the redactor and only
    ever displayed. A JSON column invites an equality comparison, and Postgres's
    `json` type has no equality operator — the exact Postgres-only bug a
    SQLite-only suite hid until Phase 5's review.
    """

    __tablename__ = "dead_letters"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # "slack" | "discord" | "simulator" — which adapter this concerns.
    source: Mapped[str] = mapped_column(String, index=True)
    # "inbound" | "outbound" | "connection"
    kind: Mapped[str] = mapped_column(String, index=True)
    reason: Mapped[str] = mapped_column(String)
    # Redacted before it ever gets here — see DeadLetterRepository.redact.
    payload: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
```

**`server_default=text("''")`, not `""`.** Alembic's SQLite comparator cannot strip the quotes from
a zero-length default literal, so a bare `server_default=""` false-positives as drift against the
migrated schema. The emitted DDL (`DEFAULT ''`) is identical either way. `WorkflowRow.description`
carries the same comment and the same fix.

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd backend && python -m pytest tests/test_orm_phase6.py -v
```

Expected: PASS (3 passed). The `session` fixture builds the schema with
`Base.metadata.create_all()`, so it sees the model before any migration exists — which is why
Step 5 is a separate, independently failing step.

- [ ] **Step 5: Run the drift guard to verify it now fails**

```bash
cd backend && python -m pytest tests/test_migrations.py::test_migrations_match_the_models -v
```

Expected: FAIL — `models and migrations disagree: [('add_table', ...dead_letters...),
('add_column', None, 'tasks', Column('last_notified_state', ...))]`. **This failure is the point:**
`conftest.py` builds the test schema with `create_all()`, NOT Alembic, so without this guard the
new column would exist in every test and be missing from every migrated database.

- [ ] **Step 6: Write the migration**

Create `backend/ley_khaa/alembic/versions/0007_channels.py`:

```python
"""phase 6: channel adapters — dead letters and the notification guard

Revision ID: 0007_channels
Revises: 0006_alias_jsonb

Creates the dead_letters TABLE and adds tasks.last_notified_state. It seeds
nothing: there is nothing to seed, and a migration docstring that claims a seed
it does not perform is the false-statement class of defect commit 8cebd1f
cleaned up.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_channels"
down_revision = "0006_alias_jsonb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dead_letters",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_dead_letters_source", "dead_letters", ["source"])
    op.create_index("ix_dead_letters_kind", "dead_letters", ["kind"])
    op.create_index("ix_dead_letters_created_at", "dead_letters", ["created_at"])

    op.add_column("tasks", sa.Column("last_notified_state", sa.String(), nullable=True))


def downgrade() -> None:
    # LIFO, matching 0002-0006: drop what this revision added, newest first.
    op.drop_column("tasks", "last_notified_state")
    op.drop_index("ix_dead_letters_created_at", table_name="dead_letters")
    op.drop_index("ix_dead_letters_kind", table_name="dead_letters")
    op.drop_index("ix_dead_letters_source", table_name="dead_letters")
    op.drop_table("dead_letters")
```

- [ ] **Step 7: Run the drift guard to verify it passes**

```bash
cd backend && python -m pytest tests/test_migrations.py -v
```

Expected: PASS (3 passed). If `compare_server_default` reports drift on `payload`, you wrote
`server_default=""` instead of `server_default=text("''")` somewhere — fix the ORM, not the test.

- [ ] **Step 8: Run the full backend suite**

```bash
cd backend && python -m pytest -q
```

Expected: 642 passed, 0 skipped, 0 warnings.

- [ ] **Step 9: Commit**

```bash
git add backend/ley_khaa/persistence/orm.py \
        backend/ley_khaa/alembic/versions/0007_channels.py \
        backend/tests/test_orm_phase6.py
git commit -m "$(cat <<'EOF'
feat(schema): add dead_letters and the last-notified guard

The dead-letter table (spec §3.8) makes a dropped inbound message, a failed
notification and a lost connection visible instead of silent.
tasks.last_notified_state is what stops a re-entrant advance() from repeating
a task's clarifying question every pass.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `DeadLetterRepository` and redaction

**Files:**
- Create: `backend/ley_khaa/persistence/dead_letter_repository.py`
- Test: `backend/tests/test_dead_letters.py`

**Interfaces:**
- Consumes: `DeadLetterRow` (Task 1).
- Produces:
  - `redact(payload: object) -> str` — recursive, returns redacted JSON text, truncated.
  - `DeadLetterRepository(session)` with
    `record(*, source: str, kind: str, reason: str, payload: object = None) -> DeadLetterRow`
    and `list(limit: int = 100) -> list[DeadLetterRow]` (newest first).
  - `MAX_PAYLOAD_CHARS: int`.

Every later task that needs to record a drop uses exactly this `record()` signature.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_dead_letters.py`:

```python
import json

from ley_khaa.persistence.dead_letter_repository import (
    MAX_PAYLOAD_CHARS,
    DeadLetterRepository,
    redact,
)


def test_a_dead_letter_is_recorded_and_listed(session):
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="no text", payload={"a": 1})

    rows = repo.list()
    assert len(rows) == 1
    assert rows[0].source == "slack"
    assert rows[0].kind == "inbound"
    assert rows[0].reason == "no text"
    assert json.loads(rows[0].payload) == {"a": 1}


def test_the_newest_dead_letter_comes_first(session):
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="first")
    repo.record(source="slack", kind="inbound", reason="second")

    assert [r.reason for r in repo.list()] == ["second", "first"]


def test_the_limit_is_honoured(session):
    repo = DeadLetterRepository(session)
    for i in range(5):
        repo.record(source="slack", kind="inbound", reason=f"r{i}")

    assert [r.reason for r in repo.list(limit=2)] == ["r4", "r3"]


def test_a_token_never_reaches_storage(session):
    """This is the whole point of the redactor, so it is asserted on the STORED
    text, not on redact()'s return value: a repository that forgot to call the
    redactor would still pass a test written against redact() alone."""
    repo = DeadLetterRepository(session)
    repo.record(
        source="slack",
        kind="inbound",
        reason="translation failed",
        payload={"token": "xoxb-super-secret", "event": {"text": "hello"}},
    )

    stored = repo.list()[0].payload
    assert "xoxb-super-secret" not in stored
    assert "[redacted]" in stored
    assert "hello" in stored, "redaction must not eat the diagnostic content"


def test_redaction_reaches_nested_values_and_lists():
    text = redact(
        {
            "outer": {"api_key": "k", "keep": "yes"},
            "items": [{"Authorization": "Bearer x"}, {"fine": 1}],
        }
    )
    assert "\"k\"" not in text
    assert "Bearer x" not in text
    assert "yes" in text
    assert "\"fine\": 1" in text


def test_redaction_is_case_insensitive_and_matches_substrings():
    text = redact({"SLACK_BOT_TOKEN": "t", "signingSecret": "s", "password": "p"})
    assert "\"t\"" not in text
    assert "\"s\"" not in text
    assert "\"p\"" not in text
    assert text.count("[redacted]") == 3


def test_an_oversized_payload_is_truncated(session):
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="huge", payload={"t": "x" * 50_000})

    stored = repo.list()[0].payload
    assert len(stored) <= MAX_PAYLOAD_CHARS
    assert stored.endswith("…[truncated]")


def test_an_unserialisable_payload_is_described_rather_than_raising(session):
    """A dead letter exists to record a failure. It must not become one."""
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="odd", payload={"o": object()})

    assert repo.list()[0].payload  # something readable, and no exception


def test_no_payload_stores_an_empty_string(session):
    repo = DeadLetterRepository(session)
    repo.record(source="discord", kind="connection", reason="socket closed")

    assert repo.list()[0].payload == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_dead_letters.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named
'ley_khaa.persistence.dead_letter_repository'`.

- [ ] **Step 3: Write the implementation**

Create `backend/ley_khaa/persistence/dead_letter_repository.py`:

```python
"""Where a dropped message goes so it is not simply gone (spec §3.8, §7).

This is the first part of the system that handles credentials, and a dead
letter is the one place a raw platform payload would otherwise be written to
disk verbatim — Slack's own Socket Mode envelope carries a `token` field. So
redaction is not a nicety here; it is the reason this module owns payload
serialisation rather than letting callers pass a pre-rendered string.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .orm import DeadLetterRow

# A payload is a diagnostic, not an archive. Big enough to hold a whole Slack
# event, small enough that a flood cannot fill the database.
MAX_PAYLOAD_CHARS = 4_000

_TRUNCATED = "…[truncated]"

# Substring match, case-folded: a credential added later ("app_token",
# "SLACK_SIGNING_SECRET") is caught by default rather than leaking until
# somebody notices it in the dashboard.
_SECRET_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "api_key",
    "apikey",
    "signing",
    "credential",
)

REDACTED = "[redacted]"


def _scrub(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_secret(key) else _scrub(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


def _is_secret(key: object) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def redact(payload: object) -> str:
    """Serialise `payload` with every credential-shaped key replaced.

    Never raises: a payload that will not serialise is described instead, since
    a dead letter records a failure and must not become one. `default=repr`
    covers the values json cannot render; the outer guard covers a container
    that breaks before it gets there.
    """
    if payload is None:
        return ""
    try:
        text = json.dumps(_scrub(payload), default=repr, ensure_ascii=False)
    except Exception:  # pragma: no cover - defensive, see docstring
        text = f"<unserialisable payload: {type(payload).__name__}>"
    if len(text) > MAX_PAYLOAD_CHARS:
        text = text[: MAX_PAYLOAD_CHARS - len(_TRUNCATED)] + _TRUNCATED
    return text


class DeadLetterRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self, *, source: str, kind: str, reason: str, payload: object = None
    ) -> DeadLetterRow:
        row = DeadLetterRow(
            id=str(uuid.uuid4()),
            source=source,
            kind=kind,
            reason=reason,
            payload=redact(payload),
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def list(self, limit: int = 100) -> list[DeadLetterRow]:
        """Newest first — the dashboard shows the most recent drop at the top.

        Ordered by created_at AND id: two rows written in the same clock tick
        (which SQLite's second-resolution timestamps make ordinary in a test)
        would otherwise come back in an arbitrary order, and a test asserting
        on that order would pass by accident.
        """
        return list(
            self.session.scalars(
                select(DeadLetterRow)
                .order_by(DeadLetterRow.created_at.desc(), DeadLetterRow.id.desc())
                .limit(limit)
            )
        )
```

**Note on `test_the_newest_dead_letter_comes_first`:** if it proves flaky because two rows share a
timestamp and the uuid ordering is arbitrary, do NOT relax the assertion — that would be exactly the
"test that passes for the wrong reason" class. Make the ordering real instead: give
`DeadLetterRow.id` a monotonic component, or write the two rows with explicit, distinct
`created_at` values in the test.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_dead_letters.py -v
```

Expected: PASS (9 passed).

- [ ] **Step 5: Mutation-test the two load-bearing assertions**

Each of these must fail for the right reason, then be restored:

1. Delete the `redact(payload)` call in `record()` (store `str(payload)` instead) →
   `test_a_token_never_reaches_storage` must fail on `"xoxb-super-secret" not in stored`.
2. Delete the `len(text) > MAX_PAYLOAD_CHARS` truncation → `test_an_oversized_payload_is_truncated`
   must fail on the length assertion, not on `endswith`.

Purge `__pycache__` between mutations. Record the observed failure messages in your task report.

- [ ] **Step 6: Run the full backend suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/ley_khaa/persistence/dead_letter_repository.py backend/tests/test_dead_letters.py
git commit -m "$(cat <<'EOF'
feat(dead-letters): record drops with credentials redacted

Slack's Socket Mode envelope carries a `token` field, so the repository owns
payload serialisation rather than accepting a pre-rendered string: redaction
cannot be forgotten by a caller. Recording a failure must never become one, so
redact() never raises.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: The `ChannelAdapter` protocol, `Destination`, and the Simulator retrofit

**Files:**
- Create: `backend/ley_khaa/adapters/__init__.py` (empty)
- Create: `backend/ley_khaa/adapters/base.py`
- Modify: `backend/ley_khaa/intake/simulator.py`
- Test: `backend/tests/test_adapter_base.py`

**Interfaces:**
- Consumes: nothing.
- Produces, and every later task depends on these exact names:
  - `Destination(source: str, conversation_id: str, external_id: str | None)` — frozen dataclass.
  - `ChannelAdapter` — `Protocol` with `name: str`, `async start()`, `async stop()`,
    `async notify(dest: Destination, text: str) -> None`.
  - `AdapterError(Exception)` — an adapter's connection failed.
  - `TranslationError(Exception)` — an event that looked like work could not be translated.
  - `channel_set(raw: str) -> frozenset[str]`.
  - `Simulator` gains `name = "simulator"`, `start()`, `stop()`, `notify()`.

**Why `Destination` carries `source`.** §3.3 names `conversation_id` and `external_id`; §3.6 says
`ChannelNotifier` "routes to the adapter named by `source`". The router therefore needs `source`,
and it comes from the same `MessageRow` lookup as the other two fields. Carrying it on the
`Destination` rather than passing it beside it keeps "where to answer" a single value.

**Why the Simulator is retrofitted rather than left alone (§3.3).** With three implementations from
the start — Slack, Discord, Simulator — the protocol is shaped by more than one caller. A protocol
shaped around Slack and bolted onto Discord afterwards is the failure this avoids. The Simulator's
`replay()` and `available()` are untouched; it gains the four members and nothing else.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_adapter_base.py`:

```python
import asyncio
import inspect

from ley_khaa.adapters.base import ChannelAdapter, Destination, channel_set
from ley_khaa.intake.simulator import Simulator


def test_a_destination_is_hashable_and_frozen():
    """It is passed across a thread boundary and used as a log key, so it must
    not be mutable in flight."""
    dest = Destination(source="slack", conversation_id="slack:T:C:1.0", external_id="slack:C:1.0")
    assert hash(dest)
    try:
        dest.source = "discord"  # type: ignore[misc]
    except Exception as exc:
        assert "frozen" in str(exc).lower() or isinstance(exc, AttributeError)
    else:
        raise AssertionError("Destination must be frozen")


def test_channel_set_parses_a_comma_separated_allowlist():
    assert channel_set("C1, C2 ,C3") == frozenset({"C1", "C2", "C3"})


def test_channel_set_of_nothing_is_empty_not_permissive():
    """An empty allowlist must mean 'ingest nothing', never 'ingest
    everything'. Decision #4: being invited to a channel is not consent."""
    assert channel_set("") == frozenset()
    assert channel_set("  ,, ") == frozenset()


def test_the_simulator_satisfies_the_adapter_protocol():
    sim = Simulator(orchestrator=None)
    assert isinstance(sim, ChannelAdapter)
    assert sim.name == "simulator"


def test_the_simulator_start_stop_and_notify_are_awaitable_no_ops():
    """It has no socket to open and no channel to answer into. The point of the
    retrofit is that the protocol has three implementations, not that the
    simulator gains a network."""
    sim = Simulator(orchestrator=None)
    for method in (sim.start, sim.stop):
        assert inspect.iscoroutinefunction(method)

    asyncio.run(sim.start())
    asyncio.run(sim.notify(Destination(source="simulator", conversation_id="c", external_id=None), "hi"))
    asyncio.run(sim.stop())


def test_the_simulator_still_replays(session):
    """The retrofit must not disturb the behaviour every existing simulator
    test depends on — asserted here too so a reviewer sees the guarantee in the
    task that made the change."""
    from ley_khaa.crystallizer.gate import ReadinessGate
    from ley_khaa.llm.heuristic import HeuristicLLM
    from ley_khaa.orchestrator.orchestrator import Orchestrator
    from ley_khaa.persistence.candidate_repository import CandidateRepository
    from ley_khaa.persistence.message_repository import MessageRepository
    from ley_khaa.persistence.repository import TaskRepository

    orchestrator = Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
    )
    results = Simulator(orchestrator).replay("messy_universe_check")
    assert results
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_adapter_base.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.adapters'`.

- [ ] **Step 3: Write `base.py`**

```bash
mkdir -p backend/ley_khaa/adapters
touch backend/ley_khaa/adapters/__init__.py
```

Create `backend/ley_khaa/adapters/base.py`:

```python
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
```

**`runtime_checkable` is deliberate:** it is what lets `isinstance(sim, ChannelAdapter)` be a real
test rather than a comment. Note its limit — it checks member presence, not signatures — so the
signature is pinned separately by `test_the_simulator_start_stop_and_notify_are_awaitable_no_ops`.

- [ ] **Step 4: Retrofit the Simulator**

In `backend/ley_khaa/intake/simulator.py`, add the import and a `logger`, and extend the class
docstring and body. The existing `__init__`, `available()` and `replay()` are unchanged:

```python
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..adapters.base import Destination
from ..orchestrator.orchestrator import IntakeResult, Orchestrator

logger = logging.getLogger(__name__)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "conversations"


class Simulator:
    """Replays a synthetic conversation through the real intake path.

    Timestamps are backdated so the readiness gate sees a settled conversation
    rather than one still in progress.

    Also a ChannelAdapter (spec §3.3): it already goes through IntakeGateway,
    so satisfying the protocol costs four members and means the interface has
    three implementations from the start rather than being shaped around Slack
    and bolted onto the others afterwards. It has no socket and no channel, so
    start/stop are no-ops and notify only logs — a fixture replay has nobody to
    answer.
    """

    name = "simulator"

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator

    async def start(self) -> None:
        """Nothing to connect. Replay is driven by POST /simulate, not by a socket."""

    async def stop(self) -> None:
        """Nothing to disconnect."""

    async def notify(self, dest: Destination, text: str) -> None:
        logger.info("simulator notification for %s: %s", dest.conversation_id, text)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_adapter_base.py tests/test_simulator.py -v
```

Expected: PASS. Watch for a circular import: `simulator.py` imports from `adapters.base`, which
must import nothing from `intake` or `orchestrator` — `base.py` above imports only from the
standard library, which is what keeps that true.

- [ ] **Step 6: Mutation-test the allowlist default**

Change `channel_set("")` to return a set containing `"*"`, or make an empty allowlist fall through
to permissive. `test_channel_set_of_nothing_is_empty_not_permissive` must fail. This is the
assertion that stands between "no configuration" and "ingest every channel the bot was invited to",
so it gets its own mutation check. Restore.

- [ ] **Step 7: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/ley_khaa/adapters/ backend/ley_khaa/intake/simulator.py backend/tests/test_adapter_base.py
git commit -m "$(cat <<'EOF'
feat(adapters): add the ChannelAdapter seam and retrofit the simulator

The protocol gets three implementations from the start rather than being shaped
around Slack. An empty allowlist means ingest nothing, never everything —
being invited to a channel is not consent to read it (decision #4).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `slack/translate.py` — the pure half of the Slack adapter

**Files:**
- Create: `backend/ley_khaa/adapters/slack/__init__.py` (empty)
- Create: `backend/ley_khaa/adapters/slack/translate.py`
- Create: `backend/tests/fixtures/payloads/slack_channel_message.json`
- Create: `backend/tests/fixtures/payloads/slack_thread_reply.json`
- Create: `backend/tests/fixtures/payloads/slack_bot_message.json`
- Create: `backend/tests/fixtures/payloads/slack_file_share.json`
- Test: `backend/tests/test_slack_translate.py`

**Interfaces:**
- Consumes: `TranslationError` (Task 3).
- Produces:
  `translate(payload: dict, *, allowed_channels: frozenset[str], bot_user_id: str | None) -> dict | None`
  and `conversation_parts(conversation_id: str) -> tuple[str, str, str]`.

**The two return shapes, and why they are different.** `None` means *deliberately ignored* — an
unlisted channel, the bot's own message, a non-message event, an edit. Those are normal and must
never reach the dead-letter panel, or the panel that exists to show real drops becomes noise.
`TranslationError` means *this looked like work and could not be handled* — that is what the caller
dead-letters.

**Everything that can be wrong lives here** (§3.2). A defect in thread derivation, the allowlist or
the dedupe key is a unit-test failure in this file; `client.py` holds no decisions.

- [ ] **Step 1: Write the fixtures**

These are synthetic, shaped exactly like real Socket Mode `events_api` envelopes. **No real
workspace, channel or user ids, and no real token** — the `token` field carries an obviously fake
value precisely so the redaction test in Task 2 has a realistic shape to match.

`backend/tests/fixtures/payloads/slack_channel_message.json`:

```json
{
  "token": "fake-verification-token",
  "team_id": "T0SYNTH01",
  "api_app_id": "A0SYNTH01",
  "event": {
    "type": "message",
    "channel": "C0ALLOWED1",
    "user": "U0HUMAN01",
    "text": "compare the Bloomberg universe against FactSet and send the difference as an Excel file",
    "ts": "1756600000.000100",
    "event_ts": "1756600000.000100",
    "channel_type": "channel"
  },
  "type": "event_callback",
  "event_id": "Ev0SYNTH01",
  "event_time": 1756600000
}
```

`backend/tests/fixtures/payloads/slack_thread_reply.json`:

```json
{
  "token": "fake-verification-token",
  "team_id": "T0SYNTH01",
  "api_app_id": "A0SYNTH01",
  "event": {
    "type": "message",
    "channel": "C0ALLOWED1",
    "user": "U0HUMAN01",
    "text": "as a csv please",
    "ts": "1756600300.000200",
    "thread_ts": "1756600000.000100",
    "event_ts": "1756600300.000200",
    "channel_type": "channel"
  },
  "type": "event_callback",
  "event_id": "Ev0SYNTH02",
  "event_time": 1756600300
}
```

`backend/tests/fixtures/payloads/slack_bot_message.json` — what the bot's OWN notification looks
like coming back at us. This is the fixture that keeps the system from feeding itself:

```json
{
  "token": "fake-verification-token",
  "team_id": "T0SYNTH01",
  "api_app_id": "A0SYNTH01",
  "event": {
    "type": "message",
    "subtype": "bot_message",
    "channel": "C0ALLOWED1",
    "bot_id": "B0SYNTH01",
    "username": "ley-khaa",
    "text": "Before I start, I still need: output_format. Can you fill those in?",
    "ts": "1756600100.000100",
    "thread_ts": "1756600000.000100",
    "event_ts": "1756600100.000100",
    "channel_type": "channel"
  },
  "type": "event_callback",
  "event_id": "Ev0SYNTH03",
  "event_time": 1756600100
}
```

`backend/tests/fixtures/payloads/slack_file_share.json`:

```json
{
  "token": "fake-verification-token",
  "team_id": "T0SYNTH01",
  "api_app_id": "A0SYNTH01",
  "event": {
    "type": "message",
    "channel": "C0ALLOWED1",
    "user": "U0HUMAN01",
    "text": "here is the screenshot and the holdings file",
    "ts": "1756600500.000100",
    "event_ts": "1756600500.000100",
    "channel_type": "channel",
    "files": [
      {
        "id": "F0SYNTH01",
        "name": "screenshot.png",
        "mimetype": "image/png",
        "url_private": "https://files.example.invalid/screenshot.png"
      },
      {
        "id": "F0SYNTH02",
        "name": "holdings.csv",
        "mimetype": "text/csv",
        "url_private": "https://files.example.invalid/holdings.csv"
      }
    ]
  },
  "type": "event_callback",
  "event_id": "Ev0SYNTH04",
  "event_time": 1756600500
}
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_slack_translate.py`:

```python
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


def test_a_message_from_the_bots_user_id_is_dropped():
    """The same filter from the other direction: a Slack app posting as a user
    token carries `user`, not `bot_id`."""
    payload = _payload("slack_channel_message")
    payload["event"]["user"] = BOT
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
    it is the half CI can actually exercise (§3.2)."""
    import sys

    import ley_khaa.adapters.slack.translate as module

    assert "slack_sdk" not in {name.split(".")[0] for name in vars(module) if not name.startswith("_")}
    assert module.__name__ in sys.modules
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_slack_translate.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.adapters.slack'`.

- [ ] **Step 4: Write the implementation**

```bash
mkdir -p backend/ley_khaa/adapters/slack
touch backend/ley_khaa/adapters/slack/__init__.py
```

Create `backend/ley_khaa/adapters/slack/translate.py`:

```python
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
    channel = event.get("channel")
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
    except ValueError as exc:
        raise TranslationError(f"unparsable Slack ts {ts!r}") from exc
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


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
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_slack_translate.py -v
```

Expected: PASS (20 passed).

- [ ] **Step 6: Mutation-test the three load-bearing filters**

Each must fail for the right reason, then be restored. Purge `__pycache__` between mutations.

1. Delete the `if event.get("bot_id"): return None` line → `test_the_bots_own_message_is_dropped`
   must fail because a dict came back where `None` was expected. **This is the one that matters
   most:** without it, every notification the bot posts is ingested as new work.
2. Delete the allowlist check → `test_a_message_from_an_unlisted_channel_is_dropped` AND
   `test_an_empty_allowlist_drops_everything` must both fail.
3. Change `external_id` to a bare `ts` → `test_the_external_id_is_namespaced_by_channel` must fail
   on the value, not on a KeyError.

- [ ] **Step 7: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/ley_khaa/adapters/slack/ backend/tests/test_slack_translate.py backend/tests/fixtures/payloads/
git commit -m "$(cat <<'EOF'
feat(slack): translate Socket Mode events into intake dicts

Pure, dependency-free, and where every decision that can be wrong lives: the
allowlist runs before any parsing so an unlisted channel cannot reach storage
by any path, the self-message filter stops the bot ingesting its own
notifications, and the dedupe key is namespaced by channel because a Slack ts
is unique only within one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `discord/translate.py` — the pure half of the Discord adapter

**Files:**
- Create: `backend/ley_khaa/adapters/discord/__init__.py` (empty)
- Create: `backend/ley_khaa/adapters/discord/translate.py`
- Create: `backend/tests/fixtures/payloads/discord_channel_message.json`
- Create: `backend/tests/fixtures/payloads/discord_thread_reply.json`
- Create: `backend/tests/fixtures/payloads/discord_bot_message.json`
- Create: `backend/tests/fixtures/payloads/discord_attachment_message.json`
- Test: `backend/tests/test_discord_translate.py`

**Interfaces:**
- Consumes: `TranslationError` (Task 3).
- Produces:
  `translate(payload: dict, *, allowed_channels: frozenset[str], bot_user_id: str | None) -> dict | None`
  and `conversation_parts(conversation_id: str) -> tuple[str, str, str]`.

**The payload shape is the seam.** `discord.py` hands `client.py` a `discord.Message` OBJECT, not a
dict, so `client.py` flattens it into the dict below before calling `translate`. That dict mirrors
Discord's own MESSAGE_CREATE gateway JSON with one addition — `parent_id` — because the flattening
is where `message.channel.parent_id` is available and the pure function must not need a live
object to find it. Task 11 builds exactly this dict and nothing else.

```json
{
  "id": "1180000000000000002",
  "channel_id": "998877665544332211",
  "parent_id": null,
  "guild_id": "112233445566778899",
  "content": "…",
  "timestamp": "2026-08-31T09:00:00+00:00",
  "type": 0,
  "author": {"id": "555", "username": "ana", "bot": false},
  "attachments": [{"id": "1", "filename": "a.png", "url": "https://…", "content_type": "image/png"}]
}
```

**The trap this task exists to avoid.** In Discord, a message posted *inside a thread* has
`channel_id` equal to **the thread's** id, not the parent channel's. Checking the allowlist against
`channel_id` alone would reject every threaded reply in an allowlisted channel — which is precisely
the path the clarification loop runs on, so the feature would look like it worked until the moment
someone answered a question. The allowlist is checked against `parent_id or channel_id`.

- [ ] **Step 1: Write the fixtures**

Synthetic snowflakes, no real guild, channel or user ids.

`backend/tests/fixtures/payloads/discord_channel_message.json`:

```json
{
  "id": "1180000000000000002",
  "channel_id": "998877665544332211",
  "parent_id": null,
  "guild_id": "112233445566778899",
  "content": "compare the Bloomberg universe against FactSet and send the difference as an Excel file",
  "timestamp": "2026-08-31T09:00:00+00:00",
  "type": 0,
  "author": {"id": "555000000000000001", "username": "ana", "bot": false},
  "attachments": []
}
```

`backend/tests/fixtures/payloads/discord_thread_reply.json` — posted in the thread that
`1180000000000000002` started, so `channel_id` is the THREAD and `parent_id` is the channel:

```json
{
  "id": "1180000000000000003",
  "channel_id": "1180000000000000002",
  "parent_id": "998877665544332211",
  "guild_id": "112233445566778899",
  "content": "as a csv please",
  "timestamp": "2026-08-31T09:05:00+00:00",
  "type": 0,
  "author": {"id": "555000000000000001", "username": "ana", "bot": false},
  "attachments": []
}
```

`backend/tests/fixtures/payloads/discord_bot_message.json`:

```json
{
  "id": "1180000000000000004",
  "channel_id": "1180000000000000002",
  "parent_id": "998877665544332211",
  "guild_id": "112233445566778899",
  "content": "Before I start, I still need: output_format. Can you fill those in?",
  "timestamp": "2026-08-31T09:02:00+00:00",
  "type": 0,
  "author": {"id": "999000000000000001", "username": "ley-khaa", "bot": true},
  "attachments": []
}
```

`backend/tests/fixtures/payloads/discord_attachment_message.json`:

```json
{
  "id": "1180000000000000005",
  "channel_id": "998877665544332211",
  "parent_id": null,
  "guild_id": "112233445566778899",
  "content": "here is the screenshot and the holdings file",
  "timestamp": "2026-08-31T09:10:00+00:00",
  "type": 0,
  "author": {"id": "555000000000000001", "username": "ana", "bot": false},
  "attachments": [
    {
      "id": "700000000000000001",
      "filename": "screenshot.png",
      "url": "https://cdn.example.invalid/screenshot.png",
      "content_type": "image/png"
    },
    {
      "id": "700000000000000002",
      "filename": "holdings.csv",
      "url": "https://cdn.example.invalid/holdings.csv",
      "content_type": "text/csv"
    }
  ]
}
```

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_discord_translate.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_discord_translate.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.adapters.discord'`.

- [ ] **Step 4: Write the implementation**

```bash
mkdir -p backend/ley_khaa/adapters/discord
touch backend/ley_khaa/adapters/discord/__init__.py
```

Create `backend/ley_khaa/adapters/discord/translate.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_discord_translate.py -v
```

Expected: PASS (20 passed).

- [ ] **Step 6: Mutation-test the thread rule and both self-filters**

1. Change `channel = parent_id or channel_id` to `channel = channel_id` →
   `test_a_thread_reply_in_an_allowlisted_channel_is_allowed` and
   `test_a_thread_reply_lands_in_the_same_conversation_as_its_parent` must both fail. This is the
   defect the whole task is shaped around.
2. Delete `if author.get("bot"): return None` → `test_a_bot_authored_message_is_dropped` fails.
3. Delete the `str(author_id) == bot_user_id` check → `test_a_message_from_the_bots_user_id_is_dropped`
   fails. **Both must be verified separately** — a single test covering both guards would let
   either be deleted later without anything going red.

- [ ] **Step 7: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/ley_khaa/adapters/discord/ backend/tests/test_discord_translate.py backend/tests/fixtures/payloads/
git commit -m "$(cat <<'EOF'
feat(discord): translate gateway messages into intake dicts

The allowlist is checked against the PARENT channel: a message inside a thread
carries the thread's id as channel_id, so checking that alone would reject
every answer to every clarifying question — the clarification loop's own path.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: The `Notifier` seam and the notification policy

**Files:**
- Create: `backend/ley_khaa/adapters/notifier.py`
- Test: `backend/tests/test_notification_policy.py`

**Interfaces:**
- Consumes: `Destination` (Task 3), `TaskRow`, `TaskState`.
- Produces:
  - `Notifier` — `Protocol` with `name: str` and `notify(dest: Destination, text: str) -> None`
    (**synchronous** — see below).
  - `NullNotifier` — the default; does nothing.
  - `RecordingNotifier` — records `(dest, text)` pairs; the offline stand-in tests use it.
  - `NOTIFY_STATES: frozenset[TaskState]`.
  - `message_for(row: TaskRow) -> str | None`.
  - `set_notifier(notifier)` / `current_notifier()` — the process-wide holder.

**Why `Notifier.notify` is synchronous** while `ChannelAdapter.notify` is a coroutine:
`TaskDriver.advance()` is synchronous and, in workers mode, already runs inside `asyncio.to_thread`
on a dispatcher worker. A `Notifier` is called from that worker thread. `ChannelNotifier` (Task 8)
is where the sync call becomes an async one, via `asyncio.run_coroutine_threadsafe` onto the loop
the supervisor captured. Making the seam async would force `advance()` to become async, which would
rewrite the entire driver — the exact opposite of "the pipeline downstream of the gateway is
untouched" (§3.1).

**Why a process-wide holder.** `build_orchestrator(session)` is a module-level function in
`api/app.py` and the dispatcher reaches it through `_drive_task(session, task_id)`, which has no
`app` handle and never will — the dispatcher is deliberately ignorant of FastAPI. So the notifier
cannot ride on `app.state`. One module-level holder, set by the lifespan and reset by it on
shutdown, is the honest shape; it is defaulted to `NullNotifier()` so nothing that forgets to set it
can misbehave.

**Notification policy (§3.6), and nothing else:** a bot narrating every transition is noise, and
noise gets muted.

| State | Message |
|---|---|
| `needs_clarification` | the open question |
| `awaiting_approval` | the recommended mode and its reason |
| `done` | completion and where the bundle is |
| `failed` | the failure reason |

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_notification_policy.py`:

```python
import pytest

from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.notifier import (
    NOTIFY_STATES,
    NullNotifier,
    RecordingNotifier,
    current_notifier,
    message_for,
    set_notifier,
)
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.orm import TaskRow


def _row(state: TaskState, **fields) -> TaskRow:
    return TaskRow(id="t1", project="default", state=state.value, title="universe check", **fields)


# Exactly the four states in §3.6's table, and no others. Table-driven so a
# fifth state added later has to be added here deliberately.
_SILENT = [
    TaskState.RECEIVED,
    TaskState.CLASSIFIED,
    TaskState.INTERPRETED,
    TaskState.EXECUTING,
    TaskState.VALIDATING,
]


@pytest.mark.parametrize("state", sorted(_SILENT, key=lambda s: s.value))
def test_an_in_flight_state_says_nothing(state):
    assert message_for(_row(state)) is None


def test_the_notify_states_are_exactly_the_four_in_the_policy():
    assert NOTIFY_STATES == frozenset(
        {
            TaskState.NEEDS_CLARIFICATION,
            TaskState.AWAITING_APPROVAL,
            TaskState.DONE,
            TaskState.FAILED,
        }
    )


def test_every_notify_state_produces_a_message():
    """The set and the renderer must agree: a state in NOTIFY_STATES with no
    branch in message_for would silently notify nothing."""
    for state in NOTIFY_STATES:
        assert message_for(_row(state)), f"{state} is in NOTIFY_STATES but renders nothing"


def test_needs_clarification_asks_the_open_question():
    text = message_for(
        _row(TaskState.NEEDS_CLARIFICATION, open_question="Which output format?")
    )
    assert "Which output format?" in text


def test_needs_clarification_with_no_question_still_asks_something():
    """A task can reach this state with open_question NULL (the validate path
    clears it). Posting an empty string into a channel is worse than a generic
    prompt."""
    text = message_for(_row(TaskState.NEEDS_CLARIFICATION, open_question=None))
    assert text.strip()


def test_awaiting_approval_names_the_mode_and_the_reason():
    text = message_for(
        _row(
            TaskState.AWAITING_APPROVAL,
            recommended_mode="suggest",
            autonomy_reason="low certainty → suggest",
        )
    )
    assert "suggest" in text
    assert "low certainty" in text


def test_awaiting_approval_reports_the_effective_mode_not_the_recommendation():
    """A human who pinned a mode must be told what is actually in force, not
    what the engine wanted — effective_mode is the field the dashboard shows
    and the driver acts on."""
    text = message_for(
        _row(
            TaskState.AWAITING_APPROVAL,
            recommended_mode="suggest",
            mode_override="copilot",
            autonomy_reason="r",
        )
    )
    assert "copilot" in text
    assert "suggest" not in text


def test_done_says_where_the_bundle_is():
    text = message_for(_row(TaskState.DONE, workspace_path="/work/task-workspaces/task-t1"))
    assert "/work/task-workspaces/task-t1" in text


def test_done_with_no_bundle_still_reports_completion():
    text = message_for(_row(TaskState.DONE, workspace_path=None))
    assert text.strip()
    assert "None" not in text


def test_failed_gives_the_reason():
    text = message_for(_row(TaskState.FAILED, failure_reason="the sandbox was unavailable"))
    assert "the sandbox was unavailable" in text


def test_failed_with_no_reason_still_reports_the_failure():
    text = message_for(_row(TaskState.FAILED, failure_reason=None))
    assert text.strip()
    assert "None" not in text


def test_the_null_notifier_does_nothing_and_says_so():
    notifier = NullNotifier()
    assert notifier.name == "null"
    assert notifier.notify(Destination(source="slack", conversation_id="c"), "hi") is None


def test_the_recording_notifier_keeps_what_it_was_asked_to_send():
    notifier = RecordingNotifier()
    dest = Destination(source="slack", conversation_id="c", external_id="e")
    notifier.notify(dest, "hello")
    assert notifier.sent == [(dest, "hello")]


def test_the_default_notifier_is_null():
    assert isinstance(current_notifier(), NullNotifier)


def test_the_holder_can_be_set_and_reset():
    recording = RecordingNotifier()
    previous = current_notifier()
    set_notifier(recording)
    try:
        assert current_notifier() is recording
    finally:
        set_notifier(previous)
    assert current_notifier() is previous
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_notification_policy.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.adapters.notifier'`.

- [ ] **Step 3: Write the implementation**

Create `backend/ley_khaa/adapters/notifier.py`. `ChannelNotifier` is added to this same file in
Task 8; write only what is below now.

```python
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

    reason = (row.failure_reason or "").strip() or "no reason was recorded"
    return f"“{title}” failed: {reason}"


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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_notification_policy.py -v
```

Expected: PASS (19 passed).

- [ ] **Step 5: Mutation-test the policy boundary**

1. Add `TaskState.EXECUTING` to `NOTIFY_STATES` → `test_an_in_flight_state_says_nothing[executing]`
   must fail, AND `test_the_notify_states_are_exactly_the_four_in_the_policy` must fail, AND
   `test_every_notify_state_produces_a_message` must fail. Three failures for one mutation is the
   sign the set and the renderer are genuinely pinned to each other.
2. Change `row.effective_mode` to `row.recommended_mode` in the `AWAITING_APPROVAL` branch →
   `test_awaiting_approval_reports_the_effective_mode_not_the_recommendation` must fail on
   `"copilot" in text`.

- [ ] **Step 6: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/ley_khaa/adapters/notifier.py backend/tests/test_notification_policy.py
git commit -m "$(cat <<'EOF'
feat(notify): add the Notifier seam and the four-state policy

Same shape as LLMClient and SandboxRunner, with an offline default that keeps
every existing test and every token-free run unchanged. Only the four states in
§3.6 speak: a bot narrating every transition is noise, and noise gets muted.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Wire the notifier into the driver, guarded by `last_notified_state`

**Files:**
- Modify: `backend/ley_khaa/persistence/repository.py` (add `mark_notified`)
- Modify: `backend/ley_khaa/orchestrator/driver.py` (accept a notifier; add `_announce`)
- Modify: `backend/ley_khaa/orchestrator/orchestrator.py` (pass the notifier through)
- Test: `backend/tests/test_notifier_wiring.py`

**Interfaces:**
- Consumes: `Notifier`, `NullNotifier`, `RecordingNotifier`, `message_for` (Task 6); `Destination`
  (Task 3).
- Produces:
  - `TaskRepository.mark_notified(task_id: str, state: str) -> bool` — compare-and-swap; True means
    "you won the right to send this".
  - `TaskDriver(..., notifier: Notifier | None = None)`; `TaskDriver._announce(row) -> None`.
  - `Orchestrator(..., notifier: Notifier | None = None)`.

**Two orderings this task locks in.**

*Claim before you send.* `mark_notified` succeeds first, and only then is the text handed to the
notifier. That is the same rule as `claim()`/`set_override()`/`fold_into()`, applied to a side
effect that leaves the process. The cost is stated plainly and accepted: a send that then fails is
NOT retried. §9 already says notification is best-effort with dead-lettering, not a durable outbox —
so the alternative (send first, mark after) would trade "a lost message" for "the same question
posted twice on every re-drive", which is worse in a channel.

*Outbound work never fails a task* (§3.6). `_announce` catches everything. A wedged Slack API must
not be able to stop work from completing.

**Where `_announce` is called, and why there.** `advance()` gets one exit point so no return path
can forget it; `reject()` is called separately because it moves a task to FAILED without going
through `advance()` at all.

**A gap this task creates and does not close** — write it into your task report so the docs task
picks it up: `Dispatcher._fail_poison` also moves a task to FAILED, and it has no driver, no
message repository and no notifier. A task abandoned past `max_lease_attempts` therefore fails
without a notification. Do NOT widen the dispatcher here; Task 16 records it as a known limit and a
backlog item.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_notifier_wiring.py`:

```python
from ley_khaa.adapters.notifier import RecordingNotifier
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _orchestrator(session, notifier=None) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
        notifier=notifier,
    )


def _blocked(session, notifier):
    """A task parked in needs_clarification, arriving from a channel-shaped
    message so it has a source, a conversation and an external id to answer to."""
    orchestrator = _orchestrator(session, notifier)
    result = orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": "slack:T1:C1:100.1",
            "external_id": "slack:C1:100.1",
            "author": "U1",
            "text": "compare the holdings against the portfolio",
        }
    )
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.NEEDS_CLARIFICATION.value
    return orchestrator, task


def test_mark_notified_wins_once_per_state(session):
    repo = TaskRepository(session)
    row = repo.create(project="default", title="t", source_message_ids=[])

    assert repo.mark_notified(row.id, "done") is True
    assert repo.mark_notified(row.id, "done") is False, "a re-drive must not re-announce"
    assert repo.mark_notified(row.id, "failed") is True, "a NEW state may be announced"


def test_mark_notified_on_an_unknown_task_is_false_not_an_error(session):
    assert TaskRepository(session).mark_notified("nope", "done") is False


def test_a_parked_task_asks_its_question_in_its_own_conversation(session):
    notifier = RecordingNotifier()
    _orchestrator_, task = _blocked(session, notifier)

    assert len(notifier.sent) == 1
    dest, text = notifier.sent[0]
    assert dest.source == "slack"
    assert dest.conversation_id == "slack:T1:C1:100.1"
    assert dest.external_id == "slack:C1:100.1"
    assert task.open_question in text


def test_re_driving_the_same_state_does_not_repeat_the_question(session):
    """advance() is re-entrant and the sweeper re-drives tasks, so without the
    guard a parked task would repeat its question on every pass."""
    notifier = RecordingNotifier()
    orchestrator, task = _blocked(session, notifier)

    orchestrator.driver.advance(task.id)
    orchestrator.driver.advance(task.id)

    assert len(notifier.sent) == 1


def test_a_new_state_is_announced_even_after_an_earlier_one(session):
    notifier = RecordingNotifier()
    orchestrator, task = _blocked(session, notifier)

    orchestrator.driver.reject(task.id, "not needed after all")

    assert len(notifier.sent) == 2
    assert "not needed after all" in notifier.sent[1][1]


def test_rejection_notifies_even_though_it_never_calls_advance(session):
    """reject() moves a task to FAILED on its own. If _announce were only wired
    into advance(), the human would never be told."""
    notifier = RecordingNotifier()
    orchestrator, task = _blocked(session, notifier)
    notifier.sent.clear()

    orchestrator.driver.reject(task.id, "duplicate")

    assert [t for _d, t in notifier.sent if "duplicate" in t]


def test_a_task_with_no_source_messages_is_not_announced(session):
    """There is nowhere to answer. It must be a silent skip, not a crash."""
    notifier = RecordingNotifier()
    repo = TaskRepository(session)
    orchestrator = _orchestrator(session, notifier)
    row = repo.create(project="default", title="orphan", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION)

    orchestrator.driver.advance(row.id)

    assert notifier.sent == []


def test_a_raising_notifier_never_fails_the_task(session):
    """Spec §3.6: outbound work never fails a task. A wedged Slack API must not
    be able to stop work from completing."""

    class Exploding:
        name = "exploding"

        def notify(self, dest, text):
            raise RuntimeError("slack is down")

    orchestrator, task = _blocked(session, Exploding())
    assert TaskRepository(session).get(task.id).state == TaskState.NEEDS_CLARIFICATION.value


def test_the_default_notifier_is_null_so_nothing_existing_changes(session):
    orchestrator = _orchestrator(session)
    assert orchestrator.driver.notifier.name == "null"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_notifier_wiring.py -v
```

Expected: FAIL — `TypeError: Orchestrator.__init__() got an unexpected keyword argument 'notifier'`.

- [ ] **Step 3: Add `mark_notified` to `TaskRepository`**

In `backend/ley_khaa/persistence/repository.py`, add after `save_memory_hit`:

```python
    def mark_notified(self, task_id: str, state: str) -> bool:
        """Claim the right to announce `state` for this task. True if we won it.

        Same compare-and-swap discipline as claim() and set_override(), applied
        to a side effect that leaves the process. advance() is re-entrant and
        the sweeper re-drives tasks, so an unguarded announcement would repeat a
        task's clarifying question on every pass; two workers reaching the same
        state at once would post it twice.

        The caller sends only after this returns True, which means a send that
        then fails is NOT retried — stated rather than hidden. §9 already says
        notification is best-effort with dead-lettering, not a durable outbox,
        and the alternative ordering trades a lost message for a duplicated one,
        which in a channel is worse.
        """
        result = self.session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                or_(
                    TaskRow.last_notified_state.is_(None),
                    TaskRow.last_notified_state != state,
                ),
            )
            .values(last_notified_state=state)
        )
        self.session.commit()
        return result.rowcount == 1
```

`or_` and `update` are already imported at the top of this file. **`updated_at` is deliberately not
touched:** announcing a task is not a change to the work, and bumping `updated_at` would reorder
the dashboard and the FIFO queue for a purely outbound event.

- [ ] **Step 4: Wire the notifier into `TaskDriver`**

In `backend/ley_khaa/orchestrator/driver.py`:

Add to the imports:

```python
from ..adapters.base import Destination
from ..adapters.notifier import Notifier, NullNotifier, message_for
```

Add the parameter to `__init__` (keyword-only, defaulted, so no existing call site changes):

```python
        workflows: WorkflowRepository | None = None,
        memories: MemoryRepository | None = None,
        notifier: Notifier | None = None,
    ) -> None:
```

and in the body, beside the other assignments:

```python
        # NullNotifier by default, so every existing test and every token-free
        # run behaves exactly as it did before this phase.
        self.notifier = notifier or NullNotifier()
```

Replace `advance()` with a two-part version. **The body of the loop is moved verbatim into
`_drive`; do not retype it** — its KeyError on a missing row and its step-ceiling warning are
depended on by existing tests:

```python
    def advance(self, task_id: str) -> TaskRow:
        """Push a task as far as it can go unattended, then return where it landed.

        One exit point, so no return path can forget to announce. The driving
        itself is unchanged and lives in _drive.
        """
        row = self._drive(task_id)
        self._announce(row)
        return row

    def _drive(self, task_id: str) -> TaskRow:
        for _ in range(_MAX_STEPS):
            row = self.repo.get(task_id)
            if row is None:
                raise KeyError(task_id)
            state = TaskState(row.state)
            if state in _WAITING:
                return row
            if not _STEPS[state](self, row):
                # No progress: a lost claim (another caller won the race) or a
                # retryable failure. Either way, stop here.
                return self.repo.get(task_id)
        logger.warning("task %s hit the step ceiling; leaving it where it is", task_id)
        return self.repo.get(task_id)
```

Add `_announce`, next to `_remember` at the end of the automatic steps:

```python
    def _announce(self, row: TaskRow | None) -> None:
        """Tell the originating channel, at most once per state (spec §3.6).

        Everything here is best-effort and nothing here can fail a task: a
        wedged platform API must not be able to stop work from completing. The
        order is claim-then-send — mark_notified is a compare-and-swap, so a
        re-entrant advance() or a second worker cannot repeat the message.
        """
        if row is None:
            return
        try:
            text = message_for(row)
            if text is None:
                return
            dest = self._destination(row)
            if dest is None:
                # No originating message means no channel to answer into. A
                # task created directly (a test, a future CLI) is not a failure.
                return
            if not self.repo.mark_notified(row.id, row.state):
                return
            self.notifier.notify(dest, text)
        except Exception:
            logger.exception("could not announce task %s", row.id)

    def _destination(self, row: TaskRow) -> Destination | None:
        """Where this task's channel conversation is.

        No mapping table (§3.6): the originating MessageRow already carries
        source, conversation_id and external_id. The FIRST source message is the
        anchor — it is the one that started the thread, and its external_id is
        what a threaded reply hangs under.
        """
        sources = self.messages.get_many(list(row.source_message_ids or []))
        if not sources:
            return None
        first = sources[0]
        return Destination(
            source=first.source,
            conversation_id=first.conversation_id,
            external_id=first.external_id,
        )
```

And add the announcement to `reject()`, which never goes through `advance()`. Its last two lines
become:

```python
        self.repo.record_failure(task_id, reason)
        row = self.repo.get(task_id)
        # reject() moves a task to FAILED on its own, so advance()'s single exit
        # point does not cover it. Without this the human who was waiting on the
        # question is never told the task is over.
        self._announce(row)
        return row
```

- [ ] **Step 5: Pass the notifier through `Orchestrator`**

In `backend/ley_khaa/orchestrator/orchestrator.py`, add the import:

```python
from ..adapters.notifier import Notifier
```

add the keyword-only parameter after `projects`:

```python
        projects: ProjectRepository | None = None,
        notifier: Notifier | None = None,
    ) -> None:
```

and pass it into the driver construction:

```python
        self.driver = TaskDriver(
            repo, llm=llm, messages=messages, candidates=candidates,
            workflows=workflows, memories=memories, notifier=notifier,
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_notifier_wiring.py -v
```

Expected: PASS (10 passed). Watch for a circular import — `driver.py` imports
`adapters.notifier`, which imports `persistence.orm` and `domain.states` only. If Python complains,
you have added an import to `notifier.py` that reaches back into `orchestrator`.

- [ ] **Step 7: Mutation-test the guard and the swallow**

1. Make `mark_notified` unconditional (drop the `or_(...)` clause) →
   `test_re_driving_the_same_state_does_not_repeat_the_question` must fail with `2 != 1`, and
   `test_mark_notified_wins_once_per_state` must fail on the second assertion.
2. Remove the `try/except` from `_announce` → `test_a_raising_notifier_never_fails_the_task` must
   fail with `RuntimeError: slack is down`, not with a state assertion.
3. Remove `self._announce(row)` from `reject()` →
   `test_rejection_notifies_even_though_it_never_calls_advance` must fail on an empty list. Note
   that `test_a_new_state_is_announced_even_after_an_earlier_one` also fails; both are kept because
   they guard different things (that FAILED is announced at all, and that a second state is not
   suppressed by the first).

- [ ] **Step 8: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
```

Expected: still 0 failures. If a test that previously asserted on `advance()` now fails, check that
you MOVED the loop into `_drive` rather than rewriting it.

```bash
git add backend/ley_khaa/persistence/repository.py backend/ley_khaa/orchestrator/driver.py \
        backend/ley_khaa/orchestrator/orchestrator.py backend/tests/test_notifier_wiring.py
git commit -m "$(cat <<'EOF'
feat(notify): announce the four human-facing states, once each

advance() gains a single exit point so no return path can forget to announce,
and reject() announces separately because it reaches FAILED without advancing.
mark_notified is a compare-and-swap: a re-entrant advance() or a second worker
cannot repeat a task's clarifying question. Outbound work never fails a task.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: `ChannelNotifier` — the sync/async boundary

**Files:**
- Modify: `backend/ley_khaa/adapters/notifier.py` (append `ChannelNotifier`)
- Test: `backend/tests/test_channel_notifier.py`

**Interfaces:**
- Consumes: `Notifier`, `Destination`, `ChannelAdapter`, `DeadLetterRepository`.
- Produces:
  `ChannelNotifier(adapters: dict[str, ChannelAdapter], *, loop: asyncio.AbstractEventLoop | None, session_factory)`
  with `name = "channel"` and the `Notifier` `notify(dest, text) -> None`.

**The ordering §3.6 and the pre-scan together fix, and it must be exactly this:**

1. **No adapter registered for `dest.source` → return silently.** A task's source can be
   `simulator` (the fresh clone's demo task) or `dashboard` (every `/tasks/{id}/answer` message).
   There is no channel to answer into and that is not a failure. Dead-lettering it would fill the
   panel that exists to show real drops with a fresh clone's own demo traffic.
2. **`dispatch_mode == "inline"` → dead-letter and return.** §3.6, verbatim: inline is the
   single-operator dashboard mode and has no channel to answer into. It is recorded, not delivered.
3. **No running loop → dead-letter and return.** There is nothing to hand the coroutine to.
4. **Otherwise → `asyncio.run_coroutine_threadsafe(adapter.notify(...), loop)`, fire-and-forget.**
   The driver does not wait on the future, so a wedged platform API cannot extend a task's execution
   time. The future's exception is consumed by a done-callback that writes the dead letter — without
   that callback the exception is swallowed by asyncio and the failure is invisible.

**Dead letters go on their own session.** `notify()` is called from a dispatcher worker thread,
which already holds a session for the task it is driving; writing through that one would interleave
an unrelated commit into the middle of the driver's transaction. The `session_factory` gives each
dead letter its own, exactly as `Dispatcher._drive` does.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_channel_notifier.py`:

```python
import asyncio
import threading
from dataclasses import replace

import pytest

from ley_khaa.adapters import notifier as notifier_module
from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.notifier import ChannelNotifier
from ley_khaa.config import settings as real_settings
from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository

SLACK = Destination(source="slack", conversation_id="slack:T1:C1:100.1", external_id="slack:C1:100.1")


class FakeAdapter:
    """A ChannelAdapter that records, and can be told to fail."""

    def __init__(self, name="slack", fail=False):
        self.name = name
        self.fail = fail
        self.sent: list[tuple[Destination, str]] = []
        self.delivered = threading.Event()

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def notify(self, dest: Destination, text: str) -> None:
        if self.fail:
            self.delivered.set()
            raise RuntimeError("slack rejected it")
        self.sent.append((dest, text))
        self.delivered.set()


@pytest.fixture
def loop_in_a_thread():
    """A real running event loop on another thread — the shape production has.

    pytest-asyncio is NOT a dependency of this project (a deliberate Phase 5
    finding: adding it would break the zero-warnings bar), so async behaviour is
    driven with a real loop rather than an async test.
    """
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


@pytest.fixture
def workers_mode(monkeypatch):
    """Settings is a frozen dataclass (a Phase 0 invariant). Pin it by rebinding
    the name on every consuming module — rebinding ley_khaa.config.settings
    alone pins nothing, because each module bound the object at import time."""
    patched = replace(real_settings, dispatch_mode="workers")
    monkeypatch.setattr(notifier_module, "settings", patched)
    return patched


def _notifier(session_factory, adapters, loop):
    return ChannelNotifier(adapters, loop=loop, session_factory=session_factory)


def test_a_notification_reaches_the_named_adapter(session_factory, loop_in_a_thread, workers_mode):
    adapter = FakeAdapter()
    _notifier(session_factory, {"slack": adapter}, loop_in_a_thread).notify(SLACK, "hello")

    assert adapter.delivered.wait(timeout=5), "the coroutine never ran on the loop"
    assert adapter.sent == [(SLACK, "hello")]


def test_a_source_with_no_adapter_is_skipped_silently(session_factory, loop_in_a_thread, workers_mode):
    """A fresh clone's demo task has source 'simulator' and every dashboard
    answer has source 'dashboard'. Dead-lettering those would bury the real
    drops under a clone's own traffic."""
    _notifier(session_factory, {"slack": FakeAdapter()}, loop_in_a_thread).notify(
        Destination(source="simulator", conversation_id="conv-1"), "hello"
    )

    with session_factory() as session:
        assert DeadLetterRepository(session).list() == []


def test_inline_mode_dead_letters_rather_than_delivering(
    session_factory, loop_in_a_thread, monkeypatch
):
    """Spec §3.6: inline is the single-operator dashboard mode and has no
    channel to answer into, so a notification is recorded, not delivered."""
    monkeypatch.setattr(notifier_module, "settings", replace(real_settings, dispatch_mode="inline"))
    adapter = FakeAdapter()

    _notifier(session_factory, {"slack": adapter}, loop_in_a_thread).notify(SLACK, "hello")

    assert adapter.sent == []
    with session_factory() as session:
        rows = DeadLetterRepository(session).list()
    assert len(rows) == 1
    assert rows[0].kind == "outbound"
    assert rows[0].source == "slack"


def test_no_loop_dead_letters(session_factory, workers_mode):
    adapter = FakeAdapter()
    _notifier(session_factory, {"slack": adapter}, None).notify(SLACK, "hello")

    assert adapter.sent == []
    with session_factory() as session:
        assert [r.kind for r in DeadLetterRepository(session).list()] == ["outbound"]


def test_a_failing_delivery_dead_letters_without_raising(
    session_factory, loop_in_a_thread, workers_mode
):
    """The future's exception is consumed by a done-callback. Without one,
    asyncio swallows it and a failed notification is invisible."""
    adapter = FakeAdapter(fail=True)

    _notifier(session_factory, {"slack": adapter}, loop_in_a_thread).notify(SLACK, "hello")

    assert adapter.delivered.wait(timeout=5)
    deadline = threading.Event()
    for _ in range(50):
        with session_factory() as session:
            rows = DeadLetterRepository(session).list()
        if rows:
            break
        deadline.wait(0.1)
    assert len(rows) == 1
    assert rows[0].kind == "outbound"
    assert "slack rejected it" in rows[0].reason


def test_notify_does_not_wait_for_the_adapter(session_factory, loop_in_a_thread, workers_mode):
    """Fire-and-forget (§3.6): a slow platform API must not extend a task's
    execution time. A blocking implementation would sit here for the full 3
    seconds; this asserts on ORDER, not on a duration, so it cannot pass by
    accident of a fast machine."""
    started = threading.Event()
    released = threading.Event()

    class Slow(FakeAdapter):
        async def notify(self, dest, text):
            started.set()
            await asyncio.get_running_loop().run_in_executor(None, released.wait)
            self.sent.append((dest, text))
            self.delivered.set()

    adapter = Slow()
    _notifier(session_factory, {"slack": adapter}, loop_in_a_thread).notify(SLACK, "hello")

    # notify() has already returned while the adapter is still blocked.
    assert started.wait(timeout=5)
    assert adapter.sent == []
    released.set()
    assert adapter.delivered.wait(timeout=5)


def test_the_dead_letter_payload_carries_no_text_secrets(session_factory, workers_mode):
    """The payload is the destination and the reason, never a token — and the
    redactor is what guarantees it, so this asserts on what was stored."""
    _notifier(session_factory, {"slack": FakeAdapter()}, None).notify(SLACK, "hello")

    with session_factory() as session:
        payload = DeadLetterRepository(session).list()[0].payload
    assert "slack:T1:C1:100.1" in payload
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_channel_notifier.py -v
```

Expected: FAIL — `ImportError: cannot import name 'ChannelNotifier'`.

- [ ] **Step 3: Write the implementation**

Append to `backend/ley_khaa/adapters/notifier.py`, and add these to its imports:

```python
import asyncio
from collections.abc import Callable
from concurrent.futures import Future

from ..config import settings
from ..persistence.dead_letter_repository import DeadLetterRepository
from .base import ChannelAdapter
```

```python
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
        session_factory: Callable[[], object],
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
        session = self.session_factory()
        try:
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
            with suppress(Exception):
                session.close()
```

Add `from contextlib import suppress` to the imports.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_channel_notifier.py -v
```

Expected: PASS (7 passed).

- [ ] **Step 5: Mutation-test the two orderings**

1. Move the `dispatch_mode == "inline"` check ABOVE the adapter lookup →
   `test_a_source_with_no_adapter_is_skipped_silently` must fail with one dead letter where none
   was expected. This is the pre-scan's finding #5 and the reason the order is written down.
2. Delete `future.add_done_callback(...)` →
   `test_a_failing_delivery_dead_letters_without_raising` must fail on an empty dead-letter list.
3. Replace the `run_coroutine_threadsafe` call with `future.result()` (i.e. wait for it) →
   `test_notify_does_not_wait_for_the_adapter` must fail or hang; if it hangs, that IS the failure
   — kill it and note the timeout.

- [ ] **Step 6: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/ley_khaa/adapters/notifier.py backend/tests/test_channel_notifier.py
git commit -m "$(cat <<'EOF'
feat(notify): deliver across the sync/async boundary, fire-and-forget

A driver on a worker thread hands its coroutine to the captured loop and does
not wait, so a wedged platform API cannot extend a task's execution time; the
future's exception is consumed by a done-callback that dead-letters, because
asyncio would otherwise swallow it and a failed notification would leave no
trace. A source with no adapter is skipped silently, not dead-lettered — the
demo task and every dashboard answer take that path.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Configuration and `AdapterSupervisor`

**Files:**
- Modify: `backend/ley_khaa/config.py` (five new settings)
- Create: `backend/ley_khaa/adapters/supervisor.py`
- Test: `backend/tests/test_supervisor.py`

**Interfaces:**
- Consumes: `ChannelAdapter`, `AdapterError` (Task 3); `DeadLetterRepository` (Task 2).
- Produces:
  - `settings.slack_bot_token`, `settings.slack_app_token`, `settings.slack_channels`,
    `settings.discord_bot_token`, `settings.discord_channels` — all `str`, all default `""`.
  - `AdapterSupervisor(adapters, *, session_factory, base_backoff=1.0, max_backoff=60.0)` with
    `async start()`, `async stop()`, `.loop`, `.registry` (`dict[str, ChannelAdapter]`).

`build_adapters()` is NOT here — it needs the two clients, so it lands in Task 13 with the lifespan
that calls it. The supervisor takes the adapters it is given, which is also what makes it testable
with no tokens and no network.

**Supervision is a named unit with its own tests, not an afterthought** (§3.4): running adapters
in-process beside the dispatcher is the acknowledged cost of decision #2, so a crash must be logged,
dead-lettered and restarted with backoff, and must never propagate into the API or the dispatcher.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_supervisor.py`:

```python
import asyncio

import pytest

from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.supervisor import AdapterSupervisor
from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository


class Adapter:
    """Counts starts. `crashes` is how many of them blow up before it settles."""

    def __init__(self, name="slack", crashes=0):
        self.name = name
        self.crashes = crashes
        self.starts = 0
        self.stops = 0
        self.settled = asyncio.Event()

    async def start(self) -> None:
        self.starts += 1
        if self.starts <= self.crashes:
            raise RuntimeError(f"{self.name} socket closed")
        self.settled.set()
        # A real adapter blocks here for the life of the process.
        await asyncio.sleep(3600)

    async def stop(self) -> None:
        self.stops += 1

    async def notify(self, dest: Destination, text: str) -> None: ...


def _supervisor(session_factory, adapters):
    # A tiny backoff so the tests do not sleep. The GROWTH is asserted
    # separately, on the computed delays, rather than by timing anything.
    return AdapterSupervisor(
        adapters, session_factory=session_factory, base_backoff=0.001, max_backoff=0.01
    )


def _run(coro):
    return asyncio.run(coro)


def test_every_adapter_is_started(session_factory):
    slack, discord = Adapter("slack"), Adapter("discord")

    async def scenario():
        supervisor = _supervisor(session_factory, [slack, discord])
        await supervisor.start()
        await asyncio.wait_for(
            asyncio.gather(slack.settled.wait(), discord.settled.wait()), timeout=5
        )
        await supervisor.stop()

    _run(scenario())
    assert (slack.starts, discord.starts) == (1, 1)


def test_a_crashing_adapter_is_restarted(session_factory):
    slack = Adapter("slack", crashes=2)

    async def scenario():
        supervisor = _supervisor(session_factory, [slack])
        await supervisor.start()
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    assert slack.starts == 3, "two crashes, then a start that stuck"


def test_a_crashing_adapter_does_not_take_down_its_neighbour(session_factory):
    """The whole point of decision #2's cost being acknowledged: adapters run
    in-process beside the dispatcher, so one must never poison the others."""
    slack = Adapter("slack", crashes=3)
    discord = Adapter("discord")

    async def scenario():
        supervisor = _supervisor(session_factory, [slack, discord])
        await supervisor.start()
        await asyncio.wait_for(discord.settled.wait(), timeout=5)
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    assert discord.starts == 1
    assert slack.starts == 4


def test_a_crash_is_dead_lettered(session_factory):
    slack = Adapter("slack", crashes=1)

    async def scenario():
        supervisor = _supervisor(session_factory, [slack])
        await supervisor.start()
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    with session_factory() as session:
        rows = DeadLetterRepository(session).list()
    assert [r.kind for r in rows] == ["connection"]
    assert rows[0].source == "slack"
    assert "socket closed" in rows[0].reason


def test_the_backoff_grows_and_is_capped(session_factory):
    """Asserted on the computed delays, not by timing a sleep — a duration
    assertion would pass or fail on how busy the machine is."""
    supervisor = _supervisor(session_factory, [])
    delays = []
    delay = supervisor.base_backoff
    for _ in range(8):
        delays.append(delay)
        delay = supervisor.next_backoff(delay)

    assert delays[0] == 0.001
    assert delays[1] == 0.002
    assert delays[2] == 0.004
    assert delays[-1] == supervisor.max_backoff
    assert all(b >= a for a, b in zip(delays, delays[1:])), "backoff must never shrink"


def test_stop_stops_every_adapter(session_factory):
    slack, discord = Adapter("slack"), Adapter("discord")

    async def scenario():
        supervisor = _supervisor(session_factory, [slack, discord])
        await supervisor.start()
        await asyncio.wait_for(
            asyncio.gather(slack.settled.wait(), discord.settled.wait()), timeout=5
        )
        await supervisor.stop()

    _run(scenario())
    assert (slack.stops, discord.stops) == (1, 1)


def test_stop_is_safe_when_nothing_started(session_factory):
    _run(_supervisor(session_factory, []).stop())


def test_the_registry_is_keyed_by_adapter_name(session_factory):
    """ChannelNotifier routes on dest.source, which is exactly this key."""
    supervisor = _supervisor(session_factory, [Adapter("slack"), Adapter("discord")])
    assert set(supervisor.registry) == {"slack", "discord"}


def test_the_loop_is_captured_at_start(session_factory):
    """ChannelNotifier needs it to hand coroutines across from a worker
    thread, and it must be the loop the adapters actually run on."""
    slack = Adapter("slack")
    captured = {}

    async def scenario():
        supervisor = _supervisor(session_factory, [slack])
        assert supervisor.loop is None
        await supervisor.start()
        captured["loop"] = supervisor.loop
        captured["running"] = asyncio.get_running_loop()
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    assert captured["loop"] is captured["running"]


def test_cancellation_is_not_treated_as_a_crash(session_factory):
    """Shutdown cancels the supervised tasks. If CancelledError were caught as
    a failure, every clean shutdown would dead-letter and try to restart."""
    slack = Adapter("slack")

    async def scenario():
        supervisor = _supervisor(session_factory, [slack])
        await supervisor.start()
        await asyncio.wait_for(slack.settled.wait(), timeout=5)
        await supervisor.stop()

    _run(scenario())
    with session_factory() as session:
        assert DeadLetterRepository(session).list() == []
    assert slack.starts == 1, "a cancelled adapter must not be restarted"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_supervisor.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.adapters.supervisor'`.

- [ ] **Step 3: Add the settings**

In `backend/ley_khaa/config.py`, append to the `Settings` dataclass, after `max_lease_attempts`:

```python
    # --- channel adapters (spec §5). Tokens come from the environment only:
    # never committed, never logged, never written into a bundle, never
    # returned by an API. No token -> that adapter does not start, which is
    # what keeps `docker compose up` a zero-account demo.
    slack_bot_token: str = os.getenv("LEY_KHAA_SLACK_BOT_TOKEN", "")
    slack_app_token: str = os.getenv("LEY_KHAA_SLACK_APP_TOKEN", "")
    # Comma-separated channel ids. EMPTY MEANS INGEST NOTHING, never "ingest
    # everything": being invited to a channel is not consent to read it
    # (decision #4), and an adapter with a token and an empty allowlist starts,
    # ingests nothing, and says so — the safe reading of an incomplete
    # configuration.
    slack_channels: str = os.getenv("LEY_KHAA_SLACK_CHANNELS", "")
    discord_bot_token: str = os.getenv("LEY_KHAA_DISCORD_BOT_TOKEN", "")
    discord_channels: str = os.getenv("LEY_KHAA_DISCORD_CHANNELS", "")
```

- [ ] **Step 4: Write the supervisor**

Create `backend/ley_khaa/adapters/supervisor.py`:

```python
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

from ..persistence.dead_letter_repository import DeadLetterRepository
from .base import ChannelAdapter

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
        request's or worker's unit of work."""
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
```

**No payload on a connection dead letter, deliberately:** an exception raised by a platform client
can carry the request that produced it, and that request carries the bot token. The reason string
is `type: message`, which is diagnostic without being a credential leak. The redactor is a second
line of defence, not the first.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_supervisor.py -v
```

Expected: PASS (10 passed). `_dead_letter` calls a synchronous, committing repository from the
event loop; the writes are tiny and SQLite/Postgres both return in microseconds, and a crash loop is
already rate-limited by the backoff. If it ever becomes a problem the fix is `asyncio.to_thread`,
not a queue.

- [ ] **Step 6: Mutation-test the isolation guarantees**

1. Replace `except asyncio.CancelledError: raise` with nothing (let the general `except` catch it) →
   `test_cancellation_is_not_treated_as_a_crash` must fail, on the dead-letter list or on
   `slack.starts == 1`. This is what keeps a clean shutdown from looking like an outage.
2. Change `_supervise` to re-raise on failure instead of looping →
   `test_a_crashing_adapter_does_not_take_down_its_neighbour` must fail.
3. Change `next_backoff` to return `delay * 2` uncapped → the `delays[-1] == max_backoff`
   assertion must fail.

- [ ] **Step 7: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/ley_khaa/config.py backend/ley_khaa/adapters/supervisor.py backend/tests/test_supervisor.py
git commit -m "$(cat <<'EOF'
feat(adapters): supervise adapters so a crash cannot reach the API

Running adapters in-process beside the dispatcher is decision #2's acknowledged
cost, so supervision is a named unit with its own tests: a crash is logged,
dead-lettered and restarted with capped exponential backoff, a neighbour is
unaffected, and cancellation is shutdown rather than an outage. A connection
dead letter carries no payload — a platform client's exception can quote the
request that produced it, and that request carries the token.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: `slack/client.py` — the Socket Mode connection

**Files:**
- Modify: `backend/pyproject.toml` (add `slack_sdk==3.44.0`)
- Create: `backend/ley_khaa/adapters/slack/client.py`
- Test: `backend/tests/test_slack_client.py`

**Interfaces:**
- Consumes: `translate`, `conversation_parts` (Task 4); `Destination`, `AdapterError`,
  `TranslationError` (Task 3).
- Produces:
  ```python
  SlackAdapter(
      *,
      bot_token: str,
      app_token: str,
      allowed_channels: frozenset[str],
      ingest: Callable[[dict], None],
      dead_letter: Callable[..., None],
  )
  ```
  with `name = "slack"`, `async start()`, `async stop()`, `async notify(dest, text)`, and the
  **testable** `async _handle(payload: dict) -> None`.

**The split, restated where it matters most.** `_handle` is the whole of this adapter's behaviour
and it takes a plain dict, so CI exercises it fully. `start()` is the only truly untestable part —
it connects a socket and hands whatever arrives to `_handle`. Keep it that small.

**Two callables, not a session.** §5.1 says adapters hold no business logic. `ingest` and
`dead_letter` are injected by Task 13's lifespan; this module never sees a `Session`, an
`Orchestrator` or a repository.

**`ingest` is synchronous and must not run on the event loop.** The whole intake pipeline is sync
SQLAlchemy; calling it directly from a coroutine would block the loop — and with it every other
adapter and the dispatcher — for the length of an LLM call. `await asyncio.to_thread(self.ingest, raw)`.

- [ ] **Step 1: Add the dependency**

In `backend/pyproject.toml`, add to `dependencies`:

```toml
    "slack_sdk==3.44.0",
```

Pinned exactly, matching `openpyxl==3.1.5` / `python-docx==1.2.0`. Then:

```bash
cd backend && ../.venv/bin/pip install -e '.[dev]'
```

The root `.venv` goes stale whenever a phase adds a dependency — this is the standing gotcha. The
**sandbox image is untouched** (§6): synthesized code has no business talking to Slack.

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_slack_client.py`:

```python
import asyncio
import json
import threading
from pathlib import Path

from ley_khaa.adapters.slack.client import SlackAdapter

PAYLOADS = Path(__file__).resolve().parent / "fixtures" / "payloads"
ALLOWED = frozenset({"C0ALLOWED1"})


def _payload(name: str) -> dict:
    return json.loads((PAYLOADS / f"{name}.json").read_text())


def _adapter(**kwargs):
    ingested: list[dict] = []
    dead: list[dict] = []
    adapter = SlackAdapter(
        bot_token="xoxb-not-a-real-token",
        app_token="xapp-not-a-real-token",
        allowed_channels=kwargs.pop("allowed_channels", ALLOWED),
        ingest=kwargs.pop("ingest", None) or ingested.append,
        dead_letter=lambda **kw: dead.append(kw),
    )
    adapter.bot_user_id = kwargs.pop("bot_user_id", "U0BOT0001")
    return adapter, ingested, dead


def test_the_adapter_is_named_slack():
    adapter, _, _ = _adapter()
    assert adapter.name == "slack"


def test_a_channel_message_is_ingested():
    adapter, ingested, dead = _adapter()
    asyncio.run(adapter._handle(_payload("slack_channel_message")))

    assert len(ingested) == 1
    assert ingested[0]["source"] == "slack"
    assert dead == []


def test_the_bots_own_message_is_neither_ingested_nor_dead_lettered():
    """A normal drop is silent. If it dead-lettered, the panel would fill with
    the bot's own notifications."""
    adapter, ingested, dead = _adapter()
    asyncio.run(adapter._handle(_payload("slack_bot_message")))

    assert ingested == []
    assert dead == []


def test_an_unlisted_channel_is_neither_ingested_nor_dead_lettered():
    adapter, ingested, dead = _adapter(allowed_channels=frozenset())
    asyncio.run(adapter._handle(_payload("slack_channel_message")))

    assert (ingested, dead) == ([], [])


def test_a_malformed_event_is_dead_lettered_as_inbound():
    payload = _payload("slack_channel_message")
    payload["event"]["ts"] = "not-a-timestamp"
    adapter, ingested, dead = _adapter()

    asyncio.run(adapter._handle(payload))

    assert ingested == []
    assert len(dead) == 1
    assert dead[0]["kind"] == "inbound"
    assert dead[0]["source"] == "slack"


def test_a_failing_ingest_is_dead_lettered_and_does_not_escape():
    """The event loop runs every adapter and the dispatcher. An exception
    escaping here would be swallowed by asyncio at best and take the socket
    down at worst."""

    def boom(_raw):
        raise RuntimeError("the database is down")

    adapter, _, dead = _adapter(ingest=boom)
    asyncio.run(adapter._handle(_payload("slack_channel_message")))

    assert len(dead) == 1
    assert dead[0]["kind"] == "inbound"
    assert "the database is down" in dead[0]["reason"]


def test_ingest_runs_off_the_event_loop():
    """The intake pipeline is synchronous SQLAlchemy. Running it on the loop
    would block every other adapter and the dispatcher for the length of an LLM
    call, so it must be handed to a thread."""
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def record(_raw):
        seen.append(threading.get_ident())

    adapter, _, _ = _adapter(ingest=record)
    asyncio.run(adapter._handle(_payload("slack_channel_message")))

    assert seen and seen[0] != loop_thread


def test_notify_posts_into_the_conversations_thread():
    """The channel and the thread anchor come from the conversation id — no
    mapping table (§3.5)."""
    posted = {}

    class FakeWeb:
        async def chat_postMessage(self, **kwargs):
            posted.update(kwargs)

    from ley_khaa.adapters.base import Destination

    adapter, _, _ = _adapter()
    adapter.web = FakeWeb()
    asyncio.run(
        adapter.notify(
            Destination(
                source="slack",
                conversation_id="slack:T0SYNTH01:C0ALLOWED1:1756600000.000100",
                external_id="slack:C0ALLOWED1:1756600000.000100",
            ),
            "the question",
        )
    )

    assert posted["channel"] == "C0ALLOWED1"
    assert posted["thread_ts"] == "1756600000.000100"
    assert posted["text"] == "the question"


def test_no_token_is_ever_in_the_repr():
    """A token must never be logged, and an adapter ends up in a log line the
    moment anything goes wrong."""
    adapter, _, _ = _adapter()
    assert "xoxb-not-a-real-token" not in repr(adapter)
    assert "xapp-not-a-real-token" not in repr(adapter)
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_slack_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.adapters.slack.client'`.

- [ ] **Step 4: Write the implementation**

Create `backend/ley_khaa/adapters/slack/client.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_slack_client.py -v
```

Expected: PASS (9 passed).

- [ ] **Step 6: Mutation-test the two guarantees CI can actually hold**

1. Replace `await asyncio.to_thread(self.ingest, raw)` with `self.ingest(raw)` →
   `test_ingest_runs_off_the_event_loop` must fail because the recorded thread id equals the
   loop's.
2. Remove the `try/except` around the ingest call →
   `test_a_failing_ingest_is_dead_lettered_and_does_not_escape` must fail with
   `RuntimeError: the database is down`.

- [ ] **Step 7: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/pyproject.toml backend/ley_khaa/adapters/slack/client.py backend/tests/test_slack_client.py
git commit -m "$(cat <<'EOF'
feat(slack): connect over Socket Mode

Outbound WebSocket, so no public URL, no tunnel and no inbound port. The
connection holds no decisions — they are all in translate.py, which CI can
exercise — and _handle takes a plain dict so the adapter's whole behaviour is
testable without a socket. Events are acked before processing, and ingest is
handed to a thread so a synchronous pipeline cannot block the loop that runs
every other adapter and the dispatcher.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: `discord/client.py` — the Gateway connection

**Files:**
- Modify: `backend/pyproject.toml` (add `discord.py==2.7.1`)
- Create: `backend/ley_khaa/adapters/discord/client.py`
- Test: `backend/tests/test_discord_client.py`

**Interfaces:**
- Consumes: `translate`, `conversation_parts` (Task 5); `Destination`, `AdapterError`,
  `TranslationError` (Task 3).
- Produces: `DiscordAdapter(*, bot_token, allowed_channels, ingest, dead_letter)` with
  `name = "discord"`, `async start()`, `async stop()`, `async notify(dest, text)`, the testable
  `async _handle(payload: dict) -> None`, and the **pure** `flatten(message) -> dict`.

**`flatten` is the seam and the reason this task has real tests.** `discord.py` hands the gateway
callback a `discord.Message` OBJECT; `translate` takes a dict. `flatten` is the one function that
converts, it is where `message.channel.parent_id` is reachable, and it is testable against a stub
object with no library and no network. Everything downstream of it is already covered by Task 5.

- [ ] **Step 1: Add the dependency**

In `backend/pyproject.toml`, add to `dependencies`:

```toml
    "discord.py==2.7.1",
```

Then `cd backend && ../.venv/bin/pip install -e '.[dev]'`. Sandbox image untouched (§6).

- [ ] **Step 2: Write the failing tests**

Create `backend/tests/test_discord_client.py`:

```python
import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.discord.client import DiscordAdapter, flatten

PAYLOADS = Path(__file__).resolve().parent / "fixtures" / "payloads"
ALLOWED = frozenset({"998877665544332211"})


def _payload(name: str) -> dict:
    return json.loads((PAYLOADS / f"{name}.json").read_text())


def _adapter(**kwargs):
    ingested: list[dict] = []
    dead: list[dict] = []
    adapter = DiscordAdapter(
        bot_token="not-a-real-token",
        allowed_channels=kwargs.pop("allowed_channels", ALLOWED),
        ingest=kwargs.pop("ingest", None) or ingested.append,
        dead_letter=lambda **kw: dead.append(kw),
    )
    adapter.bot_user_id = kwargs.pop("bot_user_id", "999000000000000001")
    return adapter, ingested, dead


def _message(*, parent_id=None, bot=False):
    """A stand-in for discord.Message with only the attributes flatten reads."""
    return SimpleNamespace(
        id=1180000000000000002,
        content="compare the universes",
        type=SimpleNamespace(value=0),
        created_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
        guild=SimpleNamespace(id=112233445566778899),
        author=SimpleNamespace(id=555000000000000001, bot=bot),
        channel=SimpleNamespace(id=998877665544332211, parent_id=parent_id),
        attachments=[
            SimpleNamespace(
                id=700000000000000001,
                filename="a.png",
                url="https://cdn.example.invalid/a.png",
                content_type="image/png",
            )
        ],
    )


def test_flatten_renders_every_field_translate_reads():
    raw = flatten(_message())
    assert raw["id"] == "1180000000000000002"
    assert raw["channel_id"] == "998877665544332211"
    assert raw["parent_id"] is None
    assert raw["guild_id"] == "112233445566778899"
    assert raw["content"] == "compare the universes"
    assert raw["type"] == 0
    assert raw["author"] == {"id": "555000000000000001", "bot": False}
    assert datetime.fromisoformat(raw["timestamp"]).tzinfo is not None
    assert raw["attachments"] == [
        {
            "id": "700000000000000001",
            "filename": "a.png",
            "url": "https://cdn.example.invalid/a.png",
            "content_type": "image/png",
        }
    ]


def test_flatten_carries_the_parent_channel_of_a_thread():
    """The one fact only the live object knows, and the one the allowlist and
    the conversation id both depend on."""
    raw = flatten(_message(parent_id=998877665544332211))
    assert raw["parent_id"] == "998877665544332211"


def test_flatten_survives_a_message_with_no_guild():
    """A DM has guild None. flatten must render it, not raise — translate is
    what decides to drop it (§9: no DMs in this phase)."""
    message = _message()
    message.guild = None
    assert flatten(message)["guild_id"] is None


def test_flattened_output_is_what_translate_accepts():
    from ley_khaa.adapters.discord.translate import translate

    raw = translate(
        flatten(_message()), allowed_channels=ALLOWED, bot_user_id="999000000000000001"
    )
    assert raw is not None and raw["source"] == "discord"


def test_the_adapter_is_named_discord():
    adapter, _, _ = _adapter()
    assert adapter.name == "discord"


def test_a_channel_message_is_ingested():
    adapter, ingested, dead = _adapter()
    asyncio.run(adapter._handle(_payload("discord_channel_message")))
    assert len(ingested) == 1 and dead == []


def test_a_bot_message_is_neither_ingested_nor_dead_lettered():
    adapter, ingested, dead = _adapter()
    asyncio.run(adapter._handle(_payload("discord_bot_message")))
    assert (ingested, dead) == ([], [])


def test_a_malformed_message_is_dead_lettered_as_inbound():
    payload = _payload("discord_channel_message")
    payload["timestamp"] = "yesterday"
    adapter, ingested, dead = _adapter()

    asyncio.run(adapter._handle(payload))

    assert ingested == []
    assert dead[0]["kind"] == "inbound" and dead[0]["source"] == "discord"


def test_a_failing_ingest_is_dead_lettered_and_does_not_escape():
    def boom(_raw):
        raise RuntimeError("the database is down")

    adapter, _, dead = _adapter(ingest=boom)
    asyncio.run(adapter._handle(_payload("discord_channel_message")))

    assert "the database is down" in dead[0]["reason"]


def test_ingest_runs_off_the_event_loop():
    loop_thread = threading.get_ident()
    seen: list[int] = []
    adapter, _, _ = _adapter(ingest=lambda _raw: seen.append(threading.get_ident()))

    asyncio.run(adapter._handle(_payload("discord_channel_message")))

    assert seen and seen[0] != loop_thread


def test_notify_posts_into_the_thread_named_by_the_conversation_id():
    sent = {}

    class FakeChannel:
        async def send(self, text):
            sent["text"] = text

    class FakeClient:
        def get_channel(self, channel_id):
            sent["channel_id"] = channel_id
            return FakeChannel()

    adapter, _, _ = _adapter()
    adapter.client = FakeClient()
    asyncio.run(
        adapter.notify(
            Destination(
                source="discord",
                conversation_id="discord:112233445566778899:998877665544332211:1180000000000000002",
            ),
            "the question",
        )
    )

    assert sent["channel_id"] == 1180000000000000002
    assert sent["text"] == "the question"


def test_no_token_is_ever_in_the_repr():
    adapter, _, _ = _adapter()
    assert "not-a-real-token" not in repr(adapter)
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_discord_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.adapters.discord.client'`.

- [ ] **Step 4: Write the implementation**

Create `backend/ley_khaa/adapters/discord/client.py`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_discord_client.py -v
```

Expected: PASS (13 passed).

- [ ] **Step 6: Mutation-test `flatten`'s one real decision**

1. Change `"parent_id"` to always be `None` → `test_flatten_carries_the_parent_channel_of_a_thread`
   must fail. That field is what Task 5's whole allowlist rule depends on.
2. Change the id renderings from `str(...)` to the raw ints →
   `test_flatten_renders_every_field_translate_reads` must fail on the id assertions, and
   `test_flattened_output_is_what_translate_accepts` must fail because the allowlist (a set of
   strings) no longer matches.

- [ ] **Step 7: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/pyproject.toml backend/ley_khaa/adapters/discord/client.py backend/tests/test_discord_client.py
git commit -m "$(cat <<'EOF'
feat(discord): connect over the gateway

flatten() is the seam: discord.py hands the callback a Message object and
translate takes a dict, and flatten is the only place message.channel.parent_id
is reachable — the fact the allowlist and the conversation id both depend on. It
is pure, so it has real tests; the socket around it holds no decisions.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: A reply in the thread answers the question (§3.7)

**Files:**
- Modify: `backend/ley_khaa/persistence/message_repository.py` (add `set_reply_target`)
- Modify: `backend/ley_khaa/orchestrator/orchestrator.py` (`ingest` gains the lookup)
- Test: `backend/tests/test_clarification_routing.py`

**Interfaces:**
- Consumes: `MessageRow`, `TaskRepository.list_by_state`, `Orchestrator._route_reply`,
  `Orchestrator._task_conversation_id`.
- Produces:
  - `MessageRepository.set_reply_target(message_id: str, task_id: str) -> MessageRow`.
  - `Orchestrator._clarifying_task_in(conversation_id: str) -> TaskRow | None`.

**The rule lives in the orchestrator, not the adapter** (§3.7): §5.1 says adapters hold no business
logic, and deciding what a message *means* is business logic. A Slack thread reply and a dashboard
answer then take the same path, and cannot drift apart.

**Why the reply target is WRITTEN, not just passed along.** `_route_reply` reads
`row.reply_to_task_id`, and that column is what makes the message visibly a reply everywhere else —
in the database, in `/conversations/{id}/messages`, in any later audit. Setting it persistently
means this path and `POST /tasks/{id}/answer` produce *identical* rows, so
`test_the_answer_is_still_a_real_message_in_the_conversation` keeps meaning what it says. The
alternative — passing the task id as an argument — would create a second kind of reply that looks
different in storage from the first.

**Ordering:** `gateway.accept()` has already committed the message by the time `ingest` can look at
it, so the sequence is accept → find the clarifying task → write `reply_to_task_id` → `_route_reply`.
`_route_reply`'s own `ForeignReplyTarget` check then compares the task's conversation to the
message's, which match by construction — it is a no-op on this path, and left in place rather than
special-cased, because it is the guard that protects the explicit-`reply_to_task_id` path.

**⚠ This changes existing intake behaviour on the HTTP path too.** The pre-scan says the existing
suite should survive (`test_task_replies.py` always passes `reply_to_task_id`; `test_simulator.py`
ingests with `promote=False`, so no task is parked during its ingests). **Run the FULL suite at
Step 6.** If something breaks, re-read its intent: a test that meant to send a second *independent*
request gets its own `conversation_id`. Never weaken the rule to keep an old test green — that is
the "test that passes for the wrong reason" class this project has been burned by eight times.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_clarification_routing.py`:

```python
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository

CONV = "slack:T1:C1:100.1"


def _orchestrator(session) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
    )


def _blocked(session, conversation_id=CONV):
    orchestrator = _orchestrator(session)
    result = orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": conversation_id,
            "text": "compare the holdings against the portfolio",
        }
    )
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.NEEDS_CLARIFICATION.value
    return orchestrator, task


def test_a_plain_message_in_a_clarifying_conversation_answers_the_question(session):
    """The headline of §3.7: nobody types a task id into Slack."""
    orchestrator, task = _blocked(session)

    result = orchestrator.ingest(
        {"source": "slack", "client": "T1", "conversation_id": CONV, "text": "as a csv please"}
    )

    assert result.replied_to_task_id == task.id
    refreshed = TaskRepository(session).get(task.id)
    assert refreshed.state == TaskState.AWAITING_APPROVAL.value
    assert refreshed.spec["output_format"] == "csv"


def test_the_answer_is_recorded_as_a_reply_in_the_database(session):
    """The same row shape POST /tasks/{id}/answer produces, so the two paths
    cannot drift apart and an audit shows the message for what it is."""
    orchestrator, task = _blocked(session)

    result = orchestrator.ingest(
        {"source": "slack", "client": "T1", "conversation_id": CONV, "text": "as a csv please"}
    )

    row = MessageRepository(session).get_many([result.message_id])[0]
    assert row.reply_to_task_id == task.id


def test_the_answer_never_spawns_a_second_candidate(session):
    """The original candidate is PROMOTED, which is terminal, so stage B would
    happily start a new one for the same request. The reply text deliberately
    contains words stage A would otherwise claim."""
    orchestrator, _task = _blocked(session)
    before = {c.candidate_key for c in CandidateRepository(session).list_all()}

    orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": CONV,
            "text": "export it as a csv report please",
        }
    )

    assert {c.candidate_key for c in CandidateRepository(session).list_all()} == before


def test_a_message_in_a_conversation_with_no_clarifying_task_is_unaffected(session):
    """§3.7 explicitly: it flows on to the crystallizer and the amendment
    detector as before."""
    orchestrator = _orchestrator(session)
    result = orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": "slack:T1:C1:900.1",
            "text": "compare the Bloomberg universe against FactSet and send it as an Excel file",
        }
    )
    assert result.replied_to_task_id is None
    assert result.task_ids


def test_a_clarifying_task_in_another_conversation_is_not_answered(session):
    """Two channels, two tasks. A message in one must never answer the other's
    question — that would attach a foreign message to a task's source set."""
    orchestrator, task_a = _blocked(session, conversation_id=CONV)
    other = "slack:T1:C2:200.1"

    result = orchestrator.ingest(
        {"source": "slack", "client": "T1", "conversation_id": other, "text": "as a csv please"}
    )

    assert result.replied_to_task_id is None
    assert TaskRepository(session).get(task_a.id).state == TaskState.NEEDS_CLARIFICATION.value


def test_an_explicit_reply_target_still_wins(session):
    """The dashboard names a task id, and that must keep beating inference."""
    orchestrator, task = _blocked(session)

    result = orchestrator.ingest(
        {
            "source": "dashboard",
            "client": "T1",
            "conversation_id": CONV,
            "text": "as a csv please",
            "reply_to_task_id": task.id,
        }
    )

    assert result.replied_to_task_id == task.id


def test_a_second_message_after_the_question_is_answered_forms_work_again(session):
    """Once the task leaves needs_clarification there is nothing to answer, so
    the conversation goes back to producing candidates. Without this the first
    parked task in a channel would swallow every later message forever."""
    orchestrator, _task = _blocked(session)
    orchestrator.ingest(
        {"source": "slack", "client": "T1", "conversation_id": CONV, "text": "as a csv please"}
    )

    result = orchestrator.ingest(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": CONV,
            "text": "also compare the Bloomberg universe against FactSet as an Excel file",
        }
    )

    assert result.replied_to_task_id is None


def test_the_most_recently_updated_clarifying_task_is_the_one_answered(session):
    """Two parked tasks in one conversation is possible (the simulator's split
    request produces exactly that). The tie-break must be deterministic, or the
    answer lands on an arbitrary one."""
    orchestrator, first = _blocked(session)
    repo = TaskRepository(session)
    second = repo.create(
        project="default", title="second", source_message_ids=list(first.source_message_ids)
    )
    repo.claim(second.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(second.id, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION)

    assert orchestrator._clarifying_task_in(CONV).id == second.id
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_clarification_routing.py -v
```

Expected: FAIL — the first test fails with `result.replied_to_task_id is None`, not with an
ImportError, because `ingest` currently forms a candidate instead.

- [ ] **Step 3: Add `set_reply_target`**

In `backend/ley_khaa/persistence/message_repository.py`, after `record_verdict`:

```python
    def set_reply_target(self, message_id: str, task_id: str) -> MessageRow:
        """Record that this message answers a task.

        The gateway has already committed the row by the time the orchestrator
        can decide this (§3.7), so it is a write rather than a constructor
        argument. Writing it — rather than passing the id along in memory —
        makes an inferred reply and an explicit one IDENTICAL in storage, so
        the two paths cannot drift and an audit shows the message for what it
        is.
        """
        row = self.session.get(MessageRow, message_id)
        if row is None:
            raise KeyError(message_id)
        row.reply_to_task_id = task_id
        self.session.commit()
        self.session.refresh(row)
        return row
```

- [ ] **Step 4: Add the rule to `ingest`**

In `backend/ley_khaa/orchestrator/orchestrator.py`, replace the opening of `ingest`:

```python
    def ingest(self, raw: dict, *, promote: bool = True) -> IntakeResult:
        row = self.gateway.accept(raw)
        if row.reply_to_task_id:
            return self._route_reply(row, promote=promote)

        # Spec §3.7: a message arriving in a conversation whose task is asking
        # a question IS that task's answer. Nobody types a task id into Slack,
        # and this rule lives here rather than in the adapter because deciding
        # what a message MEANS is business logic (§5.1).
        #
        # An explicit reply_to_task_id above still wins: the dashboard names
        # the task it is answering, and inference must never override a caller
        # that was specific.
        clarifying = self._clarifying_task_in(row.conversation_id)
        if clarifying is not None:
            self.messages.set_reply_target(row.id, clarifying.id)
            row = self.messages.get_many([row.id])[0]
            return self._route_reply(row, promote=promote)

        verdict = self.relevance.judge(row)
```

and add the lookup beside `_task_conversation_id`:

```python
    def _clarifying_task_in(self, conversation_id: str) -> TaskRow | None:
        """The task in this conversation that is currently asking something.

        TaskRow carries no conversation_id — it is derived from the messages
        that formed it, the same lookup _task_conversation_id and app.py's
        answer_task already do. The scan is over tasks in NEEDS_CLARIFICATION
        only, which is by definition a small set: every one of them is blocked
        on a human.

        Most recently updated wins. Two parked tasks in one conversation is a
        real shape — the simulator's split request produces exactly that — so
        the tie-break has to be deterministic rather than whatever the database
        returned first, and the newest question is the one the human is
        answering.
        """
        candidates = [
            task
            for task in self.repo.list_by_state(TaskState.NEEDS_CLARIFICATION)
            if self._task_conversation_id(task) == conversation_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda t: (t.updated_at, t.id))
```

- [ ] **Step 5: Run the new tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_clarification_routing.py -v
```

Expected: PASS (8 passed).

- [ ] **Step 6: Run the FULL suite — this is the step this task exists to survive**

```bash
cd backend && python -m pytest -q
```

If anything fails, apply the rule at the top of this task: give the test's second message its own
`conversation_id` if it meant an independent request. **Record every test you touched and why in
your task report** — a behaviour change that silently edited existing tests is exactly what a
reviewer needs to see.

- [ ] **Step 7: Mutation-test the two halves of the rule**

1. Delete the `if row.reply_to_task_id:` short-circuit that runs BEFORE the new block →
   `test_an_explicit_reply_target_still_wins` must still pass (inference reaches the same task), so
   instead mutate `_clarifying_task_in` to ignore the conversation filter →
   `test_a_clarifying_task_in_another_conversation_is_not_answered` must fail. That filter is what
   stops a foreign message joining a task's source set.
2. Change `max(...)` to `min(...)` →
   `test_the_most_recently_updated_clarifying_task_is_the_one_answered` must fail.
3. Delete `self.messages.set_reply_target(...)` and pass the id directly to a modified
   `_route_reply` → `test_the_answer_is_recorded_as_a_reply_in_the_database` must fail. Restore.

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa/persistence/message_repository.py \
        backend/ley_khaa/orchestrator/orchestrator.py \
        backend/tests/test_clarification_routing.py
git commit -m "$(cat <<'EOF'
feat(intake): a reply in the thread answers the open question

Nobody types a task id into Slack. The rule lives in the orchestrator, not the
adapter — deciding what a message means is business logic (§5.1) — so a thread
reply and a dashboard answer take the same path and cannot drift. The target is
written to reply_to_task_id rather than passed along, so an inferred reply and
an explicit one are identical in storage.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: Start the adapters in the lifespan

**Files:**
- Modify: `backend/ley_khaa/adapters/supervisor.py` (add `build_adapters`)
- Modify: `backend/ley_khaa/api/app.py` (lifespan, `build_orchestrator`, two callables)
- Test: `backend/tests/test_adapter_startup.py`

**Interfaces:**
- Consumes: `AdapterSupervisor` (Task 9), `SlackAdapter` (Task 10), `DiscordAdapter` (Task 11),
  `ChannelNotifier`/`set_notifier`/`NullNotifier` (Tasks 6, 8), `DeadLetterRepository` (Task 2).
- Produces:
  - `build_adapters(*, ingest, dead_letter) -> list[ChannelAdapter]`.
  - `app.state.supervisor` — the running supervisor, or `None`.
  - `build_orchestrator(session)` now passes `notifier=current_notifier()`.

**`build_adapters` takes callables rather than a session factory** so `supervisor.py` never imports
`api/app.py` — that would be circular, since the lifespan is what calls it. It is also what keeps
§5.1 true: an adapter holds no business logic and never sees a repository.

**No tokens → no adapters → a fresh clone behaves precisely as it does today** (§3.4). That is what
keeps `docker compose up` a zero-account demo, and it is asserted, not assumed.

**§4's "startup logs exactly which channels are live"** is implemented here as well as in each
client: what the bot is listening to must always be *visible*, never inferred from config.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_adapter_startup.py`:

```python
import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from ley_khaa.adapters import notifier as notifier_module
from ley_khaa.adapters import supervisor as supervisor_module
from ley_khaa.adapters.base import Destination
from ley_khaa.adapters.notifier import NullNotifier, current_notifier
from ley_khaa.adapters.supervisor import build_adapters
from ley_khaa.api import app as app_module
from ley_khaa.config import settings as real_settings

NOOP = {"ingest": lambda raw: None, "dead_letter": lambda **kw: None}


def _pin(monkeypatch, **fields):
    """Settings is frozen (Phase 0 invariant). Pin it on every module that
    bound the object at import time."""
    patched = replace(real_settings, **fields)
    monkeypatch.setattr(supervisor_module, "settings", patched)
    monkeypatch.setattr(notifier_module, "settings", patched)
    monkeypatch.setattr(app_module, "settings", patched)
    return patched


def test_no_tokens_means_no_adapters(monkeypatch):
    """The whole zero-account demo rests on this line."""
    _pin(monkeypatch, slack_bot_token="", slack_app_token="", discord_bot_token="")
    assert build_adapters(**NOOP) == []


def test_a_half_configured_slack_does_not_start(monkeypatch):
    """Both tokens or nothing (spec §5): a bot token with no app token cannot
    open a Socket Mode connection, and starting anyway would crash-loop."""
    _pin(monkeypatch, slack_bot_token="xoxb-x", slack_app_token="", discord_bot_token="")
    assert build_adapters(**NOOP) == []


def test_slack_starts_when_both_tokens_are_present(monkeypatch):
    _pin(
        monkeypatch,
        slack_bot_token="xoxb-x",
        slack_app_token="xapp-x",
        slack_channels="C1, C2",
        discord_bot_token="",
    )
    adapters = build_adapters(**NOOP)
    assert [a.name for a in adapters] == ["slack"]
    assert adapters[0].allowed_channels == frozenset({"C1", "C2"})


def test_discord_starts_on_its_token_alone(monkeypatch):
    _pin(monkeypatch, slack_bot_token="", discord_bot_token="d", discord_channels="9")
    adapters = build_adapters(**NOOP)
    assert [a.name for a in adapters] == ["discord"]
    assert adapters[0].allowed_channels == frozenset({"9"})


def test_an_adapter_with_an_empty_allowlist_still_starts(monkeypatch):
    """Spec §5: it starts and ingests nothing, logging that plainly — the safe
    reading of an incomplete configuration."""
    _pin(monkeypatch, slack_bot_token="xoxb-x", slack_app_token="xapp-x", slack_channels="")
    adapters = build_adapters(**NOOP)
    assert [a.name for a in adapters] == ["slack"]
    assert adapters[0].allowed_channels == frozenset()


def test_a_token_free_startup_leaves_the_notifier_null(monkeypatch, session):
    """A fresh clone behaves precisely as it did before this phase."""
    _pin(monkeypatch, disable_startup=False, dispatch_mode="inline",
         slack_bot_token="", discord_bot_token="")
    monkeypatch.setattr(app_module, "run_migrations", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)

    with TestClient(app_module.app) as client:
        assert client.get("/health").status_code == 200
        assert app_module.app.state.supervisor is None
        assert isinstance(current_notifier(), NullNotifier)

    assert isinstance(current_notifier(), NullNotifier)


def test_a_configured_startup_runs_a_supervisor_and_installs_a_channel_notifier(
    monkeypatch, session
):
    started = []

    class StubAdapter:
        name = "slack"

        async def start(self):
            started.append(1)
            await asyncio.Event().wait()

        async def stop(self): ...

        async def notify(self, dest: Destination, text: str): ...

    _pin(monkeypatch, disable_startup=False, dispatch_mode="inline")
    monkeypatch.setattr(app_module, "run_migrations", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)
    monkeypatch.setattr(app_module, "build_adapters", lambda **kw: [StubAdapter()])

    with TestClient(app_module.app) as client:
        assert client.get("/health").status_code == 200
        supervisor = app_module.app.state.supervisor
        assert supervisor is not None
        assert set(supervisor.registry) == {"slack"}
        assert current_notifier().name == "channel"

    # Shutdown must put it back, or a later test in the same process inherits a
    # notifier pointing at a dead loop.
    assert isinstance(current_notifier(), NullNotifier)
    assert app_module.app.state.supervisor is None


def test_the_ingest_callable_reaches_the_orchestrator(monkeypatch, session):
    """What build_adapters hands an adapter must actually create work, or the
    whole chain is wired to nothing."""
    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)

    app_module._ingest_from_channel(
        {
            "source": "slack",
            "client": "T1",
            "conversation_id": "slack:T1:C1:1.0",
            "external_id": "slack:C1:1.0",
            "text": "compare the Bloomberg universe against FactSet as an Excel file",
        }
    )

    from ley_khaa.persistence.repository import TaskRepository

    assert TaskRepository(session).list()


def test_the_dead_letter_callable_writes_a_row(monkeypatch, session):
    from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository

    monkeypatch.setattr(app_module, "SessionLocal", lambda: session)
    app_module._record_dead_letter(
        source="slack", kind="inbound", reason="bad payload", payload={"token": "x"}
    )

    rows = DeadLetterRepository(session).list()
    assert len(rows) == 1
    assert "x" not in rows[0].payload
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_adapter_startup.py -v
```

Expected: FAIL — `ImportError: cannot import name 'build_adapters'`.

- [ ] **Step 3: Add `build_adapters`**

Append to `backend/ley_khaa/adapters/supervisor.py`, and add the imports it needs:

```python
from ..config import settings
from .base import channel_set
from .discord.client import DiscordAdapter
from .slack.client import SlackAdapter
```

```python
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
```

- [ ] **Step 4: Wire the lifespan**

In `backend/ley_khaa/api/app.py`, add the imports:

```python
from ..adapters.notifier import ChannelNotifier, NullNotifier, current_notifier, set_notifier
from ..adapters.supervisor import AdapterSupervisor, build_adapters
from ..persistence.dead_letter_repository import DeadLetterRepository
```

Pass the notifier into every orchestrator:

```python
def build_orchestrator(session: Session) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=build_llm(settings.llm_backend),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(settings.crystallizer_debounce_seconds),
        workflows=WorkflowRepository(session),
        memories=MemoryRepository(session),
        projects=ProjectRepository(session),
        # Whatever the lifespan installed — NullNotifier when no tokens are
        # set, which is every existing test and every fresh clone.
        notifier=current_notifier(),
    )
```

Add the two callables next to `_drive_task`:

```python
def _ingest_from_channel(raw: dict) -> None:
    """What an adapter's `ingest` is bound to.

    Its own session: this runs on a thread the adapter handed it to, not inside
    any request's unit of work — the same discipline Dispatcher._drive follows.
    """
    session = SessionLocal()
    try:
        build_orchestrator(session).ingest(raw)
    finally:
        session.close()


def _record_dead_letter(**kwargs) -> None:
    """What an adapter's `dead_letter` is bound to. Own session, same reason."""
    session = SessionLocal()
    try:
        DeadLetterRepository(session).record(**kwargs)
    finally:
        session.close()
```

In `lifespan`, add `app.state.supervisor = None` beside the other two in the `disable_startup`
branch, and after the dispatcher is created:

```python
    app.state.supervisor = None
    adapters = build_adapters(ingest=_ingest_from_channel, dead_letter=_record_dead_letter)
    if adapters:
        supervisor = AdapterSupervisor(adapters, session_factory=SessionLocal)
        await supervisor.start()
        # The loop is captured by start(); ChannelNotifier needs it because a
        # driver on a dispatcher worker thread has no running loop of its own.
        set_notifier(
            ChannelNotifier(
                supervisor.registry, loop=supervisor.loop, session_factory=SessionLocal
            )
        )
        app.state.supervisor = supervisor
```

and in the `finally` block, before the task cancellation:

```python
        if app.state.supervisor is not None:
            await app.state.supervisor.stop()
            app.state.supervisor = None
        # Put the holder back. Leaving a ChannelNotifier installed would point
        # a later run — or a later test in the same process — at a dead loop.
        set_notifier(NullNotifier())
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_adapter_startup.py -v
```

Expected: PASS (9 passed).

- [ ] **Step 6: Mutation-test the zero-account guarantee and the reset**

1. Make `build_adapters` always append a `SlackAdapter` → `test_no_tokens_means_no_adapters` and
   `test_a_token_free_startup_leaves_the_notifier_null` must both fail.
2. Drop the `and settings.slack_app_token` condition →
   `test_a_half_configured_slack_does_not_start` must fail.
3. Delete `set_notifier(NullNotifier())` from the shutdown path → the final assertion of
   `test_a_configured_startup_runs_a_supervisor_and_installs_a_channel_notifier` must fail. This
   one also protects every test that runs after it in the same process.

- [ ] **Step 7: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/ley_khaa/adapters/supervisor.py backend/ley_khaa/api/app.py backend/tests/test_adapter_startup.py
git commit -m "$(cat <<'EOF'
feat(startup): start the adapters whose tokens are present

No tokens, no adapters, and a fresh clone behaves precisely as it did before
this phase — the zero-account demo is asserted, not assumed. Slack needs both
tokens or neither: starting on one would crash-loop through the backoff while
looking configured. Startup logs exactly which channels are live, and shutdown
puts the notifier holder back so nothing inherits a dead loop.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 14: The loop, offline — the phase's headline claim as one test

**Files:**
- Test: `backend/tests/test_channel_loop.py`

**Interfaces:**
- Consumes: everything built so far. Produces no new code.

**This is the claim §1 states so a test can settle it:**

> A message posted in an allowlisted channel becomes a task in the right project; the bot asks its
> clarifying question back in that thread; a reply in the thread answers it; and none of the bot's
> own messages are ever ingested as new work.

**This task adds no production code.** It drives the real `translate` functions, the real
`Orchestrator`, the real `TaskDriver` and the real offline `HeuristicLLM` through a `FakeAdapter`
and a `RecordingNotifier`, with no network and no tokens — the third instance of a seam this
codebase has proven twice (`HeuristicLLM`, `SubprocessSandbox`).

**If a test here fails, the defect is in an earlier task, not in this one.** Fix it there, add the
regression test there, and say so in your report. Do not weaken an assertion in this file to make it
pass: this file is the phase's definition of done in executable form.

**Use the real translators, not hand-written dicts.** A test that fabricates the intake dict itself
would pass even if `translate` produced something the pipeline rejects — the exact "passes for the
wrong reason" shape Phase 4 hit eight times.

- [ ] **Step 1: Write the test file**

Create `backend/tests/test_channel_loop.py`:

```python
"""The claim of spec §1, offline: no network, no tokens, real everything else."""
import json
from pathlib import Path

import pytest

from ley_khaa.adapters.notifier import RecordingNotifier
from ley_khaa.adapters.slack.translate import conversation_parts, translate
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.project_repository import ProjectRepository
from ley_khaa.persistence.repository import TaskRepository

PAYLOADS = Path(__file__).resolve().parent / "fixtures" / "payloads"
ALLOWED = frozenset({"C0ALLOWED1"})
BOT = "U0BOT0001"


class FakeAdapter:
    """A channel that exists only in memory.

    It uses the REAL translate(), so a defect in the allowlist, the
    self-message filter or thread derivation shows up here as a failure of the
    loop rather than being hidden behind a hand-written dict.
    """

    name = "slack"

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator
        self.ingested: list[dict] = []
        self.dropped: list[dict] = []

    def deliver(self, payload: dict):
        raw = translate(payload, allowed_channels=ALLOWED, bot_user_id=BOT)
        if raw is None:
            self.dropped.append(payload)
            return None
        self.ingested.append(raw)
        return self.orchestrator.ingest(raw)


@pytest.fixture
def channel(session):
    notifier = RecordingNotifier()
    projects = ProjectRepository(session)
    projects.create("default", description="")
    projects.create("markets", display_name="Markets", description="index and universe work")
    # A binding, so routing is deterministic offline: the heuristic LLM makes no
    # stage-2 project call, and a test that depended on one would be asserting
    # on the stand-in rather than on routing.
    projects.bind("slack", "T0SYNTH01", "", "markets", stage="seed")

    orchestrator = Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
        projects=projects,
        notifier=notifier,
    )
    return FakeAdapter(orchestrator), notifier, session


def _payload(name: str) -> dict:
    return json.loads((PAYLOADS / f"{name}.json").read_text())


def test_a_channel_message_becomes_a_task_in_the_bound_project(channel):
    adapter, _notifier, session = channel

    result = adapter.deliver(_payload("slack_channel_message"))

    assert result.task_ids
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.project == "markets"


def test_the_bot_asks_its_question_in_the_originating_thread(channel):
    adapter, notifier, session = channel

    result = adapter.deliver(_payload("slack_channel_message"))
    task = TaskRepository(session).get(result.task_ids[0])
    if task.state != TaskState.NEEDS_CLARIFICATION.value:
        pytest.skip(
            "this fixture's request is fully specified; the clarification loop "
            "is covered by test_a_thread_reply_answers_the_question below"
        )

    assert notifier.sent, "a parked task told nobody"
    dest, text = notifier.sent[-1]
    _team, channel_id, thread_ts = conversation_parts(dest.conversation_id)
    assert channel_id == "C0ALLOWED1"
    assert thread_ts == "1756600000.000100", "the answer must land in the request's own thread"
    assert task.open_question in text


def test_a_thread_reply_answers_the_question_and_the_task_resumes(channel):
    """The whole loop: a request that cannot be started, a question in the
    thread, an ordinary reply in that thread, and work that moves on."""
    adapter, _notifier, session = channel
    repo = TaskRepository(session)

    incomplete = _payload("slack_channel_message")
    incomplete["event"]["text"] = "compare the holdings against the portfolio"
    result = adapter.deliver(incomplete)
    task = repo.get(result.task_ids[0])
    assert task.state == TaskState.NEEDS_CLARIFICATION.value

    reply = adapter.deliver(_payload("slack_thread_reply"))

    assert reply.replied_to_task_id == task.id
    assert repo.get(task.id).state == TaskState.AWAITING_APPROVAL.value


def test_the_bots_own_notification_is_never_ingested(channel):
    """Without the self-message filter the bot posts into the channel it reads
    and the system feeds itself without limit. This asserts on the OUTCOME —
    no message, no candidate, no task — not merely on translate() returning
    None, which test_slack_translate.py already covers."""
    adapter, _notifier, session = channel
    adapter.deliver(_payload("slack_channel_message"))
    tasks_before = len(TaskRepository(session).list())
    messages_before = len(MessageRepository(session).list_for_conversation(
        "slack:T0SYNTH01:C0ALLOWED1:1756600000.000100"
    ))

    assert adapter.deliver(_payload("slack_bot_message")) is None

    assert len(TaskRepository(session).list()) == tasks_before
    assert (
        len(
            MessageRepository(session).list_for_conversation(
                "slack:T0SYNTH01:C0ALLOWED1:1756600000.000100"
            )
        )
        == messages_before
    ), "the bot's own message reached storage"


def test_a_message_from_an_unlisted_channel_is_provably_not_persisted(channel):
    """Spec §8. Asserted on the database, not on the return value: the
    allowlist's promise is that nothing is stored, and only the store can say."""
    adapter, _notifier, session = channel
    payload = _payload("slack_channel_message")
    payload["event"]["channel"] = "C0NOTLISTED"

    assert adapter.deliver(payload) is None

    assert MessageRepository(session).list_for_conversation(
        "slack:T0SYNTH01:C0NOTLISTED:1756600000.000100"
    ) == []
    assert TaskRepository(session).list() == []


def test_redelivery_of_the_same_message_creates_no_second_task(channel):
    """Slack and Discord both redeliver on timeout. MessageRepository.add
    dedupes on external_id, and this is the test that the adapter's namespaced
    key actually reaches it."""
    adapter, _notifier, session = channel
    payload = _payload("slack_channel_message")

    first = adapter.deliver(payload)
    second = adapter.deliver(payload)

    assert first.message_id == second.message_id
    assert len(TaskRepository(session).list()) == 1


def test_the_whole_loop_makes_no_network_call(channel, monkeypatch):
    """The offline claim, enforced rather than asserted in prose: any socket
    opened anywhere in this flow fails the test."""
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("the offline loop opened a socket")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    adapter, _notifier, _session = channel

    adapter.deliver(_payload("slack_channel_message"))
    adapter.deliver(_payload("slack_thread_reply"))
```

- [ ] **Step 2: Run it**

```bash
cd backend && python -m pytest tests/test_channel_loop.py -v
```

Expected: PASS (7 passed), with at most the one conditional `skip` in
`test_the_bot_asks_its_question_in_the_originating_thread` — and **if that skip fires, remove it**
by choosing a fixture text that genuinely parks, because the baseline bar for this project is
**0 skipped**. The `test_a_thread_reply_answers_the_question_and_the_task_resumes` test already
shows how: override `event["text"]` with a request that names no output format.

If any test FAILS, the defect is in an earlier task. Find it, fix it there with its own regression
test, and record both in your task report.

- [ ] **Step 3: Mutation-test the loop from the outside**

These prove the integration, not the units, so each one must be mutated in the module it belongs to
and then restored:

1. In `slack/translate.py`, delete the `bot_id` filter →
   `test_the_bots_own_notification_is_never_ingested` must fail on the message count. This is the
   run-away-feedback defect, caught at the level where it would actually hurt.
2. In `slack/translate.py`, delete the allowlist check →
   `test_a_message_from_an_unlisted_channel_is_provably_not_persisted` must fail on a persisted
   message.
3. In `orchestrator.py`, delete the `_clarifying_task_in` block →
   `test_a_thread_reply_answers_the_question_and_the_task_resumes` must fail because the task is
   still in `needs_clarification`.
4. In `slack/translate.py`, drop the `external_id` field entirely →
   `test_redelivery_of_the_same_message_creates_no_second_task` must fail with two tasks.

- [ ] **Step 4: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/tests/test_channel_loop.py
git commit -m "$(cat <<'EOF'
test(channels): prove the phase's claim offline, end to end

A message in an allowlisted channel becomes a task in the bound project, the
question comes back in that thread, an ordinary reply answers it, the bot's own
messages never reach storage, an unlisted channel is provably not persisted,
and a redelivery makes no second task — through the REAL translators and the
real driver, with a socket-opening guard so "offline" is enforced rather than
claimed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 15: `GET /dead-letters`

**Files:**
- Modify: `backend/ley_khaa/api/schemas.py` (add `DeadLetterOut`)
- Modify: `backend/ley_khaa/api/app.py` (add the route)
- Test: `backend/tests/test_dead_letters_api.py`

**Interfaces:**
- Consumes: `DeadLetterRepository` (Task 2).
- Produces: `GET /dead-letters?limit=` → `list[DeadLetterOut]` with fields
  `id, source, kind, reason, payload, created_at`.

§7 asks for "dead-letter + surface in UI". This is the first half; Task 16 is the second.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_dead_letters_api.py`:

```python
from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository


def test_an_empty_dead_letter_list_is_an_empty_array(client):
    response = client.get("/dead-letters")
    assert response.status_code == 200
    assert response.json() == []


def test_dead_letters_are_listed_newest_first(client, session):
    repo = DeadLetterRepository(session)
    repo.record(source="slack", kind="inbound", reason="first")
    repo.record(source="discord", kind="outbound", reason="second")

    body = client.get("/dead-letters").json()

    assert [row["reason"] for row in body] == ["second", "first"]
    assert body[0]["source"] == "discord"
    assert body[0]["kind"] == "outbound"
    assert body[0]["created_at"]


def test_the_limit_is_honoured(client, session):
    repo = DeadLetterRepository(session)
    for i in range(5):
        repo.record(source="slack", kind="inbound", reason=f"r{i}")

    assert len(client.get("/dead-letters?limit=2").json()) == 2


def test_a_nonsense_limit_is_a_422_not_a_500(client):
    assert client.get("/dead-letters?limit=0").status_code == 422
    assert client.get("/dead-letters?limit=-1").status_code == 422


def test_the_endpoint_returns_no_secrets(client, session):
    """The route serves whatever is stored, so the guarantee has to hold at the
    write. Asserted HERE as well as in test_dead_letters.py because this is the
    surface a browser reaches, and §4 says tokens are never returned by an API."""
    DeadLetterRepository(session).record(
        source="slack",
        kind="inbound",
        reason="bad envelope",
        payload={"token": "xoxb-super-secret", "event": {"text": "hi"}},
    )

    body = client.get("/dead-letters").json()

    assert "xoxb-super-secret" not in body[0]["payload"]
    assert "[redacted]" in body[0]["payload"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_dead_letters_api.py -v
```

Expected: FAIL — 404 on `/dead-letters`.

- [ ] **Step 3: Add the schema**

In `backend/ley_khaa/api/schemas.py`, after `TriageOut`:

```python
class DeadLetterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    # "inbound" | "outbound" | "connection"
    kind: str
    reason: str
    # Already redacted at the write (DeadLetterRepository.redact) — §4: tokens
    # are never returned by an API.
    payload: str
    created_at: datetime
```

- [ ] **Step 4: Add the route**

In `backend/ley_khaa/api/app.py`, add `DeadLetterOut` to the schema imports and add the route
after `list_triage`:

```python
@app.get("/dead-letters", response_model=list[DeadLetterOut])
def list_dead_letters(
    limit: int = Query(default=100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[DeadLetterOut]:
    """Every inbound message, notification and connection that was dropped (§3.8).

    A dropped message with no visible trace is the failure this exists to
    prevent, so this is a plain listing with no filtering: whatever went wrong
    is on the first page.
    """
    return [
        DeadLetterOut.model_validate(row)
        for row in DeadLetterRepository(session).list(limit=limit)
    ]
```

Add `Query` to the FastAPI import: `from fastapi import Depends, FastAPI, HTTPException, Query`.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_dead_letters_api.py -v
```

Expected: PASS (5 passed).

- [ ] **Step 6: Mutation-test the bound**

Remove `ge=1` from the `Query` → `test_a_nonsense_limit_is_a_422_not_a_500` must fail with a 200.

- [ ] **Step 7: Run the full suite and commit**

```bash
cd backend && python -m pytest -q
git add backend/ley_khaa/api/schemas.py backend/ley_khaa/api/app.py backend/tests/test_dead_letters_api.py
git commit -m "$(cat <<'EOF'
feat(api): surface dead letters

A dropped message with no visible trace is the worst failure mode an intake
system can have (§3.8), so this is a plain listing with no filtering: whatever
went wrong is on the first page. The payload is redacted at the write, and it
is asserted here too because this is the surface a browser reaches.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 16: The dead-letter panel in the dashboard

**Files:**
- Modify: `frontend/src/api.ts` (`DeadLetter` type, `fetchDeadLetters`)
- Create: `frontend/src/DeadLetters.tsx`
- Create: `frontend/src/DeadLetters.test.tsx`
- Modify: `frontend/src/App.tsx` (render the panel)
- Modify: `frontend/src/App.test.tsx` (route `/dead-letters` in the fetch stub)

**Interfaces:**
- Consumes: `GET /dead-letters` (Task 15).
- Produces: `DeadLetter` type; `fetchDeadLetters(limit?: number): Promise<DeadLetter[]>`;
  `<DeadLetters />`.

**Follow `Projects.tsx`.** It is the closest existing panel: a `useCallback` loader, a 3s poll, an
error line, and no props. Do NOT follow `Candidates.tsx` — it takes its items as props and does not
fetch at all.

**⚠ `App.test.tsx`'s `stubApi` has a catch-all** that returns `[task()]` for any unmatched URL, and
the file already carries a comment about a bug that caused. Adding `<DeadLetters />` to `App`
without adding a `/dead-letters` branch would feed task objects to this panel. Add the branch
**before** the catch-all.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/DeadLetters.test.tsx`:

```tsx
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import DeadLetters from "./DeadLetters";
import type { DeadLetter } from "./api";

const letter = (overrides: Partial<DeadLetter> = {}): DeadLetter => ({
  id: "dl1",
  source: "slack",
  kind: "inbound",
  reason: "unparsable Slack ts 'nope'",
  payload: '{"event": {"text": "hi"}}',
  created_at: "2026-08-31T09:00:00+00:00",
  ...overrides,
});

// Drives the real api layer through fetch rather than mocking api.ts, the same
// shape Triage.test.tsx uses: only this can prove fetchDeadLetters actually
// reaches the right URL.
function stub(rows: DeadLetter[], ok = true) {
  const mock = vi.fn(async () => ({
    ok,
    status: ok ? 200 : 500,
    json: async () => rows,
  }));
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(cleanup);

test("renders nothing at all when there are no dead letters", async () => {
  stub([]);
  const { container } = render(<DeadLetters />);
  // An empty panel with a heading is a permanent scar on a dashboard that is
  // usually healthy. Nothing wrong, nothing shown.
  await waitFor(() => expect(container.textContent).toBe(""));
});

test("shows the source, the kind and the reason", async () => {
  stub([letter()]);
  render(<DeadLetters />);

  expect(await screen.findByText(/unparsable Slack ts/)).toBeTruthy();
  expect(screen.getByText(/slack/)).toBeTruthy();
  expect(screen.getByText(/inbound/)).toBeTruthy();
});

test("fetches from /dead-letters", async () => {
  const mock = stub([letter()]);
  render(<DeadLetters />);

  await waitFor(() => expect(mock).toHaveBeenCalled());
  expect(String(mock.mock.calls[0][0])).toContain("/dead-letters");
});

test("shows a failure to load rather than an empty panel", async () => {
  stub([], false);
  render(<DeadLetters />);

  expect(await screen.findByText(/fetchDeadLetters failed/)).toBeTruthy();
});

test("lists every dead letter it is given", async () => {
  stub([letter(), letter({ id: "dl2", source: "discord", reason: "delivery failed: 403" })]);
  render(<DeadLetters />);

  expect(await screen.findByText(/unparsable Slack ts/)).toBeTruthy();
  expect(screen.getByText(/delivery failed: 403/)).toBeTruthy();
});
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd frontend && npm test -- DeadLetters
```

Expected: FAIL — cannot resolve `./DeadLetters`.

- [ ] **Step 3: Add the API layer**

In `frontend/src/api.ts`, after the `TriageItem` type:

```ts
export type DeadLetter = {
  id: string;
  source: string;
  // "inbound" | "outbound" | "connection"
  kind: string;
  reason: string;
  // Redacted server-side before storage — never contains a token.
  payload: string;
  created_at: string;
};

export async function fetchDeadLetters(limit = 50): Promise<DeadLetter[]> {
  const res = await fetch(`${BASE}/dead-letters?limit=${limit}`);
  if (!res.ok) throw new Error(`fetchDeadLetters failed: ${res.status}`);
  return res.json();
}
```

- [ ] **Step 4: Write the panel**

Create `frontend/src/DeadLetters.tsx`:

```tsx
import { useCallback, useEffect, useState } from "react";
import { fetchDeadLetters, type DeadLetter } from "./api";

// A dropped message with no visible trace is the failure dead letters exist to
// prevent (spec §3.8), so this panel is loud when there is something and
// absent when there is not — an empty "Dead letters" heading on a dashboard
// that is usually healthy is a permanent scar people learn to ignore.
export default function DeadLetters() {
  const [rows, setRows] = useState<DeadLetter[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      fetchDeadLetters()
        .then((r) => {
          setRows(r);
          setError(null);
        })
        .catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  if (error) return <p className="text-red-600 text-sm">{error}</p>;
  if (rows.length === 0) return null;

  return (
    <section>
      <h2 className="text-lg font-semibold mb-2 mt-8 text-red-700">
        Dead letters ({rows.length})
      </h2>
      <ul className="space-y-2">
        {rows.map((row) => (
          <li key={row.id} className="rounded border border-red-200 bg-red-50 p-3">
            <div className="flex items-baseline justify-between">
              <span className="font-medium">{row.reason}</span>
              <span className="text-xs text-gray-500">
                {row.source} · {row.kind}
              </span>
            </div>
            <p className="mt-1 text-xs text-gray-600">
              {new Date(row.created_at).toLocaleString()}
            </p>
            {row.payload && (
              <pre className="mt-2 overflow-x-auto rounded bg-white p-2 text-xs text-gray-700">
                {row.payload}
              </pre>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
```

- [ ] **Step 5: Render it in `App.tsx`**

Add the import beside the others and the element directly after `<Triage />` — a drop is more
urgent than a queue and belongs above the task list:

```tsx
import DeadLetters from "./DeadLetters";
```

```tsx
      <Triage />

      <DeadLetters />
```

- [ ] **Step 6: Route `/dead-letters` in `App.test.tsx`'s stub**

In `stubApi`, add the branch **before** the catch-all that returns `[task()]`:

```tsx
      const body = u.includes("/dead-letters")
        ? []
        : u.includes("/candidates")
          ? candidates
          : u.includes("/registry")
            ? workflows
            : u.includes("/projects")
              ? projects
              : u.includes("/triage")
                ? triage
                : [task()];
```

**Ordering matters and is not cosmetic:** without this branch, `/dead-letters` falls through to
`[task()]` and the panel renders task objects as dead letters — the same catch-all bug the comment
above `stubApi` already records.

- [ ] **Step 7: Run the tests and the typecheck**

```bash
cd frontend && npm test && npx tsc --noEmit -p tsconfig.json && npm run typecheck
```

Expected: 54 tests passing (49 baseline + 5 new), tsc silent.

- [ ] **Step 8: Mutation-test the two behaviours worth guarding**

1. Change `if (rows.length === 0) return null;` to always render the section →
   `renders nothing at all when there are no dead letters` must fail on non-empty text content.
2. Change `fetchDeadLetters` to swallow a non-ok response and return `[]` →
   `shows a failure to load rather than an empty panel` must fail. A monitoring panel that shows an
   empty list when it could not load is worse than one that shows nothing.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/api.ts frontend/src/DeadLetters.tsx frontend/src/DeadLetters.test.tsx \
        frontend/src/App.tsx frontend/src/App.test.tsx
git commit -m "$(cat <<'EOF'
feat(dashboard): surface dead letters

Loud when there is something, absent when there is not: an empty heading on a
dashboard that is usually healthy becomes a scar people learn to ignore. A
failure to load shows the failure rather than an empty list, which would say
"nothing was dropped" when the truth is "we could not tell".

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 17: Documentation, compose passthrough, and the honest limits

**Files:**
- Modify: `README.md` (a new `### Channels` section under `## Run`)
- Modify: `CHANGELOG.md` (a `## [0.7.0]` entry)
- Modify: `docs/GETTING_STARTED.md` (a new section 5.5 and an edit to section 9)
- Modify: `docker-compose.yml` (pass the five variables through)
- Modify: `docs/superpowers/specs/2026-08-28-phase-5-backlog.md` (one new item)

**Interfaces:** none. This task ships no code.

**The rule that scopes it, and it is the project's own:** **fix statements that are FALSE.** That
class has been fixed five separate times across Phases 4 and 5, and a false line in a doc a reader
acts on is the version with teeth. Three specific statements this phase makes false or newly
checkable:

1. `intake/gateway.py`'s docstring says *"simulator now; Slack/Discord later"*. It is now wrong.
2. `docs/GETTING_STARTED.md` §9 "What is not built yet" lists Slack/Discord adapters. Two of the
   three §11 items are still missing (vision intake, the Ollama fallback); the channel line goes.
3. **The gap Task 7 created:** `Dispatcher._fail_poison` moves a task to FAILED without a
   notification, so "`failed` notifies" is TRUE for every path except an abandoned lease. Say so,
   and file it.

- [ ] **Step 1: Correct the gateway docstring**

In `backend/ley_khaa/intake/gateway.py`, the class docstring's parenthetical becomes:

```python
    """Normalizes any inbound payload to a canonical Message and persists it.

    Adapters (the simulator, Slack and Discord) hand raw dicts to this one
    door. Images are stored, never interpreted here (spec §5.2).
    """
```

- [ ] **Step 2: Pass the variables through compose**

In `docker-compose.yml`, under `backend.environment`:

```yaml
      # Channel adapters (spec §5). Unset -> that adapter does not start, which
      # is what keeps `docker compose up` a zero-account demo. Point these at a
      # SCRATCH workspace and server only: the synthetic-data commitment does
      # not survive being aimed at anything work-adjacent.
      LEY_KHAA_SLACK_BOT_TOKEN: ${LEY_KHAA_SLACK_BOT_TOKEN:-}
      LEY_KHAA_SLACK_APP_TOKEN: ${LEY_KHAA_SLACK_APP_TOKEN:-}
      LEY_KHAA_SLACK_CHANNELS: ${LEY_KHAA_SLACK_CHANNELS:-}
      LEY_KHAA_DISCORD_BOT_TOKEN: ${LEY_KHAA_DISCORD_BOT_TOKEN:-}
      LEY_KHAA_DISCORD_CHANNELS: ${LEY_KHAA_DISCORD_CHANNELS:-}
```

- [ ] **Step 3: Add the README section**

In `README.md`, add a `### Channels` section after `### Amendments` and before `### Local dev (no
Docker)`:

```markdown
### Channels

ley-khaa can read and answer in a real Slack or Discord channel. Both adapters dial **out** (Slack
Socket Mode, Discord Gateway), so there is no public URL, no tunnel and no inbound port — they run
as supervised tasks inside the backend beside the dispatcher.

**With no tokens set, no adapters start and nothing changes.** `docker compose up` stays a
zero-account demo.

| Variable | Meaning |
|---|---|
| `LEY_KHAA_SLACK_BOT_TOKEN`, `LEY_KHAA_SLACK_APP_TOKEN` | Slack. **Both** or the adapter does not start. |
| `LEY_KHAA_SLACK_CHANNELS` | comma-separated channel ids the bot may read |
| `LEY_KHAA_DISCORD_BOT_TOKEN` | Discord |
| `LEY_KHAA_DISCORD_CHANNELS` | comma-separated channel ids |

**Use a scratch workspace.** Point this at a Slack workspace or Discord server you created for it,
and never at anything work-adjacent. The project's synthetic-data commitment does not survive being
aimed at a real channel.

**The allowlist is the boundary.** The bot ignores every message from a channel not named in
configuration, and that check runs before anything is persisted — being invited to a channel is not
consent to ingest it. An adapter with a token and an *empty* allowlist starts and ingests nothing,
logging that plainly; startup always logs exactly which channels are live.

What the channel is for, and what it is not:

- **It is an inbox and a reply surface.** A message becomes a task; a clarifying question comes back
  in that thread; an ordinary reply in the thread answers it. `done` and `failed` report back.
- **It is not a control panel.** Approve, reject and mode override stay in the dashboard, because
  approval releases work to run unattended and a channel has no notion of who may do that.
- **The bot never ingests its own messages**, so a notification cannot become a new request.
- **Notification is best-effort.** A failed send is dead-lettered and shown in the dashboard's
  Dead letters panel; it never fails the task.
- **Threads only.** DMs are not ingested in this release.
```

- [ ] **Step 4: Add the CHANGELOG entry**

In `CHANGELOG.md`, under `## [Unreleased]`, add:

```markdown
## [0.7.0] — 2026-08-31

### Added
- Real Slack and Discord channel adapters (§5.1), ingesting **and** notifying. Each adapter is split
  in two: a pure `translate.py` (platform event → the raw dict `IntakeGateway.accept()` already
  takes) holding the allowlist, the self-message filter, thread derivation and the dedupe key, and a
  thin `client.py` holding the socket and no decisions. Both platforms dial out — Slack Socket Mode,
  Discord Gateway — so there is no public URL, no tunnel and no inbound port, and adapters run as
  supervised asyncio tasks in the FastAPI lifespan beside the Phase 5 dispatcher. `docker compose up`
  is still one command.
- An explicit **channel allowlist** (`LEY_KHAA_SLACK_CHANNELS`, `LEY_KHAA_DISCORD_CHANNELS`),
  enforced before anything is persisted. Empty means ingest nothing, never everything. Startup logs
  exactly which channels are live.
- **Notification** as a `Notifier` seam injected into `TaskDriver` — the same shape as `LLMClient`
  and `SandboxRunner`, with `NullNotifier` as the default, so every existing test and every
  token-free run is unchanged. Exactly four states speak: `needs_clarification` (the question),
  `awaiting_approval` (the effective mode and its reason), `done` (the bundle path) and `failed`
  (the reason). `tasks.last_notified_state` plus a compare-and-swap (`TaskRepository.mark_notified`)
  is what stops a re-entrant `advance()` repeating a question every pass.
- **A reply in the thread answers the question** (§3.7). The rule lives in `Orchestrator.ingest`,
  not in an adapter — deciding what a message means is business logic — so a Slack thread reply and
  a dashboard answer take the identical path and are identical rows in storage.
- **Dead letters** (§3.8): a `dead_letters` table, `GET /dead-letters`, and a dashboard panel that
  is loud when there is something and absent when there is not. Written on a failed translation, a
  failed notification and an adapter crash. Payloads are redacted at the write — Slack's own Socket
  Mode envelope carries a `token` field.
- `Simulator` now satisfies `ChannelAdapter`, so the protocol has three implementations rather than
  being shaped around Slack and bolted onto the others.

### Changed
- `AdapterSupervisor` restarts a crashed adapter with capped exponential backoff, dead-letters the
  crash, and never lets it reach the API or the dispatcher. Cancellation is shutdown, not a crash.
- Notification is fire-and-forget across the sync/async boundary: `TaskDriver.advance()` is
  synchronous and runs on a dispatcher worker thread, so `ChannelNotifier` hands its coroutine to
  the loop captured at lifespan start and does not wait — a wedged platform API cannot extend a
  task's execution time. Under `LEY_KHAA_DISPATCH=inline` a notification is dead-lettered rather
  than delivered.

### Dependencies
- `slack_sdk==3.44.0`, `discord.py==2.7.1`, backend image only. The sandbox image is untouched.

### Known limits
- Approve, reject and mode override are dashboard actions; there are no interactive buttons, so a
  phone-only workflow is not possible.
- Notification is best-effort with dead-lettering, not a durable outbox.
- A task abandoned past `max_lease_attempts` is failed by the dispatcher, which has no notifier —
  so that one failure path does not notify. Tracked as backlog item 15.
- Attachments are carried, not understood; images from a channel are stored, not read.
- One workspace per platform, and threads only — no DMs.
```

- [ ] **Step 5: Update GETTING_STARTED**

Add a section between §5 and §6:

````markdown
## 5.5 Connecting a real Slack or Discord channel (optional)

**Create a scratch workspace or server for this.** Never point it at anything work-adjacent — the
project's synthetic-data commitment does not survive a real channel.

**Slack**

1. Create an app at api.slack.com/apps → *From scratch*.
2. **Socket Mode** → enable it. That generates the **app-level token** (`xapp-…`) —
   `LEY_KHAA_SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → bot scopes `channels:history`, `chat:write`. Install to the workspace;
   the **bot token** (`xoxb-…`) is `LEY_KHAA_SLACK_BOT_TOKEN`.
4. **Event Subscriptions** → subscribe to `message.channels`.
5. Invite the bot to a channel, copy the channel id (right-click → *Copy link*; it is the `C…` part)
   into `LEY_KHAA_SLACK_CHANNELS`.

**Discord**

1. Create an application at discord.com/developers → *Bot*. The token is
   `LEY_KHAA_DISCORD_BOT_TOKEN`.
2. **Enable the MESSAGE CONTENT INTENT.** It is privileged and off by default. Without it every
   message arrives with empty content, the bot looks connected, and it silently ingests nothing.
3. Invite the bot with the `bot` scope and *Send Messages* / *Read Message History*.
4. Turn on Developer Mode in Discord, right-click the channel → *Copy Channel ID*, into
   `LEY_KHAA_DISCORD_CHANNELS`.

Then:

```bash
export LEY_KHAA_SLACK_BOT_TOKEN=xoxb-…
export LEY_KHAA_SLACK_APP_TOKEN=xapp-…
export LEY_KHAA_SLACK_CHANNELS=C0123456789
docker compose up
```

The startup log names every channel it is listening to. Post a request in that channel; watch the
task appear in the dashboard, answer the bot's question **in the thread**, and approve it in the
dashboard — approval stays there on purpose, because it releases work to run unattended.

If nothing happens, check the **Dead letters** panel first: a dropped message leaves a trace there
with the reason.
````

And in §9 "What is not built yet", **remove the Slack/Discord line** and leave the two §11 items
that are still genuinely missing (vision intake and the Ollama fallback), plus the known limits from
the CHANGELOG above.

- [ ] **Step 6: File the backlog item**

In `docs/superpowers/specs/2026-08-28-phase-5-backlog.md`, add, in the file's existing item format:

```markdown
## 15. A poisoned task fails without telling anyone

**What is broken.** `Dispatcher._fail_poison` moves a task to FAILED when it has outlived
`max_lease_attempts` workers. It does that with a bare `TaskRepository`, and the notifier lives on
`TaskDriver` — so this is the one path to FAILED that does not notify. Everything else does
(`advance()`'s single exit point, and `reject()` explicitly). §8's "`done` and `failed` notify" is
therefore true of every path a human is likely to hit and false of exactly this one, which is
recorded in 0.7.0's known limits rather than left to be discovered.

**Shape of the fix.** Inject an `announce: Callable[[Session, str], None]` into `Dispatcher`, bound
in `api/app.py` to something that builds the orchestrator for that session and calls
`driver._announce(repo.get(task_id))`. That keeps the dispatcher ignorant of the driver, the
notifier and FastAPI, which is why it is not simply given a `TaskDriver`.

**Why it was deferred.** Phase 6 widened the review surface more than any phase so far — first
outside world, first credentials — and this adds a constructor parameter to the one component whose
concurrency correctness the whole queue rests on.
```

- [ ] **Step 7: Verify every claim you just wrote**

Run each of these and confirm the doc matches:

```bash
cd backend && python -m pytest -q                      # the counts in the CHANGELOG
grep -rn "simulator now" ley_khaa/                     # must be empty
grep -rn "Slack" ../docs/GETTING_STARTED.md            # §9 must no longer list it as missing
grep -c "LEY_KHAA_SLACK_BOT_TOKEN" ../docker-compose.yml
cd ../frontend && npm test && npm run typecheck
```

Every table in the README's Channels section must name variables that exist in `config.py`. A
config table listing a variable nothing reads is the false-statement class in its most quietly
damaging form.

- [ ] **Step 8: Commit**

```bash
git add README.md CHANGELOG.md docs/GETTING_STARTED.md docker-compose.yml \
        docs/superpowers/specs/2026-08-28-phase-5-backlog.md backend/ley_khaa/intake/gateway.py
git commit -m "$(cat <<'EOF'
docs: document the channel adapters and their honest limits

Corrects the gateway docstring's "Slack/Discord later", removes the
now-false line in GETTING_STARTED's "not built yet", and records the one
failure path that does not notify — a task the dispatcher fails after outliving
its leases — as a known limit and backlog item 15 rather than leaving §8's
claim quietly overstated.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification, before the whole-branch review

Not a task — the gate every phase in this project has passed through before its PR.

- [ ] Full backend suite: `cd backend && python -m pytest -q` → **0 failures, 0 skipped, 0 warnings.**
- [ ] The 9 `[docker]` params against a real image:
      `mkdir -p "$HOME/tmp" && TMPDIR="$HOME/tmp" python -m pytest -m docker -v` → 9 passed, 0
      skipped. (Colima mounts only `$HOME`; without `TMPDIR` these fail misleadingly.)
- [ ] Frontend: `cd frontend && npm test && npm run typecheck` → 54 passing, tsc silent.
- [ ] `python -m pytest tests/test_migrations.py -v` → the drift guard is green with
      `compare_server_default` on.
- [ ] `git grep -n "xoxb\|xapp\|Bot " -- backend/ley_khaa` → no token literal anywhere in source.
- [ ] Every §8 line in the spec has a test that would fail if it stopped being true. Walk the list
      and name the test for each in the PR body.
- [ ] **Whole-branch review on Opus.** Three phases running, this has found what per-task reviews
      structurally cannot — a symlinked deliverable, a root-running sandbox, an unbounded prompt, an
      enum member added to one set and not its sibling. Expect it to find something; the per-task
      reviews are not a substitute.

---

## Self-review of this plan

Run against the spec after writing, before execution.

**Spec coverage.** §2 decisions 1–7: both platforms ingest+notify (Tasks 4, 5, 10, 11); in-process
outbound WebSocket (10, 11, 13); inbox not control panel (17 documents it; no approve/reject path is
added anywhere); allowlist (4, 5, 9); `Notifier` seam in the driver (6, 7, 8); dead-letters
persisted and surfaced (1, 2, 15, 16); Ollama excluded (nothing added). §3.2 split (4, 5, 10, 11);
§3.3 components incl. the Simulator retrofit (3); §3.4 supervision (9, 13); §3.5 ingest flow (4, 5);
§3.6 notify flow, policy, re-notification guard, sync/async boundary, inline fallback (6, 7, 8);
§3.7 clarification replies (12); §3.8 dead-letters (1, 2, 15, 16); §4 secrets and startup logging
(2, 9, 10, 11, 13, 17); §5 configuration (9, 13, 17); §6 dependencies (10, 11); §7 testing — pure
translation against fixtures (4, 5), supervision (9), the offline loop (14), table-driven policy
(6), re-notification suppression (7), dead-letters written and redacted (2, 8, 9); §8 definition of
done — every line has a named test in Task 14 or its own task; §9 known limits (17).

**One deliberate departure, stated rather than hidden:** §3.3 gives `Destination` two fields;
this plan gives it three, adding `source`. §3.6 requires routing "to the adapter named by `source`",
so the router needs it, and it comes from the same `MessageRow` lookup as the other two. Recorded
here so a reviewer sees a decision rather than a drift.

**Three things the pre-scan changed about this plan before it was written** — the practice that has
caught defects in every phase since Phase 3: the ISO-timestamp requirement at the gateway boundary
(Slack sends an epoch float), the namespaced `external_id` (a Slack `ts` is unique only within a
channel, and `MessageRow.external_id` is globally unique), and the Discord parent-channel allowlist
rule (a threaded message carries the thread's id as `channel_id`, so the clarification loop's own
path would have been rejected). A fourth changed the `ChannelNotifier` ordering: a source with no
adapter is skipped, not dead-lettered, or a fresh clone's demo task fills the panel that exists to
show real drops.

**Placeholder scan:** every step carries the actual content — no "add error handling", no "similar
to Task N", no test referenced without its body.

**Type consistency:** `Destination(source, conversation_id, external_id)` is constructed in Task 7
and consumed in Tasks 8, 10, 11, 14 with those names. `dead_letter(source=, kind=, reason=,
payload=)` matches `DeadLetterRepository.record`'s signature at every call site (Tasks 8, 9, 10, 11,
13). `translate(payload, *, allowed_channels, bot_user_id)` is identical across both platforms and
every caller. `mark_notified(task_id, state)` returns `bool` and is called only in `_announce`.

**One risk this plan cannot remove, flagged for execution:** Task 12 changes existing intake
behaviour on the HTTP path, deliberately and per spec. The pre-scan says the current suite should
survive, but it is a reading of the tests, not a run of them. Task 12's Step 6 is where that gets
settled, and its instruction is explicit: fix the test's conversation, never the rule.
