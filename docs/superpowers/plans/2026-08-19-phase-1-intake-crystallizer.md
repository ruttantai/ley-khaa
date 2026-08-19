# Phase 1 — Intake + Task Crystallizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Phase 0 "every message becomes a task" stub with a real two-stage Task Crystallizer: messy multi-message conversations are ingested, filtered for relevance, assembled into stateful task candidates, and only a `ready` candidate — owning exactly the messages that belong to it — becomes a Task.

**Architecture:** Three new layers sit in front of the existing `Orchestrator`. (1) An **intake gateway** normalizes any inbound payload into a canonical multi-modal `Message` and persists it idempotently per conversation. (2) **Stage A**, a cheap per-message relevance/topic filter, prunes chatter. (3) **Stage B**, the LLM crystallizer, maintains persistent `TaskCandidate` rows over a rolling window per conversation — each candidate owning only its own message ids, so noise is excluded by construction and interleaved topics become separate candidates. A **readiness gate** debounces emission. All LLM access goes through a `ModelRouter` + `LLMClient` seam so policy is table-testable and tests never touch the network.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (typed `Mapped`), Postgres 16 / SQLite for tests, `anthropic` Python SDK, pytest; React + TypeScript + Vite + Tailwind v4, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-18-ley-khaa-design.md` — this plan implements §5.1 (simulator adapter only), §5.2 (intake gateway), §5.3 (Task Crystallizer, headline #1), and the §5.13 Model Router seam.

## Global Constraints

- **Python** `>=3.12`; **Pydantic** `v2`; **SQLAlchemy** `2.0` typed `Mapped` style. DB access **only** through repositories.
- **Model IDs are exact strings, never date-suffixed:** `claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-5`. Writing `claude-haiku-4-5-20251001` or similar is a bug.
- **Structured output:** always `client.messages.parse(model=…, output_format=SomePydanticModel, …)` and read `response.parsed_output`. Do not hand-roll JSON parsing, and do not use the deprecated top-level `output_format=` on `messages.create`.
- **Thinking is model-gated.** Pass `thinking={"type": "adaptive"}` **only** for `claude-opus-5` / `claude-sonnet-5`. `claude-haiku-4-5` is a pre-4.6 model: pass **no** `thinking` and **no** `output_config.effort` — both error. The router carries this as a flag so call sites never guess.
- **Tests never make network calls.** Every LLM-touching test injects `FakeLLM`. `AnthropicLLM` must not be constructed anywhere under `backend/tests/`.
- **Ollama offline fallback is NOT in this phase.** The `LLMClient` protocol exists so it can be added later; do not implement it now.
- **Data is synthetic only.** No real employer data, credentials, or infrastructure — ever.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`). Commit after every task.
- **Versioning:** SemVer; this phase is released as tag **`0.2.0`**.
- **Package name:** backend Python package is `ley_khaa` (underscore); repo/product name is `ley-khaa`.
- **Orchestrator stays synchronous** this phase. Per-project async concurrency is Phase `0.5.0`.

## File Structure

| File | Responsibility |
|---|---|
| `backend/ley_khaa/llm/router.py` | `Stage` enum, `ModelChoice`, `model_for(stage, signal)` policy table |
| `backend/ley_khaa/llm/client.py` | `LLMClient` protocol, `AnthropicLLM`, `FakeLLM` |
| `backend/ley_khaa/domain/models.py` | extend: `Attachment`, `AttachmentKind`, multi-modal `Message` |
| `backend/ley_khaa/persistence/orm.py` | extend: `MessageRow`, `CandidateRow` |
| `backend/ley_khaa/persistence/message_repository.py` | idempotent message persistence + rolling window reads |
| `backend/ley_khaa/persistence/candidate_repository.py` | candidate upsert-by-key, list per conversation |
| `backend/ley_khaa/intake/gateway.py` | normalize raw payload → canonical `Message`, dedupe |
| `backend/ley_khaa/crystallizer/relevance.py` | Stage A: per-message relevance + coarse topic |
| `backend/ley_khaa/crystallizer/candidate.py` | `CandidateState` lifecycle + transition rules |
| `backend/ley_khaa/crystallizer/engine.py` | Stage B: LLM candidate-state engine |
| `backend/ley_khaa/crystallizer/gate.py` | debounce / readiness gate |
| `backend/ley_khaa/orchestrator/orchestrator.py` | rewire: intake → Stage A → Stage B → gate → Task |
| `backend/tests/fixtures/conversations/*.json` | golden messy synthetic conversations |
| `frontend/src/Candidates.tsx` | candidate panel (state + owned message count) |

---

### Task 1: Model Router + LLM client seam

**Files:**
- Modify: `backend/pyproject.toml`
- Create: `backend/ley_khaa/llm/__init__.py`
- Create: `backend/ley_khaa/llm/router.py`
- Create: `backend/ley_khaa/llm/client.py`
- Modify: `backend/ley_khaa/config.py`
- Test: `backend/tests/test_llm_router.py`
- Test: `backend/tests/test_llm_client.py`

**Interfaces:**
- Consumes: nothing (first task of the phase).
- Produces: `Stage` (enum), `ModelChoice(model: str, supports_thinking: bool, max_tokens: int)`, `model_for(stage: Stage, complexity: str = "routine") -> ModelChoice`, `LLMClient` protocol with `parse(*, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T`, `FakeLLM(responses: list[Any])`, `AnthropicLLM(client=None)`.

- [ ] **Step 1: Add the anthropic dependency**

In `backend/pyproject.toml`, add to `dependencies`:

```toml
    "anthropic>=0.70",
```

Then run: `pip install -e ".[dev]"` from `backend/`.

- [ ] **Step 2: Write the failing router test**

Create `backend/tests/test_llm_router.py`:

```python
import pytest

from ley_khaa.llm.router import Stage, model_for


@pytest.mark.parametrize(
    "stage,complexity,expected_model",
    [
        (Stage.RELEVANCE_FILTER, "routine", "claude-haiku-4-5"),
        (Stage.RELEVANCE_FILTER, "hard", "claude-haiku-4-5"),
        (Stage.CRYSTALLIZER, "routine", "claude-haiku-4-5"),
        (Stage.CRYSTALLIZER, "hard", "claude-opus-5"),
        (Stage.INTERPRETER, "routine", "claude-opus-5"),
        (Stage.VISION_EXTRACTION, "routine", "claude-opus-5"),
    ],
)
def test_model_for_policy(stage, complexity, expected_model):
    assert model_for(stage, complexity).model == expected_model


def test_haiku_never_advertises_thinking():
    # Haiku 4.5 is pre-4.6: adaptive thinking and effort both error on it.
    choice = model_for(Stage.RELEVANCE_FILTER, "routine")
    assert choice.supports_thinking is False


def test_opus_advertises_thinking():
    assert model_for(Stage.INTERPRETER, "routine").supports_thinking is True


def test_unknown_complexity_falls_back_to_routine():
    assert model_for(Stage.CRYSTALLIZER, "banana").model == "claude-haiku-4-5"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest tests/test_llm_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.llm'`

- [ ] **Step 4: Implement the router**

Create `backend/ley_khaa/llm/__init__.py` (empty file).

Create `backend/ley_khaa/llm/router.py`:

```python
from dataclasses import dataclass
from enum import Enum

# Exact model ids. Never append a date suffix.
HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"

# Haiku 4.5 predates adaptive thinking: sending `thinking` or `output_config.effort`
# to it is a 400. Only the 5-series models below accept adaptive thinking.
_THINKING_MODELS = {SONNET, OPUS}


class Stage(str, Enum):
    RELEVANCE_FILTER = "relevance_filter"
    CRYSTALLIZER = "crystallizer"
    INTERPRETER = "interpreter"
    VISION_EXTRACTION = "vision_extraction"


@dataclass(frozen=True)
class ModelChoice:
    model: str
    supports_thinking: bool
    max_tokens: int


# stage -> {complexity -> model}. "routine" is the fallback for any unknown signal.
_POLICY: dict[Stage, dict[str, str]] = {
    Stage.RELEVANCE_FILTER: {"routine": HAIKU, "hard": HAIKU},
    Stage.CRYSTALLIZER: {"routine": HAIKU, "hard": OPUS},
    Stage.INTERPRETER: {"routine": OPUS, "hard": OPUS},
    Stage.VISION_EXTRACTION: {"routine": OPUS, "hard": OPUS},
}

_MAX_TOKENS: dict[Stage, int] = {
    Stage.RELEVANCE_FILTER: 512,
    Stage.CRYSTALLIZER: 8000,
    Stage.INTERPRETER: 8000,
    Stage.VISION_EXTRACTION: 8000,
}


def model_for(stage: Stage, complexity: str = "routine") -> ModelChoice:
    by_complexity = _POLICY[stage]
    model = by_complexity.get(complexity, by_complexity["routine"])
    return ModelChoice(
        model=model,
        supports_thinking=model in _THINKING_MODELS,
        max_tokens=_MAX_TOKENS[stage],
    )
```

- [ ] **Step 5: Run the router test to verify it passes**

Run: `python -m pytest tests/test_llm_router.py -v`
Expected: PASS (9 tests)

- [ ] **Step 6: Write the failing client test**

Create `backend/tests/test_llm_client.py`:

```python
import pytest
from pydantic import BaseModel

from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.router import Stage, model_for


class Verdict(BaseModel):
    relevant: bool


def test_fake_llm_returns_queued_responses_in_order():
    llm = FakeLLM([Verdict(relevant=True), Verdict(relevant=False)])
    choice = model_for(Stage.RELEVANCE_FILTER)
    first = llm.parse(choice=choice, system="s", user="u", output_format=Verdict)
    second = llm.parse(choice=choice, system="s", user="u", output_format=Verdict)
    assert first.relevant is True
    assert second.relevant is False


def test_fake_llm_records_calls():
    llm = FakeLLM([Verdict(relevant=True)])
    choice = model_for(Stage.RELEVANCE_FILTER)
    llm.parse(choice=choice, system="sys-prompt", user="the message", output_format=Verdict)
    assert len(llm.calls) == 1
    assert llm.calls[0].choice.model == "claude-haiku-4-5"
    assert llm.calls[0].user == "the message"


def test_fake_llm_raises_when_exhausted():
    llm = FakeLLM([])
    with pytest.raises(AssertionError, match="FakeLLM exhausted"):
        llm.parse(choice=model_for(Stage.RELEVANCE_FILTER), system="s", user="u", output_format=Verdict)
```

- [ ] **Step 7: Run it to verify it fails**

Run: `python -m pytest tests/test_llm_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.llm.client'`

- [ ] **Step 8: Implement the client seam**

Create `backend/ley_khaa/llm/client.py`:

