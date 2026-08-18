# Phase 0 — Foundation / Walking Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up an end-to-end walking skeleton: `docker compose up` boots Postgres + a FastAPI backend + a React dashboard, and a seeded synthetic message flows through a task state machine and appears as a task in the dashboard.

**Architecture:** A FastAPI backend owns the domain (`Message`, a `Task` state machine, an `Orchestrator`) and persists tasks in Postgres via SQLAlchemy. A thin React/Vite/Tailwind dashboard polls the backend and lists tasks by state. The Orchestrator's per-task processing is a deliberate **stub** in this phase — it walks a task through the real lifecycle states (`RECEIVED → CLASSIFIED → INTERPRETED → EXECUTING → VALIDATING → DONE`) with no real logic yet. Later phases replace the stub with the crystallizer, interpreter, autonomy engine, and executor.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, psycopg 3, Postgres 16, pytest; React + TypeScript + Vite + Tailwind v4, Vitest; Docker Compose.

## Global Constraints

- **Python** `>=3.12`; **Pydantic** `v2`; **SQLAlchemy** `2.0` (typed `Mapped` style).
- **Database:** Postgres at runtime (`postgresql+psycopg://…`); **SQLite in-memory** for unit tests. Access DB only through `TaskRepository`.
- **Async note:** the foundation orchestrator is **synchronous** (FastAPI runs sync endpoints in a threadpool). True async per-project concurrency arrives in the routing/concurrency phase (`0.5.0`); keep the `Orchestrator` interface small so that swap is internal.
- **Data is synthetic only.** No real employer data, credentials, or infrastructure — ever.
- **Commits:** Conventional Commits (`feat:`, `test:`, `chore:`, `docs:`). Commit after every task.
- **Versioning:** SemVer; this phase is released as tag **`0.1.0`**.
- **Package name:** backend Python package is `ley_khaa` (underscore); repo/product name is `ley-khaa`.

---

### Task 1: Backend scaffold + health endpoint

**Files:**
- Create: `backend/pyproject.toml`
- Create: `backend/ley_khaa/__init__.py`
- Create: `backend/ley_khaa/config.py`
- Create: `backend/ley_khaa/api/__init__.py`
- Create: `backend/ley_khaa/api/app.py`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_health.py`
- Create: `backend/.gitignore`

**Interfaces:**
- Produces: FastAPI `app` importable at `ley_khaa.api.app:app`; `GET /health` → `{"status": "ok"}`. A settings object `ley_khaa.config.settings` with `.database_url: str` and `.disable_startup: bool`.

- [ ] **Step 1: Create `backend/pyproject.toml`**

```toml
[project]
name = "ley-khaa"
version = "0.1.0"
description = "AI secretary — turns conversations into validated work"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.111",
    "uvicorn[standard]>=0.30",
    "sqlalchemy>=2.0",
    "psycopg[binary]>=3.1",
    "pydantic>=2.7",
]

[project.optional-dependencies]
dev = ["pytest>=8", "httpx>=0.27"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["ley_khaa*"]
```

- [ ] **Step 2: Create `backend/.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
*.egg-info/
.pytest_cache/
*.sqlite
.env
```

- [ ] **Step 3: Create `backend/ley_khaa/__init__.py` and `backend/ley_khaa/api/__init__.py` and `backend/tests/__init__.py`** (all empty files)

- [ ] **Step 4: Create `backend/ley_khaa/config.py`**

```python
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://ley:ley@localhost:5432/leykhaa"
    )
    disable_startup: bool = os.getenv("LEY_KHAA_DISABLE_STARTUP") == "1"


settings = Settings()
```

- [ ] **Step 5: Create `backend/ley_khaa/api/app.py`** (health only for now — grown in later tasks)

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="ley-khaa")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 6: Create `backend/tests/conftest.py`** (sets env BEFORE app import; provides a `client` fixture)

```python
import os

os.environ["LEY_KHAA_DISABLE_STARTUP"] = "1"

import pytest
from fastapi.testclient import TestClient