```python
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from .router import ModelChoice

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    """The single seam every LLM call goes through.

    Implementations: AnthropicLLM (production) and FakeLLM (tests). An Ollama
    offline fallback plugs in here in a later phase.
    """

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        ...


@dataclass
class RecordedCall:
    choice: ModelChoice
    system: str
    user: str
    output_format: type


@dataclass
class FakeLLM:
    """Deterministic stand-in. Returns queued responses in order and records calls."""

    responses: list[Any]
    calls: list[RecordedCall] = field(default_factory=list)

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        self.calls.append(RecordedCall(choice=choice, system=system, user=user, output_format=output_format))
        assert self.responses, "FakeLLM exhausted: more parse() calls than queued responses"
        return self.responses.pop(0)


class AnthropicLLM:
    """Production client. Never instantiated from tests."""

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        kwargs: dict[str, Any] = {
            "model": choice.model,
            "max_tokens": choice.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_format": output_format,
        }
        # Adaptive thinking only exists on the 5-series models; sending it to
        # Haiku 4.5 is a 400.
        if choice.supports_thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        response = self._client.messages.parse(**kwargs)
        return response.parsed_output
```

- [ ] **Step 9: Add LLM settings to config**

In `backend/ley_khaa/config.py`, add two fields to the `Settings` dataclass (keep the existing two):

```python
    llm_backend: str = os.getenv("LEY_KHAA_LLM", "anthropic")
    crystallizer_debounce_seconds: int = int(os.getenv("LEY_KHAA_DEBOUNCE_SECONDS", "45"))
```

- [ ] **Step 10: Pin the test environment**

Tests must never depend on a real API key and must not inherit the production
debounce window. In `backend/tests/conftest.py`, extend the env block at the very
top of the file (before any `ley_khaa` import) to:

```python
import os

os.environ["LEY_KHAA_DISABLE_STARTUP"] = "1"
os.environ["LEY_KHAA_LLM"] = "heuristic"     # never touch the network in tests
os.environ["LEY_KHAA_DEBOUNCE_SECONDS"] = "0"  # don't wait for a conversational pause
```

While in this file, drop the redundant `import pytest as _pytest` and use the
already-imported `pytest` for the `session` fixture decorator (Phase 0 cleanup).

- [ ] **Step 11: Run the full backend suite**

Run: `python -m pytest -q`
Expected: PASS — 19 pre-existing + 12 new, 0 warnings.

- [ ] **Step 12: Commit**

```bash
git add backend/pyproject.toml backend/ley_khaa/llm backend/ley_khaa/config.py backend/tests/conftest.py backend/tests/test_llm_router.py backend/tests/test_llm_client.py
git commit -m "feat: model router and LLM client seam"
```

---

### Task 2: Multi-modal Message + idempotent persistence

**Files:**
- Modify: `backend/ley_khaa/domain/models.py`
- Modify: `backend/ley_khaa/persistence/orm.py`
- Create: `backend/ley_khaa/persistence/message_repository.py`
- Test: `backend/tests/test_message_repository.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `AttachmentKind` (enum: `TEXT`/`TABLE`/`IMAGE`), `Attachment(kind, name, content)`, `Message(..., external_id: str | None, attachments: list[Attachment])`, `MessageRow`, `MessageRepository(session)` with `add(message) -> MessageRow`, `list_for_conversation(conversation_id) -> list[MessageRow]`, `window(conversation_id, limit) -> list[MessageRow]`, `last_timestamp(conversation_id) -> datetime | None`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_message_repository.py`:

```python
from datetime import datetime, timedelta, timezone

from ley_khaa.domain.models import Attachment, AttachmentKind, Message
from ley_khaa.persistence.message_repository import MessageRepository


def _msg(text="hello", conv="c1", external_id=None, ts=None):
    return Message(
        source="simulator",
        client="demo",
        conversation_id=conv,
        author="boss",
        text=text,
        external_id=external_id,
        timestamp=ts or datetime.now(timezone.utc),
    )


def test_add_persists_message(session):
    repo = MessageRepository(session)
    row = repo.add(_msg("first"))
    assert row.text == "first"
    assert repo.list_for_conversation("c1")[0].id == row.id


def test_add_is_idempotent_per_external_id(session):
    repo = MessageRepository(session)
    first = repo.add(_msg("dup", external_id="slack-123"))
    second = repo.add(_msg("dup", external_id="slack-123"))
    assert first.id == second.id
    assert len(repo.list_for_conversation("c1")) == 1


def test_messages_without_external_id_are_not_deduped(session):
    repo = MessageRepository(session)
    repo.add(_msg("same text"))
    repo.add(_msg("same text"))
    assert len(repo.list_for_conversation("c1")) == 2


def test_list_is_ordered_by_timestamp(session):
    repo = MessageRepository(session)
    base = datetime.now(timezone.utc)
    repo.add(_msg("second", ts=base + timedelta(seconds=10)))
    repo.add(_msg("first", ts=base))
    assert [m.text for m in repo.list_for_conversation("c1")] == ["first", "second"]


def test_window_returns_most_recent_n_in_order(session):
    repo = MessageRepository(session)
    base = datetime.now(timezone.utc)
    for i in range(5):
        repo.add(_msg(f"m{i}", ts=base + timedelta(seconds=i)))
    assert [m.text for m in repo.window("c1", limit=3)] == ["m2", "m3", "m4"]


def test_conversations_are_isolated(session):
    repo = MessageRepository(session)
    repo.add(_msg("in c1", conv="c1"))
    repo.add(_msg("in c2", conv="c2"))
    assert len(repo.list_for_conversation("c1")) == 1


def test_attachments_round_trip(session):
    repo = MessageRepository(session)
    m = _msg("see table")
    m.attachments = [Attachment(kind=AttachmentKind.TABLE, name="holdings.csv", content="a,b\n1,2")]
    row = repo.add(m)
    assert row.attachments[0]["kind"] == "table"
    assert row.attachments[0]["name"] == "holdings.csv"


def test_last_timestamp_returns_latest(session):
    repo = MessageRepository(session)
    base = datetime.now(timezone.utc)
    repo.add(_msg("a", ts=base))
    repo.add(_msg("b", ts=base + timedelta(seconds=30)))
    assert repo.last_timestamp("c1") == repo.list_for_conversation("c1")[-1].timestamp


def test_last_timestamp_none_for_empty_conversation(session):
    assert MessageRepository(session).last_timestamp("nope") is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_message_repository.py -v`
Expected: FAIL with `ImportError: cannot import name 'Attachment'`

- [ ] **Step 3: Extend the domain model**

In `backend/ley_khaa/domain/models.py`, add above `Message` and extend `Message`:

```python
from enum import Enum


class AttachmentKind(str, Enum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"


class Attachment(BaseModel):
    kind: AttachmentKind
    name: str
    # For TEXT/TABLE this is the literal content; for IMAGE it is a path or
    # base64 payload. Images are NOT interpreted at intake (spec §5.2).
    content: str
```

Then add these two fields to `Message`:

```python
    external_id: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
```

- [ ] **Step 4: Add the MessageRow table**

In `backend/ley_khaa/persistence/orm.py`, append:

```python
class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    source: Mapped[str] = mapped_column(String)
    client: Mapped[str] = mapped_column(String)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    author: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(String)
    attachments: Mapped[list] = mapped_column(JSON, default=list)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
```

- [ ] **Step 5: Implement the repository**

Create `backend/ley_khaa/persistence/message_repository.py`:

```python
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.models import Message
from .orm import MessageRow


class MessageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, message: Message) -> MessageRow:
        # Idempotent per external id so channel retries never duplicate (spec §5.2).
        if message.external_id is not None:
            existing = self.session.scalars(
                select(MessageRow).where(MessageRow.external_id == message.external_id)
            ).first()
            if existing is not None:
                return existing
        row = MessageRow(
            id=message.id,
            external_id=message.external_id,
            source=message.source,
            client=message.client,
            conversation_id=message.conversation_id,
            author=message.author,
            text=message.text,
            attachments=[a.model_dump(mode="json") for a in message.attachments],
            timestamp=message.timestamp,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def list_for_conversation(self, conversation_id: str) -> list[MessageRow]:
        return list(
            self.session.scalars(
                select(MessageRow)
                .where(MessageRow.conversation_id == conversation_id)
                .order_by(MessageRow.timestamp, MessageRow.id)
            )
        )

    def window(self, conversation_id: str, limit: int = 30) -> list[MessageRow]:
        """The most recent `limit` messages, oldest-first."""
        return self.list_for_conversation(conversation_id)[-limit:]

    def last_timestamp(self, conversation_id: str) -> datetime | None:
        rows = self.list_for_conversation(conversation_id)
        return rows[-1].timestamp if rows else None
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/test_message_repository.py -v`
Expected: PASS (9 tests)

- [ ] **Step 7: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS, 0 warnings.

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa/domain/models.py backend/ley_khaa/persistence/orm.py backend/ley_khaa/persistence/message_repository.py backend/tests/test_message_repository.py
git commit -m "feat: multi-modal message model with idempotent persistence"
```

---

### Task 3: Intake gateway

**Files:**
- Create: `backend/ley_khaa/intake/__init__.py`
- Create: `backend/ley_khaa/intake/gateway.py`
- Test: `backend/tests/test_intake_gateway.py`

**Interfaces:**
- Consumes: `Message`, `Attachment`, `AttachmentKind`, `MessageRepository` (Task 2).
- Produces: `IntakeGateway(repo: MessageRepository)` with `accept(raw: dict) -> MessageRow`. Normalizes `thread_id`→`conversation_id`, defaults `source`/`client`/`author`, coerces attachment dicts, and persists idempotently.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_intake_gateway.py`:

```python
import pytest

from ley_khaa.intake.gateway import IntakeGateway
from ley_khaa.persistence.message_repository import MessageRepository


@pytest.fixture
def gateway(session):
    return IntakeGateway(MessageRepository(session))


def test_accept_normalizes_minimal_payload(gateway):
    row = gateway.accept({"text": "do the universe check"})
    assert row.text == "do the universe check"
    assert row.source == "simulator"
    assert row.client == "demo"
    assert row.conversation_id == "conv-1"
    assert row.author == "user"


def test_thread_id_is_accepted_as_conversation_id(gateway):
    row = gateway.accept({"text": "hi", "thread_id": "slack-thread-9"})
    assert row.conversation_id == "slack-thread-9"


def test_explicit_conversation_id_wins_over_thread_id(gateway):
    row = gateway.accept({"text": "hi", "thread_id": "t1", "conversation_id": "c9"})
    assert row.conversation_id == "c9"


def test_attachments_are_coerced(gateway):
    row = gateway.accept(
        {
            "text": "here it is",
            "attachments": [{"kind": "table", "name": "u.csv", "content": "a,b"}],
        }
    )
    assert row.attachments[0]["kind"] == "table"


def test_image_attachment_is_stored_not_interpreted(gateway):
    row = gateway.accept(
        {"text": "see chart", "attachments": [{"kind": "image", "name": "c.png", "content": "/tmp/c.png"}]}
    )
    assert row.attachments[0]["kind"] == "image"
    assert row.attachments[0]["content"] == "/tmp/c.png"


def test_accept_is_idempotent_per_external_id(gateway):
    first = gateway.accept({"text": "same", "external_id": "slack-1"})
    second = gateway.accept({"text": "same", "external_id": "slack-1"})
    assert first.id == second.id


def test_missing_text_raises(gateway):
    with pytest.raises(ValueError, match="text is required"):
        gateway.accept({"author": "boss"})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_intake_gateway.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.intake'`