from ley_khaa.api.app import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c
```

- [ ] **Step 7: Create `backend/tests/test_health.py`**

```python
def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 8: Install and run the test — verify it passes**

Run:
```bash
cd backend && pip install -e ".[dev]" && python -m pytest tests/test_health.py -v
```
Expected: `test_health_ok PASSED`.

- [ ] **Step 9: Commit**

```bash
git add backend
git commit -m "feat: backend scaffold with health endpoint"
```

---

### Task 2: Task state machine

**Files:**
- Create: `backend/ley_khaa/domain/__init__.py`
- Create: `backend/ley_khaa/domain/states.py`
- Create: `backend/tests/test_states.py`

**Interfaces:**
- Produces:
  - `TaskState(str, Enum)` with members `RECEIVED, CLASSIFIED, INTERPRETED, AWAITING_APPROVAL, EXECUTING, VALIDATING, NEEDS_CLARIFICATION, DONE, FAILED` (values are the lowercase names).
  - `can_transition(current: TaskState, target: TaskState) -> bool`
  - `ensure_transition(current: TaskState, target: TaskState) -> None` (raises `InvalidTransition`)
  - `InvalidTransition(Exception)`

- [ ] **Step 1: Create `backend/ley_khaa/domain/__init__.py`** (empty)

- [ ] **Step 2: Write the failing test `backend/tests/test_states.py`**

```python
import pytest

from ley_khaa.domain.states import (
    TaskState,
    can_transition,
    ensure_transition,
    InvalidTransition,
)


def test_valid_transition_allowed():
    assert can_transition(TaskState.RECEIVED, TaskState.CLASSIFIED) is True


def test_invalid_transition_rejected():
    assert can_transition(TaskState.RECEIVED, TaskState.DONE) is False


def test_terminal_states_have_no_transitions():
    assert can_transition(TaskState.DONE, TaskState.EXECUTING) is False
    assert can_transition(TaskState.FAILED, TaskState.RECEIVED) is False


def test_ensure_transition_raises_on_invalid():
    with pytest.raises(InvalidTransition):
        ensure_transition(TaskState.RECEIVED, TaskState.DONE)


def test_full_stub_path_is_valid():
    path = [
        TaskState.RECEIVED,
        TaskState.CLASSIFIED,
        TaskState.INTERPRETED,
        TaskState.EXECUTING,
        TaskState.VALIDATING,
        TaskState.DONE,
    ]
    for current, target in zip(path, path[1:]):
        assert can_transition(current, target) is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_states.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.domain.states'`.

- [ ] **Step 4: Create `backend/ley_khaa/domain/states.py`**

```python
from enum import Enum


class TaskState(str, Enum):
    RECEIVED = "received"
    CLASSIFIED = "classified"
    INTERPRETED = "interpreted"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VALIDATING = "validating"
    NEEDS_CLARIFICATION = "needs_clarification"
    DONE = "done"
    FAILED = "failed"


_ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.RECEIVED: {TaskState.CLASSIFIED, TaskState.FAILED},
    TaskState.CLASSIFIED: {TaskState.INTERPRETED, TaskState.FAILED},
    TaskState.INTERPRETED: {TaskState.AWAITING_APPROVAL, TaskState.EXECUTING, TaskState.FAILED},
    TaskState.AWAITING_APPROVAL: {TaskState.EXECUTING, TaskState.NEEDS_CLARIFICATION, TaskState.FAILED},
    TaskState.EXECUTING: {TaskState.VALIDATING, TaskState.FAILED},
    TaskState.VALIDATING: {TaskState.DONE, TaskState.NEEDS_CLARIFICATION, TaskState.FAILED},
    TaskState.NEEDS_CLARIFICATION: {
        TaskState.INTERPRETED,
        TaskState.AWAITING_APPROVAL,
        TaskState.EXECUTING,
        TaskState.FAILED,
    },
    TaskState.DONE: set(),
    TaskState.FAILED: set(),
}


class InvalidTransition(Exception):
    pass


def can_transition(current: TaskState, target: TaskState) -> bool:
    return target in _ALLOWED[current]


def ensure_transition(current: TaskState, target: TaskState) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(f"{current.value} -> {target.value} not allowed")
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_states.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/domain backend/tests/test_states.py
git commit -m "feat: task state machine with transition rules"
```

---

### Task 3: Database layer, Task ORM row, and Message model

**Files:**
- Create: `backend/ley_khaa/db.py`
- Create: `backend/ley_khaa/persistence/__init__.py`
- Create: `backend/ley_khaa/persistence/orm.py`
- Create: `backend/ley_khaa/domain/models.py`
- Create: `backend/tests/test_orm.py`

**Interfaces:**
- Produces:
  - `ley_khaa.db.Base` (SQLAlchemy `DeclarativeBase`), `ley_khaa.db.engine`, `ley_khaa.db.SessionLocal` (a `sessionmaker`), `ley_khaa.db.init_db() -> None`.
  - `ley_khaa.persistence.orm.TaskRow` with columns `id: str (pk)`, `project: str`, `state: str`, `title: str`, `source_message_ids: list`, `created_at: datetime`, `updated_at: datetime`.
  - `ley_khaa.domain.models.Message` (Pydantic) with `id, source, client, conversation_id, author, text, timestamp` (`id` and `timestamp` auto-default).

- [ ] **Step 1: Create `backend/ley_khaa/db.py`**

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    from .persistence import orm  # noqa: F401 — register models on Base

    Base.metadata.create_all(engine)
```

- [ ] **Step 2: Create `backend/ley_khaa/persistence/__init__.py`** (empty)

- [ ] **Step 3: Create `backend/ley_khaa/persistence/orm.py`**

```python
from datetime import datetime, timezone

from sqlalchemy import DateTime, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project: Mapped[str] = mapped_column(String, default="default")
    state: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, default="")
    source_message_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