- [ ] **Step 3: Implement the gateway**

Create `backend/ley_khaa/intake/__init__.py` (empty file).

Create `backend/ley_khaa/intake/gateway.py`:

```python
from typing import Any

from ..domain.models import Attachment, Message
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import MessageRow


class IntakeGateway:
    """Normalizes any inbound payload to a canonical Message and persists it.

    Adapters (simulator now; Slack/Discord later) hand raw dicts to this one
    door. Images are stored, never interpreted here (spec §5.2).
    """

    def __init__(self, repo: MessageRepository) -> None:
        self.repo = repo

    def accept(self, raw: dict[str, Any]) -> MessageRow:
        text = raw.get("text")
        if not text:
            raise ValueError("text is required")

        conversation_id = raw.get("conversation_id") or raw.get("thread_id") or "conv-1"
        attachments = [Attachment(**a) for a in raw.get("attachments", [])]

        message = Message(
            source=raw.get("source", "simulator"),
            client=raw.get("client", "demo"),
            conversation_id=conversation_id,
            author=raw.get("author", "user"),
            text=text,
            external_id=raw.get("external_id"),
            attachments=attachments,
        )
        return self.repo.add(message)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_intake_gateway.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/ley_khaa/intake backend/tests/test_intake_gateway.py
git commit -m "feat: intake gateway normalizing multi-modal messages"
```

---

### Task 4: Stage A — relevance/topic filter

**Files:**
- Create: `backend/ley_khaa/crystallizer/__init__.py`
- Create: `backend/ley_khaa/crystallizer/relevance.py`
- Test: `backend/tests/test_relevance_filter.py`

**Interfaces:**
- Consumes: `LLMClient`, `FakeLLM`, `Stage`, `model_for` (Task 1); `MessageRow` (Task 2).
- Produces: `RelevanceVerdict(relevant: bool, topic: str, confidence: float)`, `RelevanceFilter(llm: LLMClient)` with `judge(row: MessageRow) -> RelevanceVerdict`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_relevance_filter.py`:

```python
from datetime import datetime, timezone

from ley_khaa.crystallizer.relevance import RelevanceFilter, RelevanceVerdict
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.orm import MessageRow


def _row(text="Can you compare the two universes?", author="boss"):
    return MessageRow(
        id="m1",
        external_id=None,
        source="simulator",
        client="demo",
        conversation_id="c1",
        author=author,
        text=text,
        attachments=[],
        timestamp=datetime.now(timezone.utc),
    )


def test_judge_returns_the_models_verdict():
    llm = FakeLLM([RelevanceVerdict(relevant=True, topic="universe-reconciliation", confidence=0.9)])
    verdict = RelevanceFilter(llm).judge(_row())
    assert verdict.relevant is True
    assert verdict.topic == "universe-reconciliation"


def test_judge_routes_to_the_cheap_model():
    llm = FakeLLM([RelevanceVerdict(relevant=False, topic="chatter", confidence=0.8)])
    RelevanceFilter(llm).judge(_row("lol same"))
    assert llm.calls[0].choice.model == "claude-haiku-4-5"
    assert llm.calls[0].choice.supports_thinking is False


def test_judge_includes_author_and_text_in_the_prompt():
    llm = FakeLLM([RelevanceVerdict(relevant=True, topic="t", confidence=0.5)])
    RelevanceFilter(llm).judge(_row("reconcile the holdings", author="alice"))
    user_prompt = llm.calls[0].user
    assert "alice" in user_prompt
    assert "reconcile the holdings" in user_prompt


def test_attachments_are_summarized_into_the_prompt():
    llm = FakeLLM([RelevanceVerdict(relevant=True, topic="t", confidence=0.5)])
    row = _row("see attached")
    row.attachments = [{"kind": "table", "name": "holdings.csv", "content": "a,b\n1,2"}]
    RelevanceFilter(llm).judge(row)
    assert "holdings.csv" in llm.calls[0].user
    assert "table" in llm.calls[0].user


def test_confidence_is_clamped_to_unit_range():
    # Pydantic bounds keep a hallucinated 4.2 out of downstream scoring.
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RelevanceVerdict(relevant=True, topic="t", confidence=4.2)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_relevance_filter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.crystallizer'`

- [ ] **Step 3: Implement Stage A**

Create `backend/ley_khaa/crystallizer/__init__.py` (empty file).

Create `backend/ley_khaa/crystallizer/relevance.py`:

```python
from pydantic import BaseModel, Field

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.orm import MessageRow

SYSTEM = """You triage messages from a work chat.

For each message decide whether it could contribute to an actionable work request
(data pulls, reconciliations, reports, analyses) or is conversational noise
(greetings, jokes, acknowledgements, scheduling chatter).

Assign a short kebab-case topic label. Reuse the same label for the same subject.
Be generous: a fragment that only makes sense with earlier messages is still relevant."""


class RelevanceVerdict(BaseModel):
    relevant: bool
    topic: str
    confidence: float = Field(ge=0.0, le=1.0)


class RelevanceFilter:
    """Stage A: cheap per-message pruning before the expensive stateful pass."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def judge(self, row: MessageRow) -> RelevanceVerdict:
        return self.llm.parse(
            choice=model_for(Stage.RELEVANCE_FILTER),
            system=SYSTEM,
            user=_render(row),
            output_format=RelevanceVerdict,
        )


def _render(row: MessageRow) -> str:
    lines = [f"author: {row.author}", f"text: {row.text}"]
    for a in row.attachments or []:
        lines.append(f"attachment: {a['kind']} named {a['name']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_relevance_filter.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/ley_khaa/crystallizer backend/tests/test_relevance_filter.py
git commit -m "feat: stage A relevance and topic filter"
```

---

### Task 5: Task candidate lifecycle + persistence

**Files:**
- Create: `backend/ley_khaa/crystallizer/candidate.py`
- Modify: `backend/ley_khaa/persistence/orm.py`
- Create: `backend/ley_khaa/persistence/candidate_repository.py`
- Test: `backend/tests/test_candidate.py`
- Test: `backend/tests/test_candidate_repository.py`

**Interfaces:**
- Consumes: `Base`, `_now` from `persistence/orm.py`.
- Produces: `CandidateState` (enum `FORMING`/`CRYSTALLIZING`/`READY`/`ABANDONED`/`PROMOTED`), `can_transition`, `ensure_transition`, `InvalidCandidateTransition`, `CandidateRow`, `CandidateRepository(session)` with `upsert(conversation_id, candidate_key, *, title, summary, state, message_ids, missing_fields, open_question) -> CandidateRow`, `get_by_key(conversation_id, candidate_key) -> CandidateRow | None`, `list_for_conversation(conversation_id) -> list[CandidateRow]`, `list_by_state(state) -> list[CandidateRow]`, `mark_promoted(candidate_id, task_id) -> CandidateRow`.

- [ ] **Step 1: Write the failing lifecycle test**

Create `backend/tests/test_candidate.py`:

```python
import pytest

from ley_khaa.crystallizer.candidate import (
    CandidateState,
    InvalidCandidateTransition,
    can_transition,
    ensure_transition,
)


@pytest.mark.parametrize(
    "current,target,allowed",
    [
        (CandidateState.FORMING, CandidateState.CRYSTALLIZING, True),
        (CandidateState.FORMING, CandidateState.ABANDONED, True),
        (CandidateState.FORMING, CandidateState.READY, True),
        (CandidateState.CRYSTALLIZING, CandidateState.READY, True),
        (CandidateState.CRYSTALLIZING, CandidateState.FORMING, True),
        (CandidateState.READY, CandidateState.PROMOTED, True),
        (CandidateState.READY, CandidateState.CRYSTALLIZING, True),
        (CandidateState.PROMOTED, CandidateState.READY, False),
        (CandidateState.PROMOTED, CandidateState.FORMING, False),
        (CandidateState.ABANDONED, CandidateState.FORMING, False),
    ],
)
def test_transition_rules(current, target, allowed):
    assert can_transition(current, target) is allowed


def test_ensure_transition_raises_on_illegal_move():
    with pytest.raises(InvalidCandidateTransition, match="promoted -> forming"):
        ensure_transition(CandidateState.PROMOTED, CandidateState.FORMING)


def test_ensure_transition_allows_legal_move():
    ensure_transition(CandidateState.FORMING, CandidateState.CRYSTALLIZING)


def test_same_state_is_allowed():
    # The LLM re-reports an unchanged candidate on most turns.
    assert can_transition(CandidateState.FORMING, CandidateState.FORMING) is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_candidate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.crystallizer.candidate'`

- [ ] **Step 3: Implement the lifecycle**

Create `backend/ley_khaa/crystallizer/candidate.py`:

```python
from enum import Enum


class CandidateState(str, Enum):
    FORMING = "forming"
    CRYSTALLIZING = "crystallizing"
    READY = "ready"
    PROMOTED = "promoted"
    ABANDONED = "abandoned"


# A candidate can slide backwards (a follow-up message reopens a settled request)
# but PROMOTED and ABANDONED are terminal.
_ALLOWED: dict[CandidateState, set[CandidateState]] = {
    CandidateState.FORMING: {
        CandidateState.FORMING,
        CandidateState.CRYSTALLIZING,
        CandidateState.READY,
        CandidateState.ABANDONED,
    },
    CandidateState.CRYSTALLIZING: {
        CandidateState.CRYSTALLIZING,
        CandidateState.FORMING,
        CandidateState.READY,
        CandidateState.ABANDONED,
    },
    CandidateState.READY: {
        CandidateState.READY,
        CandidateState.CRYSTALLIZING,
        CandidateState.PROMOTED,
        CandidateState.ABANDONED,
    },
    CandidateState.PROMOTED: set(),
    CandidateState.ABANDONED: set(),
}


class InvalidCandidateTransition(Exception):
    pass


def can_transition(current: CandidateState, target: CandidateState) -> bool:
    return target in _ALLOWED[current]


def ensure_transition(current: CandidateState, target: CandidateState) -> None:
    if not can_transition(current, target):
        raise InvalidCandidateTransition(f"{current.value} -> {target.value} not allowed")
```

- [ ] **Step 4: Run the lifecycle test to verify it passes**

Run: `python -m pytest tests/test_candidate.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Write the failing repository test**

Create `backend/tests/test_candidate_repository.py`:

```python
import pytest