```

- [ ] **Step 4: Create `backend/ley_khaa/domain/models.py`**

```python
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class Message(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str
    client: str
    conversation_id: str
    author: str
    text: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

- [ ] **Step 5: Write the test `backend/tests/test_orm.py`** (uses an in-memory SQLite session fixture, added here)

First add a `session` fixture to `backend/tests/conftest.py` (append the imports and fixture below to the existing file):

```python
import pytest as _pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ley_khaa.db import Base
from ley_khaa.persistence import orm  # noqa: F401 — register TaskRow


@_pytest.fixture
def session():
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(test_engine)
    TestingSession = sessionmaker(
        bind=test_engine, autoflush=False, expire_on_commit=False, future=True
    )
    s = TestingSession()
    try:
        yield s
    finally:
        s.close()
```

Then create `backend/tests/test_orm.py`:

```python
from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.orm import TaskRow


def test_message_autofills_id_and_timestamp():
    m = Message(source="simulator", client="demo", conversation_id="c1", author="u", text="hi")
    assert m.id
    assert m.timestamp is not None


def test_task_row_persists_and_reads_back(session):
    row = TaskRow(id="t1", project="default", state=TaskState.RECEIVED.value, title="hi", source_message_ids=["m1"])
    session.add(row)
    session.commit()
    fetched = session.get(TaskRow, "t1")
    assert fetched.state == "received"
    assert fetched.source_message_ids == ["m1"]
    assert fetched.created_at is not None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_orm.py -v`
Expected: both tests PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/ley_khaa/db.py backend/ley_khaa/persistence backend/ley_khaa/domain/models.py backend/tests/conftest.py backend/tests/test_orm.py
git commit -m "feat: db layer, Task ORM row, and Message model"
```

---

### Task 4: Task repository

**Files:**
- Create: `backend/ley_khaa/persistence/repository.py`
- Create: `backend/tests/test_repository.py`

**Interfaces:**
- Consumes: `TaskRow` (Task 3), `TaskState`/`ensure_transition` (Task 2), a SQLAlchemy `Session`.
- Produces: `TaskRepository(session)` with methods:
  - `create(*, project: str, title: str, source_message_ids: list[str]) -> TaskRow` (state defaults to `RECEIVED`)
  - `get(task_id: str) -> TaskRow | None`
  - `list() -> list[TaskRow]` (ordered by `created_at`)
  - `update_state(task_id: str, target: TaskState) -> TaskRow` (validates the transition; raises `InvalidTransition` on illegal, `KeyError` if missing)

- [ ] **Step 1: Write the failing test `backend/tests/test_repository.py`**

```python
import pytest

from ley_khaa.domain.states import TaskState, InvalidTransition
from ley_khaa.persistence.repository import TaskRepository


def test_create_starts_in_received(session):
    repo = TaskRepository(session)
    task = repo.create(project="default", title="compare universes", source_message_ids=["m1"])
    assert task.state == TaskState.RECEIVED.value
    assert task.id
    assert task.source_message_ids == ["m1"]


def test_get_and_list(session):
    repo = TaskRepository(session)
    a = repo.create(project="p", title="a", source_message_ids=[])
    b = repo.create(project="p", title="b", source_message_ids=[])
    assert repo.get(a.id).title == "a"
    assert {t.id for t in repo.list()} == {a.id, b.id}


def test_update_state_valid(session):
    repo = TaskRepository(session)
    task = repo.create(project="p", title="a", source_message_ids=[])
    updated = repo.update_state(task.id, TaskState.CLASSIFIED)
    assert updated.state == TaskState.CLASSIFIED.value


def test_update_state_invalid_raises(session):
    repo = TaskRepository(session)
    task = repo.create(project="p", title="a", source_message_ids=[])
    with pytest.raises(InvalidTransition):
        repo.update_state(task.id, TaskState.DONE)


def test_update_state_missing_raises(session):
    repo = TaskRepository(session)
    with pytest.raises(KeyError):
        repo.update_state("nope", TaskState.CLASSIFIED)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_repository.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.persistence.repository'`.

- [ ] **Step 3: Create `backend/ley_khaa/persistence/repository.py`**

```python
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..domain.states import TaskState, ensure_transition
from .orm import TaskRow


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, *, project: str, title: str, source_message_ids: list[str]) -> TaskRow:
        row = TaskRow(
            id=str(uuid.uuid4()),
            project=project,
            state=TaskState.RECEIVED.value,
            title=title,
            source_message_ids=source_message_ids,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get(self, task_id: str) -> TaskRow | None:
        return self.session.get(TaskRow, task_id)

    def list(self) -> list[TaskRow]:
        return list(self.session.scalars(select(TaskRow).order_by(TaskRow.created_at)))

    def update_state(self, task_id: str, target: TaskState) -> TaskRow:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        ensure_transition(TaskState(row.state), target)
        row.state = target.value
        self.session.commit()
        self.session.refresh(row)
        return row
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_repository.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ley_khaa/persistence/repository.py backend/tests/test_repository.py
git commit -m "feat: task repository with transition-validated updates"
```

---

### Task 5: Orchestrator (stub processing)

**Files:**
- Create: `backend/ley_khaa/orchestrator/__init__.py`
- Create: `backend/ley_khaa/orchestrator/orchestrator.py`
- Create: `backend/tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `TaskRepository` (Task 4), `Message` (Task 3), `TaskState` (Task 2).
- Produces: `Orchestrator(repo: TaskRepository)` with `ingest(message: Message) -> TaskRow`, which creates a task from the message and advances it through the stub path to `DONE`. Module constant `STUB_PATH: list[TaskState]`.

- [ ] **Step 1: Write the failing test `backend/tests/test_orchestrator.py`**

```python
from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.repository import TaskRepository


def _msg(text="Compare Bloomberg vs FactSet and send what's missing."):
    return Message(source="simulator", client="demo", conversation_id="c1", author="boss", text=text)


def test_ingest_reaches_done(session):
    orch = Orchestrator(TaskRepository(session))
    task = orch.ingest(_msg())
    assert task.state == TaskState.DONE.value


def test_ingest_records_source_message_and_title(session):
    orch = Orchestrator(TaskRepository(session))
    m = _msg("Reconcile the holdings list please")
    task = orch.ingest(m)
    assert task.source_message_ids == [m.id]
    assert task.title == "Reconcile the holdings list please"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.orchestrator.orchestrator'`.

- [ ] **Step 3: Create `backend/ley_khaa/orchestrator/__init__.py`** (empty)

- [ ] **Step 4: Create `backend/ley_khaa/orchestrator/orchestrator.py`**

```python
from ..domain.models import Message
from ..domain.states import TaskState
from ..persistence.orm import TaskRow
from ..persistence.repository import TaskRepository

# Foundation stub: walk the real lifecycle with no real logic. Later phases
# replace this with crystallizer -> interpreter -> autonomy -> executor.
STUB_PATH: list[TaskState] = [
    TaskState.CLASSIFIED,
    TaskState.INTERPRETED,
    TaskState.EXECUTING,
    TaskState.VALIDATING,
    TaskState.DONE,
]


class Orchestrator:
    def __init__(self, repo: TaskRepository) -> None:
        self.repo = repo

    def ingest(self, message: Message) -> TaskRow:
        task = self.repo.create(
            project="default",
            title=message.text[:80],
            source_message_ids=[message.id],
        )
        for state in STUB_PATH:
            self.repo.update_state(task.id, state)
        return self.repo.get(task.id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_orchestrator.py -v`
Expected: both tests PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/orchestrator backend/tests/test_orchestrator.py
git commit -m "feat: orchestrator with stub lifecycle processing"
```

---

### Task 6: API endpoints (messages + tasks)

**Files:**
- Create: `backend/ley_khaa/api/schemas.py`
- Modify: `backend/ley_khaa/api/app.py`
- Create: `backend/tests/test_api.py`
- Modify: `backend/tests/conftest.py` (override `get_session` in the `client` fixture)

**Interfaces:**
- Consumes: `Orchestrator`, `TaskRepository`, `Message`, `SessionLocal`.
- Produces:
  - `get_session()` FastAPI dependency (yields a `Session`).
  - `POST /messages` (body `MessageIn`) → `TaskOut` (the processed task).
  - `GET /tasks` → `list[TaskOut]`.
  - `GET /tasks/{task_id}` → `TaskOut` or `404`.
  - `MessageIn` fields: `source="simulator", client="demo", conversation_id="conv-1", author="user", text: str`.
  - `TaskOut` fields mirror `TaskRow` (`from_attributes=True`).

- [ ] **Step 1: Create `backend/ley_khaa/api/schemas.py`**

```python
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MessageIn(BaseModel):
    source: str = "simulator"
    client: str = "demo"
    conversation_id: str = "conv-1"
    author: str = "user"
    text: str


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project: str
    state: str
    title: str
    source_message_ids: list[str]
    created_at: datetime
    updated_at: datetime
```

- [ ] **Step 2: Replace `backend/ley_khaa/api/app.py` with the full app**

```python
from collections.abc import Iterator

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, init_db
from ..domain.models import Message
from ..orchestrator.orchestrator import Orchestrator
from ..persistence.repository import TaskRepository
from .schemas import MessageIn, TaskOut

app = FastAPI(title="ley-khaa")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/messages", response_model=TaskOut)
def post_message(body: MessageIn, session: Session = Depends(get_session)) -> TaskOut:
    orch = Orchestrator(TaskRepository(session))
    task = orch.ingest(Message(**body.model_dump()))
    return TaskOut.model_validate(task)


@app.get("/tasks", response_model=list[TaskOut])
def list_tasks(session: Session = Depends(get_session)) -> list[TaskOut]:
    return [TaskOut.model_validate(t) for t in TaskRepository(session).list()]


@app.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: str, session: Session = Depends(get_session)) -> TaskOut:
    row = TaskRepository(session).get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskOut.model_validate(row)


@app.on_event("startup")
def _startup() -> None:
    if settings.disable_startup:
        return
    init_db()
    session = SessionLocal()
    try:
        repo = TaskRepository(session)
        if not repo.list():
            Orchestrator(repo).ingest(
                Message(
                    source="simulator",
                    client="demo",
                    conversation_id="conv-seed",
                    author="boss",
                    text="Compare the Bloomberg universe against FactSet and send me what's missing.",
                )
            )
    finally:
        session.close()
```

- [ ] **Step 3: Update the `client` fixture in `backend/tests/conftest.py`** so requests use the in-memory `session`

Replace the existing `client` fixture body with this version (it overrides `get_session` to reuse the test `session`):

```python
@pytest.fixture
def client(session):
    from ley_khaa.api.app import app, get_session

    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
```

- [ ] **Step 4: Write the test `backend/tests/test_api.py`**

```python
def test_post_message_creates_done_task(client):
    resp = client.post("/messages", json={"text": "Reconcile the holdings list"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "done"
    assert body["title"] == "Reconcile the holdings list"


def test_list_tasks_returns_created(client):
    client.post("/messages", json={"text": "task one"})
    client.post("/messages", json={"text": "task two"})
    resp = client.get("/tasks")
    assert resp.status_code == 200
    titles = {t["title"] for t in resp.json()}
    assert {"task one", "task two"} <= titles


def test_get_task_by_id(client):
    created = client.post("/messages", json={"text": "fetch me"}).json()
    resp = client.get(f"/tasks/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_missing_task_404(client):
    assert client.get("/tasks/nope").status_code == 404
```

- [ ] **Step 5: Run the full backend test suite**

Run: `cd backend && python -m pytest -v`
Expected: every test PASSES (health, states, orm, repository, orchestrator, api).

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/api backend/tests/conftest.py backend/tests/test_api.py
git commit -m "feat: message and task API endpoints with startup seed"
```

---

### Task 7: Frontend dashboard shell

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/vite.config.ts`
- Create: `frontend/tsconfig.json`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/index.css`
- Create: `frontend/src/api.ts`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/App.test.tsx`
- Create: `frontend/.gitignore`

**Interfaces:**
- Consumes: backend `GET /tasks` returning `TaskOut[]`.
- Produces: a dashboard that fetches tasks from `import.meta.env.VITE_API_URL` and lists each task's `title`, `state`, and `project`. `src/api.ts` exports `fetchTasks(): Promise<Task[]>` and the `Task` type.

- [ ] **Step 1: Create `frontend/.gitignore`**

```gitignore
node_modules/
dist/
*.local
```

- [ ] **Step 2: Create `frontend/package.json`**

```json
{
  "name": "ley-khaa-frontend",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "test": "vitest run"
  },
  "dependencies": {
    "react": "^18.3.0",
    "react-dom": "^18.3.0"
  },
  "devDependencies": {
    "@tailwindcss/vite": "^4.0.0",
    "@testing-library/react": "^16.0.0",
    "@types/react": "^18.3.0",
    "@types/react-dom": "^18.3.0",
    "@vitejs/plugin-react": "^4.3.0",
    "jsdom": "^25.0.0",
    "tailwindcss": "^4.0.0",
    "typescript": "^5.5.0",
    "vite": "^5.4.0",
    "vitest": "^2.1.0"
  }
}
```

- [ ] **Step 3: Create `frontend/vite.config.ts`**

```ts
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173 },
  test: { environment: "jsdom" },
});
```

- [ ] **Step 4: Create `frontend/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["ES2020", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "skipLibCheck": true,
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "types": ["vitest/globals"]
  },
  "include": ["src"]
}
```

- [ ] **Step 5: Create `frontend/index.html`**

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>ley-khaa</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

- [ ] **Step 6: Create `frontend/src/index.css`**

```css
@import "tailwindcss";
```

- [ ] **Step 7: Create `frontend/src/main.tsx`**

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
```