from ley_khaa.crystallizer.candidate import CandidateState, InvalidCandidateTransition
from ley_khaa.persistence.candidate_repository import CandidateRepository


def _upsert(repo, key="cand-a", state=CandidateState.FORMING, message_ids=None, conv="c1"):
    return repo.upsert(
        conversation_id=conv,
        candidate_key=key,
        title="Universe check",
        summary="Compare Bloomberg vs FactSet",
        state=state,
        message_ids=message_ids if message_ids is not None else ["m1"],
        missing_fields=[],
        open_question=None,
    )


def test_upsert_creates_then_updates_same_row(session):
    repo = CandidateRepository(session)
    first = _upsert(repo)
    second = _upsert(repo, state=CandidateState.CRYSTALLIZING, message_ids=["m1", "m2"])
    assert first.id == second.id
    assert second.state == "crystallizing"
    assert second.message_ids == ["m1", "m2"]
    assert len(repo.list_for_conversation("c1")) == 1


def test_candidate_keys_are_scoped_per_conversation(session):
    repo = CandidateRepository(session)
    _upsert(repo, conv="c1")
    _upsert(repo, conv="c2")
    assert len(repo.list_for_conversation("c1")) == 1
    assert len(repo.list_for_conversation("c2")) == 1


def test_upsert_rejects_illegal_transition(session):
    repo = CandidateRepository(session)
    row = _upsert(repo, state=CandidateState.READY)
    repo.mark_promoted(row.id, task_id="t1")
    with pytest.raises(InvalidCandidateTransition):
        _upsert(repo, state=CandidateState.FORMING)


def test_mark_promoted_records_task_id(session):
    repo = CandidateRepository(session)
    row = _upsert(repo, state=CandidateState.READY)
    promoted = repo.mark_promoted(row.id, task_id="task-99")
    assert promoted.state == "promoted"
    assert promoted.task_id == "task-99"


def test_list_by_state_filters(session):
    repo = CandidateRepository(session)
    _upsert(repo, key="a", state=CandidateState.READY)
    _upsert(repo, key="b", state=CandidateState.FORMING)
    ready = repo.list_by_state(CandidateState.READY)
    assert [r.candidate_key for r in ready] == ["a"]


def test_get_by_key_returns_none_when_absent(session):
    assert CandidateRepository(session).get_by_key("c1", "nope") is None


def test_open_question_round_trips(session):
    repo = CandidateRepository(session)
    row = repo.upsert(
        conversation_id="c1",
        candidate_key="k",
        title="t",
        summary="s",
        state=CandidateState.CRYSTALLIZING,
        message_ids=["m1"],
        missing_fields=["output_format"],
        open_question="Excel or CSV?",
    )
    assert row.open_question == "Excel or CSV?"
    assert row.missing_fields == ["output_format"]
```

- [ ] **Step 6: Run it to verify it fails**

Run: `python -m pytest tests/test_candidate_repository.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.persistence.candidate_repository'`

- [ ] **Step 7: Add the CandidateRow table**

In `backend/ley_khaa/persistence/orm.py`, append:

```python
class CandidateRow(Base):
    __tablename__ = "task_candidates"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String, index=True)
    # Stable key the crystallizer reuses to re-identify a candidate across turns.
    candidate_key: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String, default="")
    summary: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String)
    message_ids: Mapped[list] = mapped_column(JSON, default=list)
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    open_question: Mapped[str | None] = mapped_column(String, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
```

- [ ] **Step 8: Implement the candidate repository**

Create `backend/ley_khaa/persistence/candidate_repository.py`:

```python
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..crystallizer.candidate import CandidateState, ensure_transition
from .orm import CandidateRow


class CandidateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        *,
        conversation_id: str,
        candidate_key: str,
        title: str,
        summary: str,
        state: CandidateState,
        message_ids: list[str],
        missing_fields: list[str],
        open_question: str | None,
    ) -> CandidateRow:
        row = self.get_by_key(conversation_id, candidate_key)
        if row is None:
            row = CandidateRow(
                id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                candidate_key=candidate_key,
                state=state.value,
            )
            self.session.add(row)
        else:
            ensure_transition(CandidateState(row.state), state)
            row.state = state.value
        row.title = title
        row.summary = summary
        row.message_ids = message_ids
        row.missing_fields = missing_fields
        row.open_question = open_question
        self.session.commit()
        self.session.refresh(row)
        return row

    def get_by_key(self, conversation_id: str, candidate_key: str) -> CandidateRow | None:
        return self.session.scalars(
            select(CandidateRow).where(
                CandidateRow.conversation_id == conversation_id,
                CandidateRow.candidate_key == candidate_key,
            )
        ).first()

    def list_for_conversation(self, conversation_id: str) -> list[CandidateRow]:
        return list(
            self.session.scalars(
                select(CandidateRow)
                .where(CandidateRow.conversation_id == conversation_id)
                .order_by(CandidateRow.created_at)
            )
        )

    def list_by_state(self, state: CandidateState) -> list[CandidateRow]:
        return list(
            self.session.scalars(
                select(CandidateRow)
                .where(CandidateRow.state == state.value)
                .order_by(CandidateRow.created_at)
            )
        )

    def list_all(self) -> list[CandidateRow]:
        return list(self.session.scalars(select(CandidateRow).order_by(CandidateRow.created_at)))

    def mark_promoted(self, candidate_id: str, task_id: str) -> CandidateRow:
        row = self.session.get(CandidateRow, candidate_id)
        if row is None:
            raise KeyError(candidate_id)
        ensure_transition(CandidateState(row.state), CandidateState.PROMOTED)
        row.state = CandidateState.PROMOTED.value
        row.task_id = task_id
        self.session.commit()
        self.session.refresh(row)
        return row
```

- [ ] **Step 9: Run the repository test to verify it passes**

Run: `python -m pytest tests/test_candidate_repository.py -v`
Expected: PASS (7 tests)

- [ ] **Step 10: Commit**

```bash
git add backend/ley_khaa/crystallizer/candidate.py backend/ley_khaa/persistence/orm.py backend/ley_khaa/persistence/candidate_repository.py backend/tests/test_candidate.py backend/tests/test_candidate_repository.py
git commit -m "feat: task candidate lifecycle and persistence"
```

---

### Task 6: Stage B — the LLM crystallizer engine

**Files:**
- Create: `backend/ley_khaa/crystallizer/engine.py`
- Test: `backend/tests/test_crystallizer_engine.py`

**Interfaces:**
- Consumes: `LLMClient`, `Stage`, `model_for` (Task 1); `MessageRepository` (Task 2); `RelevanceVerdict` (Task 4); `CandidateState`, `CandidateRepository` (Task 5).
- Produces: `CandidateDraft(candidate_key, title, summary, message_ids, state, missing_fields, open_question)`, `CrystallizerOutput(candidates: list[CandidateDraft])`, `Crystallizer(llm, messages, candidates, window_size=30)` with `observe(conversation_id: str, verdict: RelevanceVerdict) -> list[CandidateRow]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_crystallizer_engine.py`:

```python
from datetime import datetime, timedelta, timezone

from ley_khaa.crystallizer.engine import CandidateDraft, Crystallizer, CrystallizerOutput
from ley_khaa.crystallizer.relevance import RelevanceVerdict
from ley_khaa.domain.models import Message
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository

RELEVANT = RelevanceVerdict(relevant=True, topic="universe", confidence=0.9)
NOISE = RelevanceVerdict(relevant=False, topic="chatter", confidence=0.9)


def _seed(session, texts, conv="c1"):
    repo = MessageRepository(session)
    base = datetime.now(timezone.utc)
    rows = []
    for i, t in enumerate(texts):
        rows.append(
            repo.add(
                Message(
                    source="simulator",
                    client="demo",
                    conversation_id=conv,
                    author="boss",
                    text=t,
                    timestamp=base + timedelta(seconds=i),
                )
            )
        )
    return rows


def _engine(session, llm):
    return Crystallizer(llm, MessageRepository(session), CandidateRepository(session))


def _draft(**kw):
    base = dict(
        candidate_key="cand-universe",
        title="Universe reconciliation",
        summary="Compare Bloomberg vs FactSet, send the difference",
        message_ids=["m1"],
        state="forming",
        missing_fields=[],
        open_question=None,
    )
    base.update(kw)
    return CandidateDraft(**base)


def test_noise_verdict_skips_the_llm_entirely(session):
    _seed(session, ["lol"])
    llm = FakeLLM([])  # exhausted: any call would raise
    result = _engine(session, llm).observe("c1", NOISE)
    assert result == []
    assert llm.calls == []


def test_relevant_message_creates_a_candidate(session):
    rows = _seed(session, ["compare the universes"])
    llm = FakeLLM([CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])])])
    result = _engine(session, llm).observe("c1", RELEVANT)
    assert len(result) == 1
    assert result[0].title == "Universe reconciliation"
    assert result[0].state == "forming"


def test_candidate_is_updated_not_duplicated_across_turns(session):
    rows = _seed(session, ["compare the universes", "as of month end"])
    llm = FakeLLM(
        [
            CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])]),
            CrystallizerOutput(
                candidates=[_draft(state="ready", message_ids=[rows[0].id, rows[1].id])]
            ),
        ]
    )
    engine = _engine(session, llm)
    engine.observe("c1", RELEVANT)
    result = engine.observe("c1", RELEVANT)
    assert len(result) == 1
    assert result[0].state == "ready"
    assert result[0].message_ids == [rows[0].id, rows[1].id]
    assert len(CandidateRepository(session).list_for_conversation("c1")) == 1


def test_two_interleaved_topics_become_two_candidates(session):
    rows = _seed(session, ["compare universes", "also rebuild the risk report"])
    llm = FakeLLM(
        [
            CrystallizerOutput(
                candidates=[
                    _draft(candidate_key="cand-universe", message_ids=[rows[0].id]),
                    _draft(
                        candidate_key="cand-risk",
                        title="Risk report rebuild",
                        message_ids=[rows[1].id],
                    ),
                ]
            )
        ]
    )
    result = _engine(session, llm).observe("c1", RELEVANT)
    assert {c.candidate_key for c in result} == {"cand-universe", "cand-risk"}


def test_noise_messages_are_never_owned_by_a_candidate(session):
    rows = _seed(session, ["compare universes", "haha nice", "by month end"])
    llm = FakeLLM(
        [CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id, rows[2].id])])]
    )
    result = _engine(session, llm).observe("c1", RELEVANT)
    assert rows[1].id not in result[0].message_ids


def test_missing_fields_and_question_are_persisted(session):
    rows = _seed(session, ["send me the differences"])
    llm = FakeLLM(
        [
            CrystallizerOutput(
                candidates=[
                    _draft(
                        state="crystallizing",
                        message_ids=[rows[0].id],
                        missing_fields=["output_format"],
                        open_question="Excel or CSV?",
                    )
                ]
            )
        ]
    )
    result = _engine(session, llm).observe("c1", RELEVANT)
    assert result[0].missing_fields == ["output_format"]
    assert result[0].open_question == "Excel or CSV?"


def test_prompt_carries_the_rolling_window_and_existing_candidates(session):
    rows = _seed(session, ["compare universes", "by month end"])
    llm = FakeLLM(
        [
            CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])]),
            CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id, rows[1].id])]),
        ]
    )
    engine = _engine(session, llm)
    engine.observe("c1", RELEVANT)
    engine.observe("c1", RELEVANT)
    second_prompt = llm.calls[1].user
    assert "by month end" in second_prompt          # window
    assert "cand-universe" in second_prompt          # existing candidate state


def test_window_is_capped(session):
    rows = _seed(session, [f"message {i}" for i in range(40)])
    llm = FakeLLM([CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])])])
    engine = Crystallizer(
        llm, MessageRepository(session), CandidateRepository(session), window_size=5
    )
    engine.observe("c1", RELEVANT)
    prompt = llm.calls[0].user
    assert "message 39" in prompt
    assert "message 0\n" not in prompt


def test_promoted_candidate_is_not_resurrected(session):
    rows = _seed(session, ["compare universes"])
    llm = FakeLLM(
        [
            CrystallizerOutput(candidates=[_draft(state="ready", message_ids=[rows[0].id])]),
            CrystallizerOutput(candidates=[_draft(state="ready", message_ids=[rows[0].id])]),
        ]
    )
    engine = _engine(session, llm)
    engine.observe("c1", RELEVANT)
    CandidateRepository(session).mark_promoted(
        CandidateRepository(session).list_for_conversation("c1")[0].id, task_id="t1"
    )
    # Second turn re-reports the same key: it must be ignored, not raise.
    assert engine.observe("c1", RELEVANT) == []


def test_complex_conversation_routes_to_opus(session):
    rows = _seed(session, [f"message {i}" for i in range(15)])
    llm = FakeLLM([CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])])])
    _engine(session, llm).observe("c1", RELEVANT)
    # Long windows are the "hard" signal: escalate off Haiku.
    assert llm.calls[0].choice.model == "claude-opus-5"


def test_short_conversation_stays_on_haiku(session):
    rows = _seed(session, ["compare universes"])
    llm = FakeLLM([CrystallizerOutput(candidates=[_draft(message_ids=[rows[0].id])])])
    _engine(session, llm).observe("c1", RELEVANT)
    assert llm.calls[0].choice.model == "claude-haiku-4-5"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_crystallizer_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.crystallizer.engine'`