- [ ] **Step 8: Create `frontend/src/api.ts`**

```ts
export type Task = {
  id: string;
  project: string;
  state: string;
  title: string;
};

const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export async function fetchTasks(): Promise<Task[]> {
  const res = await fetch(`${BASE}/tasks`);
  if (!res.ok) throw new Error(`fetchTasks failed: ${res.status}`);
  return res.json();
}
```

- [ ] **Step 9: Create `frontend/src/App.tsx`**

```tsx
import { useEffect, useState } from "react";
import { fetchTasks, type Task } from "./api";

export default function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchTasks().then(setTasks).catch((e) => setError(String(e)));
  }, []);

  return (
    <main className="mx-auto max-w-3xl p-8">
      <h1 className="text-2xl font-bold mb-6">ley-khaa · tasks</h1>
      {error && <p className="text-red-600">{error}</p>}
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

- [ ] **Step 10: Create the failing test `frontend/src/App.test.tsx`**

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import App from "./App";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok: true,
      json: async () => [
        { id: "t1", project: "default", state: "done", title: "compare universes" },
      ],
    })),
  );
});

test("renders tasks from the API", async () => {
  render(<App />);
  await waitFor(() => expect(screen.getByText("compare universes")).toBeTruthy());
  expect(screen.getByText(/done/)).toBeTruthy();
});
```