- [ ] **Step 3: Implement Stage B**

Create `backend/ley_khaa/crystallizer/engine.py`:

```python
from typing import Literal

from pydantic import BaseModel

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import CandidateRow
from .candidate import CandidateState
from .relevance import RelevanceVerdict

# Above this many messages in the window, the assembly problem stops being
# routine and we escalate off the cheap model.
_HARD_WINDOW = 10

SYSTEM = """You maintain the set of task candidates forming inside one work conversation.

You receive the recent messages and the candidates you previously reported. Return the
COMPLETE current set of candidates — reuse each candidate_key exactly so the caller can
match them to what it already stored.

Rules:
- A candidate owns ONLY the message ids that genuinely belong to it. Leave chatter
  unassigned; never pad a candidate with unrelated messages.
- Several unrelated requests may be interleaved. Emit one candidate per real request.
- state: "forming" when a request is only hinted at; "crystallizing" when it is taking
  shape but details are missing; "ready" when everything needed to act is present and no
  question is open.
- missing_fields: names of what is still unknown (e.g. output_format, deadline, source).
- open_question: one plain-English question to ask the human, or null. Only set it when
  the candidate is genuinely blocked."""


class CandidateDraft(BaseModel):
    candidate_key: str
    title: str
    summary: str
    message_ids: list[str]
    state: Literal["forming", "crystallizing", "ready"]
    missing_fields: list[str] = []
    open_question: str | None = None


class CrystallizerOutput(BaseModel):
    candidates: list[CandidateDraft]


class Crystallizer:
    """Stage B: the stateful candidate engine (spec §5.3)."""

    def __init__(
        self,
        llm: LLMClient,
        messages: MessageRepository,
        candidates: CandidateRepository,
        window_size: int = 30,
    ) -> None:
        self.llm = llm
        self.messages = messages
        self.candidates = candidates
        self.window_size = window_size

    def observe(self, conversation_id: str, verdict: RelevanceVerdict) -> list[CandidateRow]:
        # Stage A already decided this message is chatter — don't pay for Stage B.
        if not verdict.relevant:
            return []

        window = self.messages.window(conversation_id, limit=self.window_size)
        existing = self.candidates.list_for_conversation(conversation_id)
        complexity = "hard" if len(window) > _HARD_WINDOW else "routine"

        output = self.llm.parse(
            choice=model_for(Stage.CRYSTALLIZER, complexity),
            system=SYSTEM,
            user=_render(window, existing),
            output_format=CrystallizerOutput,
        )

        # PROMOTED/ABANDONED are terminal: the model will keep re-reporting a
        # candidate it already emitted, and resurrecting it would both raise on
        # the transition rules and double-create the task.
        terminal = {CandidateState.PROMOTED.value, CandidateState.ABANDONED.value}
        existing_by_key = {c.candidate_key: c for c in existing}

        rows = []
        for draft in output.candidates:
            prior = existing_by_key.get(draft.candidate_key)
            if prior is not None and prior.state in terminal:
                continue
            rows.append(
                self.candidates.upsert(
                    conversation_id=conversation_id,
                    candidate_key=draft.candidate_key,
                    title=draft.title,
                    summary=draft.summary,
                    state=CandidateState(draft.state),
                    message_ids=draft.message_ids,
                    missing_fields=draft.missing_fields,
                    open_question=draft.open_question,
                )
            )
        return rows


def _render(window, existing) -> str:
    lines = ["## Recent messages"]
    for row in window:
        lines.append(f"[{row.id}] {row.author}: {row.text}")
        for a in row.attachments or []:
            lines.append(f"    attachment: {a['kind']} named {a['name']}")

    lines.append("")
    lines.append("## Candidates you reported previously")
    if not existing:
        lines.append("(none yet)")
    for row in existing:
        lines.append(
            f"- {row.candidate_key} [{row.state}] {row.title} "
            f"owns={row.message_ids} missing={row.missing_fields}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_crystallizer_engine.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS, 0 warnings.

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/crystallizer/engine.py backend/tests/test_crystallizer_engine.py
git commit -m "feat: stage B stateful crystallizer engine"
```

---

### Task 7: Readiness gate (debounce)

**Files:**
- Create: `backend/ley_khaa/crystallizer/gate.py`
- Test: `backend/tests/test_readiness_gate.py`

**Interfaces:**
- Consumes: `CandidateRow` (Task 5).
- Produces: `ReadinessGate(debounce_seconds: int = 45)` with `should_emit(row: CandidateRow, *, last_message_at: datetime, now: datetime) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_readiness_gate.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.persistence.orm import CandidateRow

NOW = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)


def _cand(state="ready", missing_fields=None, open_question=None):
    return CandidateRow(
        id="c",
        conversation_id="c1",
        candidate_key="k",
        title="t",
        summary="s",
        state=state,
        message_ids=["m1"],
        missing_fields=missing_fields or [],
        open_question=open_question,
    )


@pytest.mark.parametrize("state", ["forming", "crystallizing"])
def test_unready_candidates_never_emit(state):
    gate = ReadinessGate(debounce_seconds=45)
    assert gate.should_emit(_cand(state=state), last_message_at=NOW - timedelta(minutes=5), now=NOW) is False


def test_ready_candidate_emits_after_a_conversational_pause():
    gate = ReadinessGate(debounce_seconds=45)
    assert gate.should_emit(_cand(), last_message_at=NOW - timedelta(seconds=60), now=NOW) is True


def test_ready_candidate_waits_while_the_human_is_still_typing():
    gate = ReadinessGate(debounce_seconds=45)
    assert gate.should_emit(_cand(), last_message_at=NOW - timedelta(seconds=10), now=NOW) is False


def test_missing_fields_block_emission_even_after_a_pause():
    gate = ReadinessGate(debounce_seconds=45)
    row = _cand(missing_fields=["output_format"])
    assert gate.should_emit(row, last_message_at=NOW - timedelta(minutes=5), now=NOW) is False


def test_open_question_blocks_emission():
    gate = ReadinessGate(debounce_seconds=45)
    row = _cand(open_question="Excel or CSV?")
    assert gate.should_emit(row, last_message_at=NOW - timedelta(minutes=5), now=NOW) is False


def test_exactly_at_the_threshold_emits():
    gate = ReadinessGate(debounce_seconds=45)
    assert gate.should_emit(_cand(), last_message_at=NOW - timedelta(seconds=45), now=NOW) is True


def test_zero_debounce_emits_immediately():
    gate = ReadinessGate(debounce_seconds=0)
    assert gate.should_emit(_cand(), last_message_at=NOW, now=NOW) is True


def test_naive_last_message_timestamp_is_treated_as_utc():
    # SQLite hands back naive datetimes; the gate must not crash comparing them.
    gate = ReadinessGate(debounce_seconds=45)
    naive = (NOW - timedelta(minutes=5)).replace(tzinfo=None)
    assert gate.should_emit(_cand(), last_message_at=naive, now=NOW) is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_readiness_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.crystallizer.gate'`

- [ ] **Step 3: Implement the gate**

Create `backend/ley_khaa/crystallizer/gate.py`:

```python
from datetime import datetime, timezone

from ..persistence.orm import CandidateRow
from .candidate import CandidateState


class ReadinessGate:
    """Debounce: don't fire mid-thought (spec §5.3).

    A candidate emits only when the model called it ready, nothing is missing,
    no question is open, AND the conversation has gone quiet.
    """

    def __init__(self, debounce_seconds: int = 45) -> None:
        self.debounce_seconds = debounce_seconds

    def should_emit(self, row: CandidateRow, *, last_message_at: datetime, now: datetime) -> bool:
        if row.state != CandidateState.READY.value:
            return False
        if row.missing_fields:
            return False
        if row.open_question:
            return False
        quiet_for = (now - _as_utc(last_message_at)).total_seconds()
        return quiet_for >= self.debounce_seconds


def _as_utc(value: datetime) -> datetime:
    # SQLite returns naive datetimes even for timezone=True columns.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_readiness_gate.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/ley_khaa/crystallizer/gate.py backend/tests/test_readiness_gate.py
git commit -m "feat: readiness gate debouncing candidate emission"
```

---

### Task 8: Deterministic offline LLM + orchestrator rewire