- [ ] **Step 11: Install deps, run the test — verify it passes**

Run:
```bash
cd frontend && npm install && npm test
```
Expected: `renders tasks from the API` PASSES.

- [ ] **Step 12: Commit**

```bash
git add frontend
git commit -m "feat: react dashboard shell listing tasks"
```

---

### Task 8: Docker Compose, repo hygiene, and CI

**Files:**
- Create: `backend/Dockerfile`
- Create: `frontend/Dockerfile`
- Create: `docker-compose.yml`
- Create: `README.md`
- Create: `LICENSE`
- Create: `CONTRIBUTING.md`
- Create: `CHANGELOG.md`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: `docker compose up` boots `db` (Postgres), `backend` (FastAPI on :8000, seeds a demo task on startup), and `frontend` (Vite on :5173). CI runs the backend test suite on push.

- [ ] **Step 1: Create `backend/Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY . .
CMD ["uvicorn", "ley_khaa.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Create `frontend/Dockerfile`**

```dockerfile
FROM node:20-slim
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
```

- [ ] **Step 3: Create `docker-compose.yml`**

```yaml
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_USER: ley
      POSTGRES_PASSWORD: ley
      POSTGRES_DB: leykhaa
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ley"]
      interval: 5s
      timeout: 5s
      retries: 5

  backend:
    build: ./backend
    environment:
      DATABASE_URL: postgresql+psycopg://ley:ley@db:5432/leykhaa
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy

  frontend:
    build: ./frontend
    environment:
      VITE_API_URL: http://localhost:8000
    ports:
      - "5173:5173"
    depends_on:
      - backend
```

- [ ] **Step 4: Create `README.md`**

```markdown
# ley-khaa (เลขา)

> Your AI secretary — turns the way people actually talk into finished, validated work.

**Positioning:** a personal automation layer for developers. It sits *in front of* coding agents
(Claude Code, Codex) and does the toil of reading messy conversations and prompt-engineering them
into well-structured tasks. See `docs/superpowers/specs/2026-08-18-ley-khaa-design.md`.

> ⚠️ All datasets and communications in this project are **synthetic**. It connects to no real
> employer data, credentials, or infrastructure.

## Status

**v0.1.0 — Foundation / walking skeleton.** A seeded synthetic message flows through a task state
machine and appears in the dashboard. Intelligence (crystallizer, interpreter, autonomy, executor)
arrives in later phases — see `docs/superpowers/plans/`.

## Run

```bash
docker compose up
```

- Dashboard: http://localhost:5173
- API:       http://localhost:8000  (`/health`, `/tasks`, `/tasks/{id}`, `POST /messages`)

## Develop

```bash
cd backend && pip install -e ".[dev]" && python -m pytest -v
cd frontend && npm install && npm test
```
```

- [ ] **Step 5: Create `LICENSE`** (MIT — replace `<YEAR>`/`<AUTHOR>`)

```text
MIT License