**Files:**
- Create: `backend/ley_khaa/llm/heuristic.py`
- Create: `backend/ley_khaa/llm/factory.py`
- Modify: `backend/ley_khaa/orchestrator/orchestrator.py`
- Test: `backend/tests/test_heuristic_llm.py`
- Test: `backend/tests/test_orchestrator.py` (rewrite)

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces: `HeuristicLLM()` (implements `LLMClient` with no network), `build_llm(settings) -> LLMClient`, `IntakeResult(message_id, conversation_id, candidates, task_ids)`, `Orchestrator(repo, *, llm, messages, candidates, gate)` with `ingest(raw: dict) -> IntakeResult`.

**Why a heuristic client:** the §11 definition of done is "fresh clone → `docker compose up` → dashboard live". That must hold with **no `ANTHROPIC_API_KEY` set**, and CI must stay hermetic. `HeuristicLLM` is a deterministic rule-based stand-in — not a model, just enough logic to keep the pipeline honest offline.

- [ ] **Step 1: Write the failing heuristic-LLM test**

Create `backend/tests/test_heuristic_llm.py`:

```python
from ley_khaa.crystallizer.engine import CrystallizerOutput
from ley_khaa.crystallizer.relevance import RelevanceVerdict
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.llm.router import Stage, model_for


def _judge(text, author="boss"):
    return HeuristicLLM().parse(
        choice=model_for(Stage.RELEVANCE_FILTER),
        system="s",
        user=f"author: {author}\ntext: {text}",
        output_format=RelevanceVerdict,
    )


def test_request_language_is_relevant():
    assert _judge("Can you compare the two universes and send the difference?").relevant is True


def test_greeting_is_noise():
    assert _judge("morning all").relevant is False


def test_laughter_is_noise():
    assert _judge("haha nice one").relevant is False


def test_crystallizer_output_groups_the_window_into_one_candidate():
    llm = HeuristicLLM()
    user = "## Recent messages\n[m1] boss: please compare the universes\n[m2] boss: haha\n"
    out = llm.parse(
        choice=model_for(Stage.CRYSTALLIZER),
        system="s",
        user=user,
        output_format=CrystallizerOutput,
    )
    assert isinstance(out, CrystallizerOutput)
    assert len(out.candidates) == 1
    assert out.candidates[0].message_ids == ["m1"]


def test_unknown_output_format_raises_clearly():
    import pytest
    from pydantic import BaseModel

    class Other(BaseModel):
        x: int

    with pytest.raises(NotImplementedError, match="HeuristicLLM"):
        HeuristicLLM().parse(
            choice=model_for(Stage.CRYSTALLIZER), system="s", user="u", output_format=Other
        )
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_heuristic_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.llm.heuristic'`

- [ ] **Step 3: Implement the heuristic client and factory**

Create `backend/ley_khaa/llm/heuristic.py`:

```python
import re
from typing import TypeVar

from pydantic import BaseModel

from ..crystallizer.engine import CandidateDraft, CrystallizerOutput
from ..crystallizer.relevance import RelevanceVerdict
from .router import ModelChoice

T = TypeVar("T", bound=BaseModel)

_REQUEST_WORDS = (
    "compare", "reconcile", "send", "pull", "report", "check", "build",
    "export", "summar", "difference", "missing", "list", "generate",
)
_NOISE_PATTERNS = (
    r"^\s*(hi|hey|hello|morning|thanks|thank you|ok|okay|cool|nice|lol|haha|sure)\b",
    r"^\s*\W*$",
)

_MESSAGE_LINE = re.compile(r"^\[(?P<id>[^\]]+)\]\s+(?P<author>[^:]+):\s*(?P<text>.*)$")


class HeuristicLLM:
    """Deterministic, offline stand-in for a model.

    Keeps the pipeline runnable with no API key (fresh-clone demo, CI). It is
    intentionally dumb: real quality comes from AnthropicLLM.
    """

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        if output_format is RelevanceVerdict:
            return self._relevance(user)
        if output_format is CrystallizerOutput:
            return self._crystallize(user)
        raise NotImplementedError(f"HeuristicLLM has no rule for {output_format.__name__}")

    def _relevance(self, user: str) -> RelevanceVerdict:
        text = ""
        for line in user.splitlines():
            if line.startswith("text: "):
                text = line[len("text: ") :]
        lowered = text.lower()
        if any(re.search(p, lowered) for p in _NOISE_PATTERNS):
            return RelevanceVerdict(relevant=False, topic="chatter", confidence=0.6)
        relevant = any(w in lowered for w in _REQUEST_WORDS)
        return RelevanceVerdict(
            relevant=relevant,
            topic="work-request" if relevant else "chatter",
            confidence=0.6,
        )

    def _crystallize(self, user: str) -> CrystallizerOutput:
        owned: list[str] = []
        title = "Untitled request"
        for line in user.splitlines():
            m = _MESSAGE_LINE.match(line)
            if not m:
                continue
            text = m.group("text")
            lowered = text.lower()
            if any(re.search(p, lowered) for p in _NOISE_PATTERNS):
                continue
            if not any(w in lowered for w in _REQUEST_WORDS):
                continue
            owned.append(m.group("id"))
            if title == "Untitled request":
                title = text[:80]
        if not owned:
            return CrystallizerOutput(candidates=[])
        return CrystallizerOutput(
            candidates=[
                CandidateDraft(
                    candidate_key="heuristic-1",
                    title=title,
                    summary=title,
                    message_ids=owned,
                    state="ready",
                    missing_fields=[],
                    open_question=None,
                )
            ]
        )
```

Create `backend/ley_khaa/llm/factory.py`:

```python
import os

from .client import AnthropicLLM, LLMClient
from .heuristic import HeuristicLLM


def build_llm(backend: str = "anthropic") -> LLMClient:
    """Pick the client. Falls back to the offline heuristic with no API key set,
    so a fresh clone demos without credentials."""
    if backend == "heuristic":
        return HeuristicLLM()
    if not os.getenv("ANTHROPIC_API_KEY"):
        return HeuristicLLM()
    return AnthropicLLM()
```

- [ ] **Step 4: Run the heuristic test to verify it passes**

Run: `python -m pytest tests/test_heuristic_llm.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Rewrite the orchestrator test**

Replace the whole contents of `backend/tests/test_orchestrator.py`:

```python
from datetime import datetime, timedelta, timezone

from ley_khaa.crystallizer.engine import CandidateDraft, CrystallizerOutput
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.crystallizer.relevance import RelevanceVerdict
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _orch(session, llm, debounce=0):
    return Orchestrator(
        TaskRepository(session),
        llm=llm,
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(debounce_seconds=debounce),
    )


def test_noise_message_creates_no_task(session):
    result = _orch(session, HeuristicLLM()).ingest({"text": "morning all"})
    assert result.task_ids == []
    assert TaskRepository(session).list() == []


def test_request_message_creates_one_task(session):
    result = _orch(session, HeuristicLLM()).ingest({"text": "compare the universes and send the difference"})
    assert len(result.task_ids) == 1
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.DONE.value


def test_task_owns_only_the_candidates_messages(session):
    orch = _orch(session, HeuristicLLM())
    orch.ingest({"text": "morning all"})
    result = orch.ingest({"text": "please compare the universes"})
    task = TaskRepository(session).get(result.task_ids[0])
    assert len(task.source_message_ids) == 1


def test_candidate_is_marked_promoted(session):
    result = _orch(session, HeuristicLLM()).ingest({"text": "compare the universes"})
    candidate = CandidateRepository(session).list_for_conversation("conv-1")[0]
    assert candidate.state == "promoted"
    assert candidate.task_id == result.task_ids[0]


def test_debounce_holds_a_ready_candidate_back(session):
    result = _orch(session, HeuristicLLM(), debounce=600).ingest({"text": "compare the universes"})
    assert result.task_ids == []
    assert CandidateRepository(session).list_for_conversation("conv-1")[0].state == "ready"


def test_unready_candidate_creates_no_task(session):
    llm = FakeLLM(
        [
            RelevanceVerdict(relevant=True, topic="t", confidence=0.9),
            CrystallizerOutput(
                candidates=[
                    CandidateDraft(
                        candidate_key="k",
                        title="Partial",
                        summary="s",
                        message_ids=["m1"],
                        state="crystallizing",
                        missing_fields=["output_format"],
                        open_question="Excel or CSV?",
                    )
                ]
            ),
        ]
    )
    result = _orch(session, llm).ingest({"text": "send me the differences"})
    assert result.task_ids == []
    assert result.candidates[0].open_question == "Excel or CSV?"


def test_ingest_is_idempotent_per_external_id(session):
    orch = _orch(session, HeuristicLLM())
    first = orch.ingest({"text": "compare the universes", "external_id": "slack-7"})
    second = orch.ingest({"text": "compare the universes", "external_id": "slack-7"})
    assert first.message_id == second.message_id
    assert len(MessageRepository(session).list_for_conversation("conv-1")) == 1


def test_result_reports_conversation_id(session):
    result = _orch(session, HeuristicLLM()).ingest({"text": "compare universes", "conversation_id": "c9"})
    assert result.conversation_id == "c9"
```

- [ ] **Step 6: Run it to verify it fails**

Run: `python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL with `TypeError: Orchestrator.__init__() got an unexpected keyword argument 'llm'`

- [ ] **Step 7: Rewrite the orchestrator**

Replace the whole contents of `backend/ley_khaa/orchestrator/orchestrator.py`:

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..crystallizer.engine import Crystallizer
from ..crystallizer.gate import ReadinessGate
from ..crystallizer.relevance import RelevanceFilter
from ..domain.states import TaskState
from ..intake.gateway import IntakeGateway
from ..llm.client import LLMClient
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import CandidateRow
from ..persistence.repository import TaskRepository

# Execution is still a stub: the real executor arrives in phase 0.4.0.
STUB_PATH: list[TaskState] = [
    TaskState.CLASSIFIED,
    TaskState.INTERPRETED,
    TaskState.EXECUTING,
    TaskState.VALIDATING,
    TaskState.DONE,
]


@dataclass
class IntakeResult:
    message_id: str
    conversation_id: str
    candidates: list[CandidateRow] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)


class Orchestrator:
    """intake → stage A → stage B → readiness gate → task."""

    def __init__(
        self,
        repo: TaskRepository,
        *,
        llm: LLMClient,
        messages: MessageRepository,
        candidates: CandidateRepository,
        gate: ReadinessGate | None = None,
    ) -> None:
        self.repo = repo
        self.messages = messages
        self.candidates = candidates
        self.gateway = IntakeGateway(messages)
        self.relevance = RelevanceFilter(llm)
        self.crystallizer = Crystallizer(llm, messages, candidates)
        self.gate = gate or ReadinessGate()

    def ingest(self, raw: dict) -> IntakeResult:
        row = self.gateway.accept(raw)
        verdict = self.relevance.judge(row)
        candidates = self.crystallizer.observe(row.conversation_id, verdict)

        result = IntakeResult(
            message_id=row.id,
            conversation_id=row.conversation_id,
            candidates=candidates,
        )

        last_at = self.messages.last_timestamp(row.conversation_id) or row.timestamp
        now = datetime.now(timezone.utc)
        for candidate in candidates:
            if self.gate.should_emit(candidate, last_message_at=last_at, now=now):
                result.task_ids.append(self._promote(candidate))
        return result

    def _promote(self, candidate: CandidateRow) -> str:
        task = self.repo.create(
            project="default",
            title=candidate.title,
            source_message_ids=list(candidate.message_ids),
        )
        for state in STUB_PATH:
            self.repo.update_state(task.id, state)
        self.candidates.mark_promoted(candidate.id, task_id=task.id)
        return task.id
```

- [ ] **Step 8: Run the orchestrator test to verify it passes**

Run: `python -m pytest tests/test_orchestrator.py -v`
Expected: PASS (8 tests)

- [ ] **Step 9: Commit**

```bash
git add backend/ley_khaa/llm/heuristic.py backend/ley_khaa/llm/factory.py backend/ley_khaa/orchestrator/orchestrator.py backend/tests/test_heuristic_llm.py backend/tests/test_orchestrator.py
git commit -m "feat: crystallizer pipeline in the orchestrator with offline fallback"
```

---

### Task 9: API surface, simulator, and the end-to-end integration test

**Files:**
- Modify: `backend/ley_khaa/api/schemas.py`
- Modify: `backend/ley_khaa/api/app.py`
- Create: `backend/ley_khaa/intake/simulator.py`
- Create: `backend/tests/fixtures/__init__.py`
- Create: `backend/ley_khaa/fixtures/conversations/messy_universe_check.json`
- Test: `backend/tests/test_api.py` (rewrite)
- Test: `backend/tests/test_simulator.py`

**Interfaces:**
- Consumes: `Orchestrator`, `IntakeResult`, `build_llm` (Task 8); all repositories.
- Produces: `AttachmentIn`, `MessageIn` (extended), `IntakeOut`, `CandidateOut`; `Simulator(orchestrator)` with `replay(name: str) -> list[IntakeResult]` and `available() -> list[str]`; endpoints `POST /messages`, `GET /candidates`, `POST /simulate/{name}`, `GET /conversations/{id}/messages`.

- [ ] **Step 1: Create the golden conversation fixture**

Create `backend/ley_khaa/fixtures/conversations/messy_universe_check.json`:

```json
{
  "conversation_id": "conv-universe",
  "client": "demo",
  "messages": [
    {"author": "alice", "text": "morning all"},
    {"author": "boss", "text": "hey did anyone see the game last night"},
    {"author": "alice", "text": "haha yes what a finish"},
    {"author": "boss", "text": "anyway - I need something for the 10am"},
    {"author": "boss", "text": "can you compare the Bloomberg universe against FactSet"},
    {"author": "alice", "text": "sure, which date?"},
    {"author": "boss", "text": "month end please, and send me what's missing as an Excel file"},
    {"author": "alice", "text": "cool, on it"},
    {"author": "boss", "text": "thanks!"}
  ]
}
```

- [ ] **Step 2: Write the failing simulator test**

Create `backend/tests/test_simulator.py`:

```python
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.intake.simulator import Simulator
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _sim(session):
    orch = Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(debounce_seconds=0),
    )
    return Simulator(orch)


def test_available_lists_the_golden_fixture(session):
    assert "messy_universe_check" in _sim(session).available()


def test_replay_ingests_every_message(session):
    _sim(session).replay("messy_universe_check")
    assert len(MessageRepository(session).list_for_conversation("conv-universe")) == 9


def test_replay_timestamps_are_in_the_past_and_ordered(session):
    _sim(session).replay("messy_universe_check")
    rows = MessageRepository(session).list_for_conversation("conv-universe")
    assert rows[0].timestamp < rows[-1].timestamp


def test_unknown_fixture_raises(session):
    import pytest

    with pytest.raises(FileNotFoundError):
        _sim(session).replay("no_such_conversation")


def test_messy_conversation_yields_a_task_that_excludes_the_chatter(session):
    """The headline integration test: noise in, one clean task out."""
    _sim(session).replay("messy_universe_check")
    tasks = TaskRepository(session).list()
    assert len(tasks) >= 1

    messages = {m.id: m.text for m in MessageRepository(session).list_for_conversation("conv-universe")}
    owned = [messages[mid] for mid in tasks[-1].source_message_ids]
    assert any("Bloomberg" in t for t in owned)
    assert not any("game last night" in t for t in owned)
    assert not any(t.strip().lower() == "thanks!" for t in owned)
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest tests/test_simulator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.intake.simulator'`

- [ ] **Step 4: Implement the simulator**

Create `backend/ley_khaa/intake/simulator.py`:

```python
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..orchestrator.orchestrator import IntakeResult, Orchestrator

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "conversations"


class Simulator:
    """Replays a synthetic conversation through the real intake path.

    Timestamps are backdated so the readiness gate sees a settled conversation
    rather than one still in progress.
    """

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator

    def available(self) -> list[str]:
        return sorted(p.stem for p in FIXTURES.glob("*.json"))

    def replay(self, name: str) -> list[IntakeResult]:
        path = FIXTURES / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"no conversation fixture named {name!r}")
        data = json.loads(path.read_text())

        messages = data["messages"]
        # Backdate: the last message lands 10 minutes ago.
        start = datetime.now(timezone.utc) - timedelta(minutes=10 + len(messages))
        results = []
        for i, m in enumerate(messages):
            results.append(
                self.orchestrator.ingest(
                    {
                        "source": "simulator",
                        "client": data.get("client", "demo"),
                        "conversation_id": data["conversation_id"],
                        "author": m["author"],
                        "text": m["text"],
                        "attachments": m.get("attachments", []),
                        "timestamp": (start + timedelta(minutes=i)).isoformat(),
                    }
                )
            )
        return results
```

- [ ] **Step 5: Make the gateway honor an explicit timestamp**

In `backend/ley_khaa/intake/gateway.py`, inside `accept`, build the `Message` with an explicit timestamp when one is supplied. Add before the `Message(...)` construction:

```python
        raw_ts = raw.get("timestamp")
        timestamp = datetime.fromisoformat(raw_ts) if raw_ts else datetime.now(timezone.utc)
```

Add `from datetime import datetime, timezone` to the imports, and pass `timestamp=timestamp` into the `Message(...)` call.

- [ ] **Step 6: Include the fixtures in the installed package**

In `backend/pyproject.toml`, add below `[tool.setuptools.packages.find]`:

```toml
[tool.setuptools.package-data]
ley_khaa = ["fixtures/conversations/*.json"]
```

- [ ] **Step 7: Run the simulator test to verify it passes**

Run: `python -m pytest tests/test_simulator.py -v`
Expected: PASS (5 tests)

- [ ] **Step 8: Rewrite the API test**

Replace the whole contents of `backend/tests/test_api.py`:

```python
def test_post_message_returns_intake_ack(client):
    resp = client.post("/messages", json={"text": "compare the universes and send the difference"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] == "conv-1"
    assert body["message_id"]
    assert len(body["task_ids"]) == 1


def test_post_noise_message_creates_no_task(client):
    body = client.post("/messages", json={"text": "morning all"}).json()
    assert body["task_ids"] == []
    assert client.get("/tasks").json() == []


def test_candidates_endpoint_exposes_state(client):
    client.post("/messages", json={"text": "compare the universes"})
    candidates = client.get("/candidates").json()
    assert len(candidates) == 1
    assert candidates[0]["state"] in {"ready", "promoted"}
    assert candidates[0]["conversation_id"] == "conv-1"


def test_message_with_attachment_is_accepted(client):
    resp = client.post(
        "/messages",
        json={
            "text": "compare these holdings",
            "attachments": [{"kind": "table", "name": "h.csv", "content": "a,b\n1,2"}],
        },
    )
    assert resp.status_code == 200


def test_conversation_messages_endpoint(client):
    client.post("/messages", json={"text": "compare the universes"})
    client.post("/messages", json={"text": "thanks!"})
    rows = client.get("/conversations/conv-1/messages").json()
    assert [r["text"] for r in rows] == ["compare the universes", "thanks!"]


def test_simulate_endpoint_replays_a_fixture(client):
    resp = client.post("/simulate/messy_universe_check")
    assert resp.status_code == 200
    assert resp.json()["messages_ingested"] == 9
    assert len(client.get("/tasks").json()) >= 1


def test_simulate_unknown_fixture_404(client):
    assert client.post("/simulate/nope").status_code == 404


def test_list_tasks_returns_created(client):
    client.post("/messages", json={"text": "compare the universes"})
    titles = {t["title"] for t in client.get("/tasks").json()}
    assert titles


def test_get_task_by_id(client):
    body = client.post("/messages", json={"text": "compare the universes"}).json()
    task_id = body["task_ids"][0]
    assert client.get(f"/tasks/{task_id}").json()["id"] == task_id


def test_get_missing_task_404(client):
    assert client.get("/tasks/does-not-exist").status_code == 404


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
```

- [ ] **Step 9: Run it to verify it fails**

Run: `python -m pytest tests/test_api.py -v`
Expected: FAIL — `KeyError: 'conversation_id'` / 404 on `/candidates`

- [ ] **Step 10: Extend the API schemas**

In `backend/ley_khaa/api/schemas.py`, add:

```python
class AttachmentIn(BaseModel):
    kind: str
    name: str
    content: str


class IntakeOut(BaseModel):
    message_id: str
    conversation_id: str
    candidate_ids: list[str]
    task_ids: list[str]


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    candidate_key: str
    title: str
    summary: str
    state: str
    message_ids: list[str]
    missing_fields: list[str]
    open_question: str | None
    task_id: str | None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    author: str
    text: str
    timestamp: datetime
```

And extend `MessageIn` with:

```python
    attachments: list[AttachmentIn] = []
    external_id: str | None = None
```

- [ ] **Step 11: Rewire the app**

In `backend/ley_khaa/api/app.py`:

Replace the imports of `Message` and `Orchestrator` usage with a builder. Add near the top:

```python
from ..crystallizer.gate import ReadinessGate
from ..intake.simulator import Simulator
from ..llm.factory import build_llm
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from .schemas import CandidateOut, IntakeOut, MessageIn, MessageOut, TaskOut


def build_orchestrator(session: Session) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=build_llm(settings.llm_backend),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(settings.crystallizer_debounce_seconds),
    )
```

Replace the `post_message` endpoint with:

```python
@app.post("/messages", response_model=IntakeOut)
def post_message(body: MessageIn, session: Session = Depends(get_session)) -> IntakeOut:
    result = build_orchestrator(session).ingest(body.model_dump())
    return IntakeOut(
        message_id=result.message_id,
        conversation_id=result.conversation_id,
        candidate_ids=[c.id for c in result.candidates],
        task_ids=result.task_ids,
    )
```