Copyright (c) 2026 ruttantai

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 6: Create `CONTRIBUTING.md`**

```markdown
# Contributing

## Conventions
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org) — `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
- **Branches (v1, solo):** work on `main`, keep it green. As the project scales: short-lived `feature/<slug>` branches merged via PR.
- **Versioning:** SemVer, git tags (`0.1.0`, …, `1.0.0`).

## Tests
- Backend: `cd backend && python -m pytest -v`
- Frontend: `cd frontend && npm test`

All data used in development is synthetic.
```

- [ ] **Step 7: Create `CHANGELOG.md`**

```markdown
# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com); versioning is [SemVer](https://semver.org).

## [Unreleased]

## [0.1.0] — 2026-08-18
### Added
- Foundation walking skeleton: FastAPI backend, task state machine, orchestrator (stub), Task API.
- React/Vite/Tailwind dashboard listing tasks.
- Docker Compose (Postgres + backend + frontend) with startup seed of a synthetic demo task.
- Repo hygiene: README, LICENSE, CONTRIBUTING, CHANGELOG, CI.
```

- [ ] **Step 8: Create `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  backend-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
        working-directory: backend
      - run: python -m pytest -v
        working-directory: backend
```

- [ ] **Step 9: Verify the whole stack boots end-to-end**

Run:
```bash
docker compose up -d --build
# wait for backend health, then confirm the seeded task exists
until curl -sf http://localhost:8000/health; do sleep 2; done
curl -s http://localhost:8000/tasks
```
Expected: `/health` returns `{"status":"ok"}`; `/tasks` returns a JSON array containing one task whose `state` is `"done"` and whose `title` starts with `"Compare the Bloomberg universe"`. Open http://localhost:5173 and confirm that task is listed. Then `docker compose down`.

- [ ] **Step 10: Commit**

```bash
git add Dockerfile* docker-compose.yml README.md LICENSE CONTRIBUTING.md CHANGELOG.md .github backend/Dockerfile frontend/Dockerfile
git commit -m "chore: docker compose, repo hygiene, and CI"
```

---

### Task 9: Tag the release

**Files:** none (git tag only).

- [ ] **Step 1: Confirm both test suites pass**

Run: `cd backend && python -m pytest -v && cd ../frontend && npm test`
Expected: all backend and frontend tests PASS.

- [ ] **Step 2: Tag `0.1.0`**

```bash
git tag -a v0.1.0 -m "Foundation / walking skeleton"
```

Expected: `git tag` lists `v0.1.0`. (Push with `git push --tags` once a remote exists.)

---

## Self-Review

**Spec coverage (foundation-relevant items):**
- Canonical `Message` model → Task 3. ✅
- Task state machine matching spec §5.9 lifecycle → Task 2. ✅
- Orchestrator per-task processing (stub for now) → Task 5. ✅
- Postgres persistence via SQLAlchemy, SQLite for tests → Tasks 3–4. ✅
- Task API + dashboard listing tasks by project/state → Tasks 6–7. ✅
- `docker compose up`, seeded, dashboard alive on first load → Task 8. ✅
- Repo hygiene (README/LICENSE/CONTRIBUTING/CHANGELOG/CI) + SemVer tag → Tasks 8–9. ✅
- Synthetic-only data → seed message + README note. ✅

**Deliberately deferred to later phases (not gaps):** channel adapters, crystallizer, interpreter, autonomy engine, HITL controls, synthesis/sandbox executor, registry, Output Bundles, model router, multi-modal intake, multi-project routing/concurrency, task memory. Each is a later phase plan (`0.2.0`+).

**Type consistency:** `TaskState` values are lowercase strings; `TaskRow.state`/`TaskOut.state` are those strings; `TaskRepository` method names (`create`, `get`, `list`, `update_state`) are used consistently in Tasks 5–6; `fetchTasks`/`Task` used consistently in Tasks 7. No mismatches found.

**Placeholder scan:** none — every step contains full file contents or exact commands.