Add three endpoints:

```python
@app.get("/candidates", response_model=list[CandidateOut])
def list_candidates(session: Session = Depends(get_session)) -> list[CandidateOut]:
    return [CandidateOut.model_validate(c) for c in CandidateRepository(session).list_all()]


@app.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
def list_conversation_messages(
    conversation_id: str, session: Session = Depends(get_session)
) -> list[MessageOut]:
    rows = MessageRepository(session).list_for_conversation(conversation_id)
    return [MessageOut.model_validate(r) for r in rows]


@app.post("/simulate/{name}")
def simulate(name: str, session: Session = Depends(get_session)) -> dict[str, int]:
    sim = Simulator(build_orchestrator(session))
    try:
        results = sim.replay(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no such conversation fixture")
    return {
        "messages_ingested": len(results),
        "tasks_created": sum(len(r.task_ids) for r in results),
    }
```

Finally replace the seed block inside `lifespan` — instead of ingesting one synthetic `Message`, replay the golden conversation so a fresh boot shows a real crystallized task:

```python
        repo = TaskRepository(session)
        if not repo.list():
            Simulator(build_orchestrator(session)).replay("messy_universe_check")
```

Remove the now-unused `from ..domain.models import Message` import.

- [ ] **Step 12: Run the API test to verify it passes**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS (11 tests)

- [ ] **Step 13: Run the full backend suite**

Run: `python -m pytest -q`
Expected: PASS, 0 warnings.

- [ ] **Step 14: Commit**

```bash
git add backend/ley_khaa/api backend/ley_khaa/intake/simulator.py backend/ley_khaa/intake/gateway.py backend/ley_khaa/fixtures backend/pyproject.toml backend/tests/test_api.py backend/tests/test_simulator.py
git commit -m "feat: candidate and simulator API with crystallized demo seed"
```

---

### Task 10: Dashboard — candidates forming alongside tasks

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/Candidates.tsx`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/src/Candidates.test.tsx`

**Interfaces:**
- Consumes: `GET /candidates`, `GET /tasks` (Task 9).
- Produces: `Candidate` type, `fetchCandidates()`, `<Candidates items={...} />`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/Candidates.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import Candidates from "./Candidates";

const items = [
  {
    id: "c1",
    conversation_id: "conv-universe",
    candidate_key: "k1",
    title: "Universe reconciliation",
    summary: "Compare Bloomberg vs FactSet",
    state: "crystallizing",
    message_ids: ["m1", "m2"],
    missing_fields: ["output_format"],
    open_question: "Excel or CSV?",
    task_id: null,
  },
];

test("renders a candidate with its state and owned message count", () => {
  render(<Candidates items={items} />);
  expect(screen.getByText("Universe reconciliation")).toBeTruthy();
  expect(screen.getByText(/crystallizing/)).toBeTruthy();
  expect(screen.getByText(/2 messages/)).toBeTruthy();
});

test("shows the open question when the candidate is blocked", () => {
  render(<Candidates items={items} />);
  expect(screen.getByText(/Excel or CSV\?/)).toBeTruthy();
});

test("renders an empty state when nothing is forming", () => {
  render(<Candidates items={[]} />);
  expect(screen.getByText(/No candidates forming/)).toBeTruthy();
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npm test` from `frontend/`
Expected: FAIL — cannot resolve `./Candidates`

- [ ] **Step 3: Extend the API module**

Append to `frontend/src/api.ts`:

```ts
export type Candidate = {
  id: string;
  conversation_id: string;
  candidate_key: string;
  title: string;
  summary: string;
  state: string;
  message_ids: string[];
  missing_fields: string[];
  open_question: string | null;
  task_id: string | null;
};

export async function fetchCandidates(): Promise<Candidate[]> {
  const res = await fetch(`${BASE}/candidates`);
  if (!res.ok) throw new Error(`fetchCandidates failed: ${res.status}`);
  return res.json();
}
```

- [ ] **Step 4: Implement the component**

Create `frontend/src/Candidates.tsx`:

```tsx
import type { Candidate } from "./api";

const STATE_STYLES: Record<string, string> = {
  forming: "bg-gray-100 text-gray-700",
  crystallizing: "bg-amber-100 text-amber-800",
  ready: "bg-emerald-100 text-emerald-800",
  promoted: "bg-blue-100 text-blue-800",
  abandoned: "bg-gray-100 text-gray-400",
};

export default function Candidates({ items }: { items: Candidate[] }) {
  if (items.length === 0) {
    return <p className="text-sm text-gray-500">No candidates forming.</p>;
  }
  return (
    <ul className="space-y-2">
      {items.map((c) => (
        <li key={c.id} className="rounded border border-gray-200 p-3">
          <div className="flex items-center justify-between gap-3">
            <span className="font-medium">{c.title}</span>
            <span className={`rounded px-2 py-0.5 text-xs ${STATE_STYLES[c.state] ?? ""}`}>
              {c.state}
            </span>
          </div>
          <p className="mt-1 text-sm text-gray-500">
            {c.message_ids.length} messages · {c.conversation_id}
          </p>
          {c.open_question && (
            <p className="mt-1 text-sm text-amber-700">❓ {c.open_question}</p>
          )}
        </li>
      ))}
    </ul>
  );
}
```

- [ ] **Step 5: Wire it into App**

Replace `frontend/src/App.tsx`:

```tsx
import { useEffect, useState } from "react";
import Candidates from "./Candidates";
import { fetchCandidates, fetchTasks, type Candidate, type Task } from "./api";

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () => {
      fetchTasks().then(setTasks).catch((e) => setError(String(e)));
      fetchCandidates().then(setCandidates).catch((e) => setError(String(e)));
    };
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  return (
    <main className="mx-auto max-w-3xl p-8">
      <h1 className="text-2xl font-bold mb-6">ley-khaa</h1>
      {error && <p className="text-red-600">{error}</p>}

      <h2 className="text-lg font-semibold mb-2">Forming</h2>
      <Candidates items={candidates} />

      <h2 className="text-lg font-semibold mb-2 mt-8">Tasks</h2>
      <ul className="space-y-2">
        {tasks.map((t) => (
          <li key={t.id} className="rounded border border-gray-200 p-3 flex justify-between">
            <span>{t.title}</span>
            <span className="text-sm text-gray-500">
              {t.project} · {t.state}
            </span>
          </li>
        ))}
      </ul>
    </main>
  );
}
```

- [ ] **Step 6: Update the existing App test for the new fetches**

In `frontend/src/App.test.tsx`, the stub must answer both endpoints. Replace the `beforeEach` block:

```tsx
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => ({
      ok: true,
      json: async () =>
        String(url).includes("/candidates")
          ? []
          : [{ id: "t1", project: "default", state: "done", title: "compare universes" }],
    })),
  );
});
```

- [ ] **Step 7: Run the frontend tests**

Run: `npm test` from `frontend/`
Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
git add frontend/src
git commit -m "feat: dashboard panel for forming task candidates"
```

---

### Task 11: Release 0.2.0

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `backend/pyproject.toml`

- [ ] **Step 1: Verify the whole system green**

Run, and paste real output into the commit:

```bash
cd backend && python -m pytest -q
cd ../frontend && npm test
```

Expected: backend all pass / frontend 4 pass, 0 warnings.

- [ ] **Step 2: Verify the live stack**

```bash
colima start
docker compose up -d --build
curl -s localhost:8000/candidates | python3 -m json.tool
curl -s localhost:8000/tasks | python3 -m json.tool
```

Expected: the seeded messy conversation produced at least one task whose `source_message_ids` exclude the chatter. Then `docker compose down -v`.

- [ ] **Step 3: Bump the version**

In `backend/pyproject.toml`, set `version = "0.2.0"`.

- [ ] **Step 4: Update the changelog**

Add above `## [0.1.0]`:

```markdown
## [0.2.0] — 2026-08-XX
### Added
- Intake gateway: canonical multi-modal `Message` (text/table/image attachments), idempotent per external id.
- Task Crystallizer stage A — cheap per-message relevance and topic filter.
- Task Crystallizer stage B — stateful LLM candidate engine: candidates own only their own message ids, interleaved topics become separate candidates, readiness and missing-field tracking.
- Readiness gate debouncing emission until the conversation settles.
- Model Router (`model_for(stage, complexity)`) with a testable policy table and a thinking-capability flag.
- `HeuristicLLM` offline fallback so a fresh clone demos with no API key.
- Conversation simulator plus a golden messy synthetic conversation fixture.
- API: `GET /candidates`, `GET /conversations/{id}/messages`, `POST /simulate/{name}`; `POST /messages` now returns an intake ack.
- Dashboard panel showing candidates forming, with state and owned-message counts.

### Changed
- The orchestrator no longer turns every message into a task — only a `ready` candidate is promoted.
```

- [ ] **Step 5: Update the README status table**

Change the phase 1 row to `✅ shipped` and update the Status paragraph to describe crystallization rather than the walking skeleton.

- [ ] **Step 6: Commit and tag**

```bash
git add README.md CHANGELOG.md backend/pyproject.toml
git commit -m "chore: release 0.2.0 — intake and task crystallizer"
git tag -a v0.2.0 -m "0.2.0 — Intake + Task Crystallizer"
git push origin main --follow-tags
```

- [ ] **Step 7: Create the GitHub release**

```bash
gh release create v0.2.0 --title "v0.2.0 — Intake + Task Crystallizer" --notes-from-tag
```

---

## Self-Review

**Spec coverage:**
- §5.1 channel adapters — simulator only; Slack/Discord adapters are deferred to a later phase (they ingest through the same `IntakeGateway.accept` door, so no rework).
- §5.2 intake gateway — Task 3 (normalize, attachments, idempotency). Vision extraction of images is deferred to the phase that needs image content; images are stored and passed downstream as the spec requires.
- §5.3 crystallizer — Tasks 4–7: stage A (relevance), stage B (stateful candidates, ownership, boundary/readiness, interleaving), debounce, clarifying question.
- §5.13 model router — Task 1, table-driven with the vision stage present in the policy.
- §9 testing — unit tests per unit, golden conversation fixture, one end-to-end integration test (`test_messy_conversation_yields_a_task_that_excludes_the_chatter`).

**Deliberately out of scope** (later phases, and named here so an executor does not add them): project routing, amendment detection, interpreter/`TaskSpec`, autonomy engine, HITL approval, executor/sandbox, output bundles, Ollama fallback, vision extraction, Slack/Discord adapters.

**Known follow-ups carried from Phase 0** (fold into any task that touches the file): drop the redundant `import pytest as _pytest` in `conftest.py`; add an ordering assertion to `test_repository.test_get_and_list`; restore dependency-layer caching in `backend/Dockerfile`; switch `vite.config.ts` to `defineConfig` from `vitest/config`.
