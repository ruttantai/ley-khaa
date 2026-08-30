# Phase 5 — Project routing, concurrent queues and amendments: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every task to a real project, run projects' queues concurrently on leased
background workers, and fold a mid-flight follow-up into the task it amends instead of spawning a
duplicate.

**Architecture:** `TaskDriver.advance()` is untouched — the phase changes only *who calls it*. A
lease on `TaskRow` turns the tasks table itself into the queue; a `Dispatcher` runs one worker per
project. A two-stage `ProjectRouter` (free binding lookup, then one Haiku call that writes a
binding on success) decides the project. An `AmendmentDetector` finds follow-ups to active tasks,
and the autonomy dial decides whether they fold — with a structural guard that always parks a
target already at `EXECUTING` or `VALIDATING`.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, pytest; React 18 +
Vite + Tailwind + vitest on the frontend.

**Spec:** `docs/superpowers/specs/2026-08-28-phase-5-routing-queues-amendments-design.md`

## Global Constraints

- **Never send `thinking` or `output_config.effort` to Haiku 4.5** — it is a 400. `ModelChoice`
  already carries `supports_thinking`; route new stages through `model_for(...)` and never
  hand-build kwargs.
- **Every new LLM call needs a `HeuristicLLM` rule.** `HeuristicLLM.parse` raises
  `NotImplementedError` for an unknown `output_format`, and CI plus `docker compose up` run with no
  `ANTHROPIC_API_KEY`. A new structured output with no offline rule turns the fresh-clone demo red.
- **`Settings` is `@dataclass(frozen=True)`** (a Phase 0 invariant). Tests must not unfreeze it —
  pin settings by rebinding the module-level `settings` name to a `dataclasses.replace(...)` copy.
- **`server_default=""` must be written `server_default=text("''")`.** Alembic's SQLite comparator
  cannot strip quotes from a zero-length default literal, so a bare `""` false-positives the
  migration drift guard. The two forms are DDL-identical.
- **`conftest.py` builds the test schema with `Base.metadata.create_all()`, not Alembic.** ORM and
  migration must agree or `tests/test_migrations.py`'s drift guard fails — and since
  `compare_server_default` is now on, a missing `server_default` on either side is a real failure.
- **Local docker-parametrized runs need `TMPDIR` under `$HOME`** (this Mac runs Colima, which
  mounts only `$HOME`). `mkdir -p "$HOME/tmp"` first — the directory must exist, or the shell hands
  pytest a path it silently falls back from, and the docker tests fail with a misleading
  "can't open file .../generator/attempt_1.py".
- **Run the backend suite as:** `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
  Baseline before this phase: **532 passed, 0 skipped, 0 warnings.**
- **Frontend:** `cd frontend && npm test` and `npx tsc --noEmit -p tsconfig.json`. Baseline: 37
  tests, typecheck clean.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`).

## The testing discipline this phase is held to

Phase 4 produced **eight separate findings of tests that passed for the wrong reason**. For every
new assertion in this plan:

> **Delete the behaviour the assertion guards, run the test, watch it fail for the RIGHT reason,
> then restore the behaviour.**

Not "it passes". Not "I mutation-tested it" — that self-report was wrong more than once in Phase 4.
Where a task adds two guards, each needs its own test, or one can be deleted later in silence.

## File Structure

**Created:**

| file | responsibility |
|---|---|
| `backend/ley_khaa/alembic/versions/0005_routing_queues.py` | the whole phase's schema, one revision |
| `backend/ley_khaa/projects/__init__.py` | package marker |
| `backend/ley_khaa/projects/models.py` | `RoutingDecision`, `ProjectChoice` (the stage-2 output) |
| `backend/ley_khaa/projects/router.py` | `ProjectRouter` — two stages and the learning rule |
| `backend/ley_khaa/projects/seeds.py` | `ensure_default_project` |
| `backend/ley_khaa/persistence/project_repository.py` | `ProjectRepository` — projects and bindings |
| `backend/ley_khaa/orchestrator/dispatcher.py` | `Dispatcher` — one worker per project, leases |
| `backend/ley_khaa/orchestrator/amendment.py` | `AmendmentDetector`, `AmendmentProposal` |
| `frontend/src/Projects.tsx` | per-project queue columns |
| `frontend/src/Triage.tsx` | parked amendment proposals with fold / separate |

**Modified:**

| file | change |
|---|---|
| `backend/ley_khaa/persistence/orm.py` | `ProjectRow`, `ProjectBindingRow`; lease + amendment columns |
| `backend/ley_khaa/persistence/repository.py` | lease claims, runnable queries, `fold_into` |
| `backend/ley_khaa/persistence/candidate_repository.py` | `claim_for_triage`, `claim_for_fold` |
| `backend/ley_khaa/persistence/workflow_repository.py` | atomic counters (backlog item 5) |
| `backend/ley_khaa/crystallizer/candidate.py` | `AWAITING_TRIAGE` state + edges |
| `backend/ley_khaa/domain/states.py` | two new edges into `CLASSIFIED`; `WAITING` moved here |
| `backend/ley_khaa/orchestrator/driver.py` | import `WAITING`; write-after-claim (backlog item 6) |
| `backend/ley_khaa/orchestrator/orchestrator.py` | route, detect, fold, enqueue instead of drive |
| `backend/ley_khaa/autonomy/engine.py` | `recommend_fold` + the structural guard |
| `backend/ley_khaa/llm/router.py` | `PROJECT_ROUTE`, `AMENDMENT_MATCH` stages |
| `backend/ley_khaa/llm/heuristic.py` | offline rules for both new outputs |
| `backend/ley_khaa/config.py` | dispatch mode, lease and concurrency settings |
| `backend/ley_khaa/api/app.py` | projects/triage endpoints; dispatcher in `lifespan` |
| `backend/ley_khaa/api/schemas.py` | `ProjectOut`, `TriageOut`, `ProjectIn` |
| `backend/tests/conftest.py` | pin `LEY_KHAA_DISPATCH=inline` |
| `frontend/src/api.ts`, `App.tsx` | new types and calls; mount the two new views |

---

## Prerequisite

**PR #6 must be merged before this plan is executed.** It carries the `compare_server_default`
drift-guard fix that three of Task 1's new columns depend on, and the Phase 5 backlog file this
plan's items 4/5/6 argue from. Verify with:

```bash
git log --oneline -1 --grep="compare_server_default" && \
  test -f docs/superpowers/specs/2026-08-28-phase-5-backlog.md && echo PREREQ OK
```

---

### Task 1: Schema — projects, bindings, leases, amendment columns

One migration for the whole phase, so there is one revision to reason about and one drift check to
satisfy. Later tasks add behaviour only.

**Files:**
- Create: `backend/ley_khaa/alembic/versions/0005_routing_queues.py`
- Modify: `backend/ley_khaa/persistence/orm.py`
- Test: `backend/tests/test_migrations.py` (existing drift guard), `backend/tests/test_orm_phase5.py`

**Interfaces:**
- Produces: `ProjectRow(name, display_name, description, active, created_at)`;
  `ProjectBindingRow(id, source, client, conversation_id, project, created_by_stage, created_at)`;
  `TaskRow.lease_owner / lease_expires_at / lease_attempts`;
  `CandidateRow.amends_task_id / amendment_reason / amendment_confidence`.

**Two traps this task exists to avoid — read before writing code:**

1. **`conversation_id` is `""`, never NULL.** A binding with no conversation binds the whole
   client. Modelling that as NULL breaks the unique constraint, because SQL treats NULLs as
   distinct — two client-wide bindings for the same client would both insert, and
   most-specific-wins would then depend on row order. `""` makes the constraint real.
2. **`server_default=text("''")`, never `server_default=""`.** Alembic's SQLite comparator cannot
   strip quotes from a zero-length default literal and reports permanent false drift. The two are
   DDL-identical.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_orm_phase5.py
import pytest
from sqlalchemy.exc import IntegrityError

from ley_khaa.persistence.orm import CandidateRow, ProjectBindingRow, ProjectRow, TaskRow


def test_a_project_stores_the_description_stage_two_reasons_over(session):
    session.add(ProjectRow(name="acme", display_name="Acme", description="Acme's equity books"))
    session.commit()
    row = session.get(ProjectRow, "acme")
    assert row.description == "Acme's equity books"
    assert row.active is True


def test_two_client_wide_bindings_for_one_client_cannot_both_exist(session):
    """The whole point of conversation_id="" rather than NULL.

    With NULL here, SQL treats the two rows as distinct and both insert, so
    "most specific wins" would silently become "whichever row came back first".
    """
    session.add(
        ProjectBindingRow(
            id="b1", source="slack", client="acme", conversation_id="", project="acme"
        )
    )
    session.commit()
    session.add(
        ProjectBindingRow(
            id="b2", source="slack", client="acme", conversation_id="", project="other"
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_conversation_binding_and_a_client_binding_coexist(session):
    session.add(
        ProjectBindingRow(
            id="b1", source="slack", client="acme", conversation_id="", project="acme"
        )
    )
    session.add(
        ProjectBindingRow(
            id="b2", source="slack", client="acme", conversation_id="C9", project="special"
        )
    )
    session.commit()
    assert session.query(ProjectBindingRow).count() == 2


def test_a_new_task_starts_with_no_lease_and_no_attempts(session):
    row = TaskRow(id="t1", project="default", state="received", title="x", source_message_ids=[])
    session.add(row)
    session.commit()
    assert row.lease_owner is None
    assert row.lease_expires_at is None
    assert row.lease_attempts == 0


def test_a_candidate_starts_with_no_amendment_proposal(session):
    row = CandidateRow(
        id="c1", conversation_id="C1", candidate_key="k", state="ready", message_ids=[]
    )
    session.add(row)
    session.commit()
    assert row.amends_task_id is None
    assert row.amendment_confidence is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_orm_phase5.py -q`
Expected: FAIL — `ImportError: cannot import name 'ProjectRow'`.

- [ ] **Step 3: Add the ORM rows and columns**

```python
# backend/ley_khaa/persistence/orm.py — add `text` to the sqlalchemy import
from sqlalchemy import (
    Boolean, DateTime, Float, Integer, JSON, String, UniqueConstraint, text,
)
```

Add to `TaskRow`, after `familiarity`:

```python
    # --- the lease that makes this table the queue (spec §3.2) --------------
    # A worker holds a task by writing its id here with an expiry it heartbeats.
    # Only an EXPIRED lease is reclaimable, which is what distinguishes "a
    # worker is busy with this" from "the worker holding this died".
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Counts RECLAIMS of an expired lease, never ordinary claims — see
    # TaskRepository.claim_lease. A task driven normally ends its life at 0.
    lease_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
```

Add to `CandidateRow`, after `task_id`:

```python
    # --- a parked amendment proposal (spec §3.9) ---------------------------
    # Set only while this candidate sits in AWAITING_TRIAGE. amends_task_id is
    # the task the detector thinks this request modifies; the reason is the
    # model's own sentence, shown to the human who decides.
    amends_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    amendment_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    amendment_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
```

Add the two new tables at the end of the file:

```python
class ProjectRow(Base):
    """A workstream tasks are routed into (spec §5.4).

    `description` is not decoration: it is the only thing stage-2 routing has to
    reason over, so a project with an empty description is unroutable by the
    model and reachable only by an explicit binding. POST /projects says so.
    """

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    display_name: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    description: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProjectBindingRow(Base):
    """Which project a channel, a client, or one conversation belongs to.

    conversation_id is "" — NOT NULL — when the binding covers a whole client.
    SQL treats NULLs as distinct, so a nullable column here would let two
    client-wide bindings for the same client both exist and turn
    "most specific wins" into "whichever row the database returned first".
    """

    __tablename__ = "project_bindings"
    __table_args__ = (
        UniqueConstraint("source", "client", "conversation_id", name="uq_binding_scope"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, index=True)
    client: Mapped[str] = mapped_column(String, index=True)
    conversation_id: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    project: Mapped[str] = mapped_column(String, index=True)
    # "seed" | "manual" | "model" — the last is what the learning rule writes,
    # and it is what makes a stage-2 decision auditable after the fact.
    created_by_stage: Mapped[str] = mapped_column(String, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
```

- [ ] **Step 4: Run the ORM tests — they pass, and the drift guard now fails**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_orm_phase5.py tests/test_migrations.py -q`
Expected: the five ORM tests PASS; `test_migrations_match_the_models` FAILS with a diff naming
`projects`, `project_bindings` and the six new columns. That failure is the point — it is the guard
doing its job.

- [ ] **Step 5: Write migration 0005**

```python
# backend/ley_khaa/alembic/versions/0005_routing_queues.py
"""phase 5: project routing, task leases and amendment proposals

Revision ID: 0005_routing_queues
Revises: 0004_registry_memory

Creates the projects and project_bindings TABLES. It does not seed the default
project row — startup does that (projects/seeds.py::ensure_default_project),
the same division the seed workflows use. Saying otherwise here would be the
false-statement class of defect that commit 8cebd1f cleaned up.
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_routing_queues"
down_revision = "0004_registry_memory"
branch_labels = None
depends_on = None

_TASK_COLUMNS = [
    sa.Column("lease_owner", sa.String(), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("lease_attempts", sa.Integer(), nullable=False, server_default="0"),
]

_CANDIDATE_COLUMNS = [
    sa.Column("amends_task_id", sa.String(), nullable=True),
    sa.Column("amendment_reason", sa.String(), nullable=True),
    sa.Column("amendment_confidence", sa.Float(), nullable=True),
]


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("display_name", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("description", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "project_bindings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("project", sa.String(), nullable=False),
        sa.Column("created_by_stage", sa.String(), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "client", "conversation_id", name="uq_binding_scope"),
    )
    op.create_index("ix_project_bindings_source", "project_bindings", ["source"])
    op.create_index("ix_project_bindings_client", "project_bindings", ["client"])
    op.create_index("ix_project_bindings_project", "project_bindings", ["project"])

    for column in _TASK_COLUMNS:
        op.add_column("tasks", column)
    for column in _CANDIDATE_COLUMNS:
        op.add_column("task_candidates", column)


def downgrade() -> None:
    # LIFO, matching 0002-0004: drop what this revision added, newest first.
    for column in reversed(_CANDIDATE_COLUMNS):
        op.drop_column("task_candidates", column.name)
    for column in reversed(_TASK_COLUMNS):
        op.drop_column("tasks", column.name)
    op.drop_index("ix_project_bindings_project", table_name="project_bindings")
    op.drop_index("ix_project_bindings_client", table_name="project_bindings")
    op.drop_index("ix_project_bindings_source", table_name="project_bindings")
    op.drop_table("project_bindings")
    op.drop_table("projects")
```

- [ ] **Step 6: Run the drift guard — it must go green**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_migrations.py -q`
Expected: PASS.

If `test_migrations_match_the_models` still reports drift on `display_name`, `description`,
`conversation_id` or `created_by_stage`, the cause is almost certainly a bare `""` server default
somewhere — go back and make it `sa.text("''")` / `text("''")` on **both** sides.

- [ ] **Step 7: Prove the new tests fail for the right reason**

For each of the five tests in `test_orm_phase5.py`, delete the behaviour it guards, confirm the
failure names that behaviour, restore. Specifically:
- Remove `UniqueConstraint` from `ProjectBindingRow.__table_args__` →
  `test_two_client_wide_bindings_for_one_client_cannot_both_exist` must fail on the missing
  `IntegrityError`, **not** on an import error.
- Change `conversation_id` to `nullable=True` with `default=None` and insert `None` in that test →
  it must fail too. This is the trap the column shape exists to close; if the test still passes,
  the test is not testing it.
- Drop `server_default="0"` from `lease_attempts` → the drift guard must fail.

- [ ] **Step 8: Run the whole suite**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 537 passed (532 baseline + 5 new), 0 skipped, 0 warnings.

- [ ] **Step 9: Commit**

```bash
git add backend/ley_khaa/persistence/orm.py \
        backend/ley_khaa/alembic/versions/0005_routing_queues.py \
        backend/tests/test_orm_phase5.py
git commit -m "feat(persistence): add projects, bindings, task leases and amendment columns"
```

---

### Task 2: `ProjectRepository` — projects and most-specific-wins bindings

**Files:**
- Create: `backend/ley_khaa/persistence/project_repository.py`
- Test: `backend/tests/test_project_repository.py`

**Interfaces:**
- Consumes: `ProjectRow`, `ProjectBindingRow` (Task 1).
- Produces:
  - `ProjectRepository(session)`
  - `.create(name, *, display_name="", description="") -> ProjectRow`
  - `.get(name) -> ProjectRow | None`
  - `.active() -> list[ProjectRow]`
  - `.binding_for(source, client, conversation_id) -> ProjectBindingRow | None`
  - `.bind(source, client, conversation_id, project, *, stage) -> ProjectBindingRow`
  - `DEFAULT_PROJECT = "default"`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_project_repository.py
from ley_khaa.persistence.project_repository import DEFAULT_PROJECT, ProjectRepository


def test_a_conversation_binding_beats_a_client_wide_one(session):
    """Most-specific-wins, stated as a test rather than trusted to row order."""
    repo = ProjectRepository(session)
    repo.create("acme")
    repo.create("special")
    repo.bind("slack", "acme", "", "acme", stage="manual")
    repo.bind("slack", "acme", "C9", "special", stage="manual")

    assert repo.binding_for("slack", "acme", "C9").project == "special"
    assert repo.binding_for("slack", "acme", "C1").project == "acme"


def test_no_binding_at_all_is_a_miss_not_a_default(session):
    """The repository reports absence; deciding what absence MEANS is the
    router's job. Returning DEFAULT_PROJECT here would hide stage-2 misses."""
    repo = ProjectRepository(session)
    assert repo.binding_for("slack", "nobody", "C1") is None


def test_binding_is_idempotent_and_rebinds_rather_than_duplicating(session):
    """The learning rule can fire twice for one conversation if two workers
    race it. The second call must move the binding, not raise on the unique
    constraint and not leave two rows."""
    repo = ProjectRepository(session)
    repo.create("acme")
    repo.create("other")
    repo.bind("slack", "acme", "C9", "acme", stage="model")
    repo.bind("slack", "acme", "C9", "other", stage="model")

    assert repo.binding_for("slack", "acme", "C9").project == "other"
    assert len(repo.bindings_for_project("other")) == 1
    assert repo.bindings_for_project("acme") == []


def test_active_excludes_deactivated_projects(session):
    repo = ProjectRepository(session)
    repo.create("acme")
    inactive = repo.create("old")
    inactive.active = False
    session.commit()
    assert [p.name for p in repo.active()] == ["acme"]


def test_create_is_idempotent_so_startup_seeding_can_repeat(session):
    repo = ProjectRepository(session)
    first = repo.create(DEFAULT_PROJECT, display_name="Default")
    second = repo.create(DEFAULT_PROJECT, display_name="Ignored")
    assert first.name == second.name
    assert second.display_name == "Default"
    assert len(repo.active()) == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_project_repository.py -q`
Expected: FAIL — `ModuleNotFoundError: ley_khaa.persistence.project_repository`.

- [ ] **Step 3: Implement the repository**

```python
# backend/ley_khaa/persistence/project_repository.py
"""Projects and the bindings that route conversations into them (spec §3.5)."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .orm import ProjectBindingRow, ProjectRow

# Every task must land somewhere. This project always exists — startup installs
# it (projects/seeds.py) — and it is where a routing miss goes.
DEFAULT_PROJECT = "default"

# The sentinel meaning "this binding covers the whole client". Not NULL: see
# ProjectBindingRow's docstring for why a nullable column here would silently
# disable the unique constraint.
ANY_CONVERSATION = ""


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, name: str, *, display_name: str = "", description: str = "") -> ProjectRow:
        """Idempotent, so startup seeding can run on every boot.

        An existing project is returned untouched rather than overwritten: a
        boot must never quietly revert a description a human edited.
        """
        existing = self.get(name)
        if existing is not None:
            return existing
        row = ProjectRow(
            name=name,
            display_name=display_name or name,
            description=description,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get(self, name: str) -> ProjectRow | None:
        return self.session.get(ProjectRow, name)

    def active(self) -> list[ProjectRow]:
        return list(
            self.session.scalars(
                select(ProjectRow).where(ProjectRow.active.is_(True)).order_by(ProjectRow.name)
            )
        )

    def binding_for(
        self, source: str, client: str, conversation_id: str
    ) -> ProjectBindingRow | None:
        """The most specific binding for this message, or None.

        Two lookups rather than one ordered query: expressing "most specific
        wins" as an ORDER BY on conversation_id would work only by accident of
        "" sorting before every other string, and would silently invert if the
        sentinel ever changed.
        """
        exact = self.session.scalars(
            select(ProjectBindingRow).where(
                ProjectBindingRow.source == source,
                ProjectBindingRow.client == client,
                ProjectBindingRow.conversation_id == conversation_id,
            )
        ).first()
        if exact is not None:
            return exact
        return self.session.scalars(
            select(ProjectBindingRow).where(
                ProjectBindingRow.source == source,
                ProjectBindingRow.client == client,
                ProjectBindingRow.conversation_id == ANY_CONVERSATION,
            )
        ).first()

    def bind(
        self,
        source: str,
        client: str,
        conversation_id: str,
        project: str,
        *,
        stage: str,
    ) -> ProjectBindingRow:
        """Point a scope at a project, moving an existing binding if there is one.

        Idempotent by scope, not by project: the learning rule can fire twice
        for the same conversation when two workers race it, and the second call
        must not hit the unique constraint.
        """
        existing = self.session.scalars(
            select(ProjectBindingRow).where(
                ProjectBindingRow.source == source,
                ProjectBindingRow.client == client,
                ProjectBindingRow.conversation_id == conversation_id,
            )
        ).first()
        if existing is not None:
            existing.project = project
            existing.created_by_stage = stage
            self.session.commit()
            self.session.refresh(existing)
            return existing

        row = ProjectBindingRow(
            id=str(uuid.uuid4()),
            source=source,
            client=client,
            conversation_id=conversation_id,
            project=project,
            created_by_stage=stage,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def bindings_for_project(self, project: str) -> list[ProjectBindingRow]:
        return list(
            self.session.scalars(
                select(ProjectBindingRow).where(ProjectBindingRow.project == project)
            )
        )
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_project_repository.py -q`
Expected: 5 passed.

- [ ] **Step 5: Prove each test fails for the right reason**

- Delete the `exact is not None` early return in `binding_for` →
  `test_a_conversation_binding_beats_a_client_wide_one` must fail on `"special" != "acme"`.
- Make `bind` always insert (delete the `existing is not None` branch) →
  `test_binding_is_idempotent_and_rebinds_rather_than_duplicating` must fail with an
  `IntegrityError`, proving the branch is what absorbs the race.
- Make `create` overwrite `display_name` on an existing row →
  `test_create_is_idempotent_so_startup_seeding_can_repeat` must fail on `"Ignored"`.

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/persistence/project_repository.py backend/tests/test_project_repository.py
git commit -m "feat(persistence): add ProjectRepository with most-specific-wins bindings"
```

---

### Task 3: `ProjectRouter` — two stages and the learning rule

Third instance of the shape `RegistryMatcher` and `MemoryMatcher` already prove. Read
`backend/ley_khaa/registry/matcher.py` first and follow it: a free deterministic stage, one cheap
model call on a miss, the answer treated as untrusted, and a blanket `except` so a failing router
costs only the routing.

**Files:**
- Create: `backend/ley_khaa/projects/__init__.py`, `backend/ley_khaa/projects/models.py`,
  `backend/ley_khaa/projects/router.py`
- Modify: `backend/ley_khaa/llm/router.py`, `backend/ley_khaa/llm/heuristic.py`
- Test: `backend/tests/test_project_router.py`

**Interfaces:**
- Consumes: `ProjectRepository` (Task 2), `LLMClient`, `model_for`, `Stage`.
- Produces:
  - `ProjectChoice(project: str | None, confidence: float, reason: str)` — the stage-2 output model
  - `RoutingDecision(project: str, stage: str, confidence: float, reason: str)` — `stage` is
    `"binding"` | `"model"` | `"default"`
  - `ProjectRouter(projects, llm).route(source, client, conversation_id, title, summary) -> RoutingDecision`
  - `Stage.PROJECT_ROUTE`
  - `ROUTING_CONFIDENCE_FLOOR = 0.8`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_project_router.py
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.project_repository import ProjectRepository
from ley_khaa.projects.models import ProjectChoice
from ley_khaa.projects.router import ProjectRouter


def _repo(session):
    repo = ProjectRepository(session)
    repo.create("default", description="")
    repo.create("acme", description="Acme's equity books and universe checks")
    return repo


def test_a_bound_conversation_routes_free(session):
    projects = _repo(session)
    projects.bind("slack", "acme", "C9", "acme", stage="manual")
    llm = FakeLLM(responses=[])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="acme", conversation_id="C9", title="t", summary="s"
    )
    assert decision.project == "acme"
    assert decision.stage == "binding"
    # The load-bearing assertion: stage 1 is FREE. If this list is non-empty the
    # binding did not short-circuit and every later message pays for a model call.
    assert llm.calls == []


def test_an_unbound_conversation_asks_the_model_once(session):
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project="acme", confidence=0.9, reason="equity books")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="universe check", summary="s"
    )
    assert decision.project == "acme"
    assert decision.stage == "model"
    assert len(llm.calls) == 1


def test_a_confident_model_match_writes_a_binding_so_the_next_message_is_free(session):
    """The learning rule (spec §3.5), and the thing memory gets wrong: this
    updates the routing for that conversation instead of forking a second row."""
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project="acme", confidence=0.9, reason="equity books")])
    router = ProjectRouter(projects, llm)
    router.route(source="slack", client="newco", conversation_id="C1", title="t", summary="s")

    assert projects.binding_for("slack", "newco", "C1").project == "acme"
    assert projects.binding_for("slack", "newco", "C1").created_by_stage == "model"

    second = router.route(
        source="slack", client="newco", conversation_id="C1", title="t2", summary="s2"
    )
    assert second.stage == "binding"
    # Still exactly one call in total — the second route paid nothing.
    assert len(llm.calls) == 1


def test_a_low_confidence_answer_falls_back_to_default_and_writes_no_binding(session):
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project="acme", confidence=0.5, reason="maybe")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    assert decision.project == "default"
    assert decision.stage == "default"
    assert projects.binding_for("slack", "newco", "C1") is None


def test_a_hallucinated_project_name_falls_back_rather_than_routing_nowhere(session):
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project="ghost", confidence=0.99, reason="sure")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    assert decision.project == "default"
    assert projects.binding_for("slack", "newco", "C1") is None


def test_an_inactive_project_is_not_offered_and_not_accepted(session):
    projects = _repo(session)
    acme = projects.get("acme")
    acme.active = False
    session.commit()
    llm = FakeLLM(responses=[ProjectChoice(project="acme", confidence=0.99, reason="sure")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    assert decision.project == "default"


def test_a_failing_model_call_routes_to_default_instead_of_blocking_intake(session):
    """Routing must never drop a request. A misrouted task is recoverable by a
    human; a request that never became a task is not."""
    projects = _repo(session)
    llm = FakeLLM(responses=[RuntimeError("transport exploded")])
    decision = ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    assert decision.project == "default"
    assert decision.stage == "default"


def test_projects_without_a_description_are_not_shown_to_the_model(session):
    """A project with no description is unroutable by stage 2 by construction —
    the model would be guessing from a slug. `default` is exactly such a row."""
    projects = _repo(session)
    llm = FakeLLM(responses=[ProjectChoice(project=None, confidence=0.0, reason="no match")])
    ProjectRouter(projects, llm).route(
        source="slack", client="newco", conversation_id="C1", title="t", summary="s"
    )
    prompt = llm.calls[0].user
    assert "acme" in prompt
    assert "default" not in prompt
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_project_router.py -q`
Expected: FAIL — `ModuleNotFoundError: ley_khaa.projects`.

- [ ] **Step 3: Add the routing stage to the model router**

In `backend/ley_khaa/llm/router.py`, add to `Stage`:

```python
    PROJECT_ROUTE = "project_route"
```

to `_POLICY`:

```python
    # Exists to decide where work goes, not to do the work. Haiku at both
    # complexities, like the other two matchers.
    Stage.PROJECT_ROUTE: {"routine": HAIKU, "hard": HAIKU},
```

and to `_MAX_TOKENS`:

```python
    # A name, a float and one sentence.
    Stage.PROJECT_ROUTE: 1024,
```

- [ ] **Step 4: Write the models and the router**

```python
# backend/ley_khaa/projects/__init__.py
```

(empty file — package marker)

```python
# backend/ley_khaa/projects/models.py
from dataclasses import dataclass

from pydantic import BaseModel


class ProjectChoice(BaseModel):
    """Stage 2's answer. `project` is a project name or null — null is a
    first-class answer meaning "route this to the default project"."""

    project: str | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class RoutingDecision:
    project: str
    # "binding" (free), "model" (stage 2 won), or "default" (miss//fallback).
    # Recorded so a routing decision can be audited after the fact.
    stage: str
    confidence: float
    reason: str
```

```python
# backend/ley_khaa/projects/router.py
"""Which project does this request belong to? (spec §5.4, §3.5)

Two stages, same contract as RegistryMatcher and MemoryMatcher: a free
deterministic lookup first, one cheap model call only on a miss, the model's
answer treated as untrusted. Unlike those two, a miss here is not "no match" —
every task must land somewhere, so a miss routes to the default project.
"""
from __future__ import annotations

import logging

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.orm import ProjectRow
from ..persistence.project_repository import DEFAULT_PROJECT, ProjectRepository
from .models import ProjectChoice, RoutingDecision

logger = logging.getLogger(__name__)

# Below this the model's answer is not evidence. Same value and same reasoning
# as the registry and memory matchers; pinned by a test.
ROUTING_CONFIDENCE_FLOOR = 0.8

SYSTEM = """You decide which project a work request belongs to.

You are given one request and a list of projects, each with a name and a description of the
work it covers. Answer with the name of the project this request belongs to, or null.

Say null unless you are confident. A wrong project puts one client's work in another client's
queue, where the wrong people see it. A null costs only that the request goes to the default
project, where a human sorts it — which is the normal path."""


class ProjectRouter:
    def __init__(self, projects: ProjectRepository, llm: LLMClient) -> None:
        self.projects = projects
        self.llm = llm

    def route(
        self,
        *,
        source: str,
        client: str,
        conversation_id: str,
        title: str,
        summary: str,
    ) -> RoutingDecision:
        binding = self.projects.binding_for(source, client, conversation_id)
        if binding is not None:
            return RoutingDecision(
                project=binding.project,
                stage="binding",
                confidence=1.0,
                reason=f"bound to {binding.project} by {binding.created_by_stage}",
            )
        try:
            return self._classify(source, client, conversation_id, title, summary)
        except Exception:
            # Routing must never block intake. A misrouted task is recoverable
            # by a human; a request that never became a task is not.
            logger.exception("project routing failed; using the default project")
            return _fallback("routing failed")

    def _classify(
        self, source: str, client: str, conversation_id: str, title: str, summary: str
    ) -> RoutingDecision:
        # A project with no description gives the model nothing but a slug to
        # guess from, so it is unroutable by stage 2 by construction and
        # reachable only by an explicit binding. `default` is exactly such a row.
        candidates = [p for p in self.projects.active() if p.description.strip()]
        if not candidates:
            return _fallback("no described project to route into")

        choice = self.llm.parse(
            choice=model_for(Stage.PROJECT_ROUTE),
            system=SYSTEM,
            user=_prompt(title, summary, candidates),
            output_format=ProjectChoice,
        )
        if not choice.project or choice.confidence < ROUTING_CONFIDENCE_FLOOR:
            return _fallback(choice.reason or "no confident project match")

        # The model names a project; it does not choose one. Same untrusted-output
        # discipline as the registry matcher, and `candidates` is what keeps a
        # deactivated project unreachable.
        chosen = next((p for p in candidates if p.name == choice.project), None)
        if chosen is None:
            logger.info("project router named an unknown project %r", choice.project)
            return _fallback("routed to an unknown project")

        # The learning rule (spec §3.5): a confident stage-2 match binds THIS
        # conversation, so every later message in it routes free. It updates the
        # binding for the scope rather than accumulating rows — the asymmetry
        # backlog item 1 records memory getting wrong.
        self.projects.bind(source, client, conversation_id, chosen.name, stage="model")
        return RoutingDecision(
            project=chosen.name,
            stage="model",
            confidence=choice.confidence,
            reason=choice.reason,
        )


def _fallback(reason: str) -> RoutingDecision:
    return RoutingDecision(
        project=DEFAULT_PROJECT, stage="default", confidence=0.0, reason=reason
    )


def _prompt(title: str, summary: str, projects: list[ProjectRow]) -> str:
    lines = [
        "## Request",
        f"title: {title}",
        f"summary: {summary}",
        "",
        "## Projects",
    ]
    lines.extend(f"- {p.name}: {p.description}" for p in projects)
    return "\n".join(lines)
```

- [ ] **Step 5: Add the offline rule to `HeuristicLLM`**

Without this, a fresh clone with no API key raises `NotImplementedError` on the first unbound
conversation. In `backend/ley_khaa/llm/heuristic.py`, import `ProjectChoice` and add to `parse`,
beside the `RegistryDecision` branch:

```python
        if output_format is ProjectChoice:
            # Offline routing is bindings-only by design, for the same reason
            # RegistryDecision is fingerprint-only: a regex has not understood
            # which client's work this is, and guessing would put one client's
            # request in another's queue. Everything unbound goes to `default`.
            return ProjectChoice(project=None, confidence=0.0, reason="offline: no model routing")
```

- [ ] **Step 6: Run the tests**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_project_router.py -q`
Expected: 8 passed.

- [ ] **Step 7: Prove they fail for the right reason**

- Delete the `binding is not None` early return → `test_a_bound_conversation_routes_free` must fail
  on `llm.calls == []`, not on the project name. If it fails on the name instead, the free-path
  assertion is not doing the work.
- Delete the `self.projects.bind(...)` call in `_classify` →
  `test_a_confident_model_match_writes_a_binding...` must fail, and it must fail on the binding
  assertion **before** it reaches the call-count assertion.
- Change `choice.confidence < ROUTING_CONFIDENCE_FLOOR` to `<= 0` →
  `test_a_low_confidence_answer_falls_back_to_default...` must fail.
- Delete the `chosen is None` guard → `test_a_hallucinated_project_name...` must fail with an
  `AttributeError` or a routed project of `"ghost"`.
- Remove the `p.description.strip()` filter →
  `test_projects_without_a_description_are_not_shown_to_the_model` must fail on `"default" not in
  prompt`.

- [ ] **Step 8: Run the whole suite**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 545 passed, 0 skipped, 0 warnings.

- [ ] **Step 9: Commit**

```bash
git add backend/ley_khaa/projects backend/ley_khaa/llm/router.py \
        backend/ley_khaa/llm/heuristic.py backend/tests/test_project_router.py
git commit -m "feat(projects): add the two-stage project router with a learning rule"
```

---

### Task 4: Route at promotion, seed `default`, and correct the statements this makes false

Three things belong in one commit because the third is only true once the first two land: Phase 4's
CHANGELOG and the memory matcher's comment both say "every task is `project='default'`", and this
task is what stops that being true. Shipping the code without the correction leaves a false
statement in the repo — the class of defect commit `8cebd1f` was written to clean up.

**Files:**
- Create: `backend/ley_khaa/projects/seeds.py`
- Modify: `backend/ley_khaa/orchestrator/orchestrator.py`, `backend/ley_khaa/api/app.py`,
  `backend/ley_khaa/memory/matcher.py`, `CHANGELOG.md`
- Test: `backend/tests/test_routing_end_to_end.py`

**Interfaces:**
- Consumes: `ProjectRouter` (Task 3), `ProjectRepository` (Task 2).
- Produces: `ensure_default_project(session) -> ProjectRow`; `Orchestrator` gains a `projects`
  keyword argument and a `self.router`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_routing_end_to_end.py
from ley_khaa.crystallizer.candidate import CandidateState
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.project_repository import ProjectRepository
from ley_khaa.projects.seeds import ensure_default_project


def test_the_default_project_is_seeded_idempotently(session):
    ensure_default_project(session)
    ensure_default_project(session)
    projects = ProjectRepository(session)
    assert projects.get("default") is not None
    assert len(projects.active()) == 1


def test_a_bound_conversation_puts_its_task_in_that_project(session, stub_execution):
    """The DoD line: a message from a bound client lands in that client's project,
    not in `default`."""
    from ley_khaa.api.app import build_orchestrator

    ensure_default_project(session)
    projects = ProjectRepository(session)
    projects.create("acme", description="Acme's equity books")
    projects.bind("simulator", "acme", "C-acme", "acme", stage="manual")

    orchestrator = build_orchestrator(session)
    orchestrator.ingest(
        {
            "text": "compare the bloomberg universe against the factset universe, csv",
            "conversation_id": "C-acme",
            "client": "acme",
        }
    )
    orchestrator.sweep()

    tasks = orchestrator.repo.list()
    assert tasks, "the conversation produced no task"
    assert {t.project for t in tasks} == {"acme"}


def test_two_clients_land_in_two_projects(session, stub_execution):
    from ley_khaa.api.app import build_orchestrator

    ensure_default_project(session)
    projects = ProjectRepository(session)
    projects.create("acme", description="Acme's books")
    projects.create("globex", description="Globex's books")
    projects.bind("simulator", "acme", "", "acme", stage="manual")
    projects.bind("simulator", "globex", "", "globex", stage="manual")

    orchestrator = build_orchestrator(session)
    for client, conversation in (("acme", "C-a"), ("globex", "C-g")):
        orchestrator.ingest(
            {
                "text": "compare the bloomberg universe against the factset universe, csv",
                "conversation_id": conversation,
                "client": client,
            }
        )
    orchestrator.sweep()

    by_project = {t.project for t in orchestrator.repo.list()}
    assert by_project == {"acme", "globex"}


def test_an_unroutable_conversation_still_produces_a_task(session, stub_execution):
    """Routing must never drop work — the offline HeuristicLLM never matches, so
    this is the ordinary fresh-clone path."""
    from ley_khaa.api.app import build_orchestrator

    ensure_default_project(session)
    orchestrator = build_orchestrator(session)
    orchestrator.ingest(
        {
            "text": "compare the bloomberg universe against the factset universe, csv",
            "conversation_id": "C-unknown",
            "client": "nobody",
        }
    )
    orchestrator.sweep()

    tasks = orchestrator.repo.list()
    assert tasks
    assert {t.project for t in tasks} == {"default"}


def test_a_candidate_with_no_messages_routes_to_default_rather_than_raising(session):
    """Defensive: _promote reads source/client off the candidate's messages, and
    a candidate with none must not take intake down with an IndexError."""
    from ley_khaa.api.app import build_orchestrator

    ensure_default_project(session)
    orchestrator = build_orchestrator(session)
    candidate = orchestrator.candidates.upsert(
        conversation_id="C-empty",
        candidate_key="k",
        title="orphan",
        summary="",
        state=CandidateState.READY,
        message_ids=[],
        missing_fields=[],
        open_question=None,
    )
    task_id = orchestrator._promote(candidate)
    assert orchestrator.repo.get(task_id).project == "default"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_routing_end_to_end.py -q`
Expected: FAIL — `ModuleNotFoundError: ley_khaa.projects.seeds`.

- [ ] **Step 3: Write the seed**

```python
# backend/ley_khaa/projects/seeds.py
"""The default project, installed at startup.

Deliberately described as installed by STARTUP, not by migration 0005: the
migration creates the table only. The seed workflows use the same division, and
a docstring claiming otherwise is the false-statement defect 8cebd1f fixed.

Its description is empty on purpose. A described project is offered to stage-2
routing, and offering `default` there would let the model route into the very
project that exists to mean "the model did not route this".
"""
from sqlalchemy.orm import Session

from ..persistence.orm import ProjectRow
from ..persistence.project_repository import DEFAULT_PROJECT, ProjectRepository


def ensure_default_project(session: Session) -> ProjectRow:
    return ProjectRepository(session).create(DEFAULT_PROJECT, display_name="Default")
```

- [ ] **Step 4: Route in `_promote`**

In `backend/ley_khaa/orchestrator/orchestrator.py`, add the imports:

```python
from ..persistence.project_repository import DEFAULT_PROJECT, ProjectRepository
from ..projects.router import ProjectRouter
```

Add a `projects` keyword to `__init__` and build the router:

```python
        projects: ProjectRepository | None = None,
```

```python
        self.projects = projects
        self.router = ProjectRouter(projects, llm) if projects is not None else None
```

Replace the `project="default"` line in `_promote`:

```python
    def _promote(self, candidate: CandidateRow) -> str | None:
        if not self.candidates.claim_for_promotion(candidate.id):
            return None
        task = self.repo.create(
            project=self._route(candidate),
            title=candidate.title,
            source_message_ids=list(candidate.message_ids),
            candidate_id=candidate.id,
        )
        self.candidates.attach_task(candidate.id, task.id)
        self.driver.advance(task.id)
        return task.id

    def _route(self, candidate: CandidateRow) -> str:
        """Which project this candidate's work belongs to (spec §5.4).

        source and client live on the MESSAGES, not on the candidate — the
        candidate only knows its conversation. A candidate with no messages
        cannot be routed and goes to the default project rather than taking
        intake down with an IndexError.
        """
        if self.router is None:
            return DEFAULT_PROJECT
        messages = self.messages.get_many(list(candidate.message_ids or []))
        if not messages:
            return DEFAULT_PROJECT
        first = messages[0]
        decision = self.router.route(
            source=first.source,
            client=first.client,
            conversation_id=candidate.conversation_id,
            title=candidate.title,
            summary=candidate.summary,
        )
        logger.info(
            "task from candidate %s routed to %s by %s (%s)",
            candidate.id, decision.project, decision.stage, decision.reason,
        )
        return decision.project
```

Add `import logging` and `logger = logging.getLogger(__name__)` at the top of the module if they
are not already there.

- [ ] **Step 5: Wire it into `build_orchestrator` and `lifespan`**

In `backend/ley_khaa/api/app.py`, add `ProjectRepository(session)` to `build_orchestrator`:

```python
        projects=ProjectRepository(session),
```

and call the seed in `lifespan`, beside `ensure_seed_workflows(session)`:

```python
        # Every task must land in a project that exists, including the very
        # first one on a fresh clone.
        ensure_default_project(session)
```

- [ ] **Step 6: Run the tests**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_routing_end_to_end.py -q`
Expected: 5 passed.

- [ ] **Step 7: Correct the two statements this task makes false**

In `backend/ley_khaa/memory/matcher.py`, replace the `_recall` comment that begins "Scoped by
TaskRow.project, which is 'default' for every task until §5.4 project routing lands":

```python
        # Scoped by TaskRow.project, which Phase 5's router now sets per client
        # (projects/router.py). Memory is scoped and the registry is global on
        # purpose: a remembered TaskSpec carries `recipient` and is reused
        # wholesale, so sharing one across clients misdelivers work — whereas a
        # workflow is code, identified by the sha256 of its source and bound
        # positionally to each run's own inputs, so it carries no client data.
```

In `CHANGELOG.md`, under `## [Unreleased]`, add an `### Added` entry for routing and correct the
0.5.0 note that says memory scoping does not isolate clients because every task is `default`. State
what is true now: routing assigns projects per client, so the scoping is real.

- [ ] **Step 8: Prove the tests fail for the right reason**

- Hardcode `return DEFAULT_PROJECT` at the top of `_route` →
  `test_a_bound_conversation_puts_its_task_in_that_project` and `test_two_clients_land_in_two_projects`
  must both fail.
- Delete the `if not messages` guard → `test_a_candidate_with_no_messages_routes_to_default...`
  must fail with `IndexError`, not with a wrong project.
- Make `create` in `ProjectRepository` non-idempotent →
  `test_the_default_project_is_seeded_idempotently` must fail.

- [ ] **Step 9: Run the whole suite**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 550 passed, 0 skipped, 0 warnings.

**If other tests fail here, read them before changing them.** A test that asserted
`task.project == "default"` for a conversation that now routes elsewhere is telling you routing
works; a test that fails because `build_orchestrator` gained an argument is telling you a call site
was missed.

- [ ] **Step 10: Commit**

```bash
git add backend/ley_khaa/projects/seeds.py backend/ley_khaa/orchestrator/orchestrator.py \
        backend/ley_khaa/api/app.py backend/ley_khaa/memory/matcher.py CHANGELOG.md \
        backend/tests/test_routing_end_to_end.py
git commit -m "feat(orchestrator): route each task into a project at promotion"
```

---

### Task 5: Leases and runnable queries

The queue is the tasks table. This task gives it the two operations a worker pool needs — claim and
release — and the query that says what is waiting to run.

**Files:**
- Modify: `backend/ley_khaa/domain/states.py`, `backend/ley_khaa/orchestrator/driver.py`,
  `backend/ley_khaa/persistence/repository.py`
- Test: `backend/tests/test_task_leases.py`

**Interfaces:**
- Produces:
  - `domain.states.WAITING: frozenset[TaskState]` (moved from `driver._WAITING`)
  - `domain.states.TERMINAL: frozenset[TaskState]` = `{DONE, FAILED}`
  - `TaskRepository.claim_lease(task_id, *, owner, ttl_seconds, now=None) -> bool`
  - `TaskRepository.heartbeat_lease(task_id, *, owner, ttl_seconds, now=None) -> bool`
  - `TaskRepository.release_lease(task_id, *, owner) -> bool`
  - `TaskRepository.runnable_projects(now=None) -> list[str]`
  - `TaskRepository.next_runnable(project, now=None) -> TaskRow | None`

**A trap to verify rather than assume.** SQLite has no native datetime type; SQLAlchemy stores
`DateTime(timezone=True)` as an ISO-ish string and **drops the offset**. Comparing
`lease_expires_at < now` in SQL therefore works only because both sides are UTC and the format
sorts lexicographically. That is true here — `_now()` is UTC-aware and every write goes through it
— but it is an assumption, so `test_a_live_lease_cannot_be_stolen` and
`test_an_expired_lease_can_be_reclaimed_once` exist to prove it on the very database the suite
uses. **If those two tests behave inconsistently**, do not add `str()` casts or timezone juggling:
switch `lease_expires_at` to an epoch `Float` column, which is unambiguous on both backends, and
say so in the migration docstring.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_task_leases.py
from datetime import datetime, timedelta, timezone

from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.repository import TaskRepository


def _task(repo, *, project="default", state=TaskState.CLASSIFIED):
    row = repo.create(project=project, title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=state)
    return repo.get(row.id)


def test_a_free_task_can_be_claimed(session):
    repo = TaskRepository(session)
    task = _task(repo)
    assert repo.claim_lease(task.id, owner="w1", ttl_seconds=30) is True
    assert repo.get(task.id).lease_owner == "w1"


def test_a_live_lease_cannot_be_stolen(session):
    repo = TaskRepository(session)
    task = _task(repo)
    assert repo.claim_lease(task.id, owner="w1", ttl_seconds=30) is True
    assert repo.claim_lease(task.id, owner="w2", ttl_seconds=30) is False
    assert repo.get(task.id).lease_owner == "w1"


def test_an_expired_lease_can_be_reclaimed_once(session):
    repo = TaskRepository(session)
    task = _task(repo)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert repo.claim_lease(task.id, owner="w1", ttl_seconds=30, now=past) is True
    assert repo.claim_lease(task.id, owner="w2", ttl_seconds=30) is True
    assert repo.get(task.id).lease_owner == "w2"


def test_an_ordinary_claim_does_not_count_as_an_attempt(session):
    """lease_attempts counts RECLAIMS, not claims.

    Incrementing on every claim would count every ordinary hand-off between
    states, so a healthy task that simply passed through several steps would
    trip the attempt cap and fail for no reason at all.
    """
    repo = TaskRepository(session)
    task = _task(repo)
    for _ in range(5):
        assert repo.claim_lease(task.id, owner="w1", ttl_seconds=30) is True
        assert repo.release_lease(task.id, owner="w1") is True
    assert repo.get(task.id).lease_attempts == 0


def test_reclaiming_an_expired_lease_does_count(session):
    repo = TaskRepository(session)
    task = _task(repo)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    repo.claim_lease(task.id, owner="w1", ttl_seconds=30, now=past)
    repo.claim_lease(task.id, owner="w2", ttl_seconds=30)
    assert repo.get(task.id).lease_attempts == 1


def test_a_worker_cannot_release_a_lease_it_no_longer_holds(session):
    """A worker whose lease expired must not be able to clear its successor's —
    that would hand the same live task to a third worker."""
    repo = TaskRepository(session)
    task = _task(repo)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    repo.claim_lease(task.id, owner="w1", ttl_seconds=30, now=past)
    repo.claim_lease(task.id, owner="w2", ttl_seconds=30)

    assert repo.release_lease(task.id, owner="w1") is False
    assert repo.get(task.id).lease_owner == "w2"


def test_a_heartbeat_extends_only_the_holder_s_lease(session):
    repo = TaskRepository(session)
    task = _task(repo)
    repo.claim_lease(task.id, owner="w1", ttl_seconds=1)
    before = repo.get(task.id).lease_expires_at

    assert repo.heartbeat_lease(task.id, owner="w1", ttl_seconds=600) is True
    assert repo.get(task.id).lease_expires_at > before
    assert repo.heartbeat_lease(task.id, owner="w2", ttl_seconds=600) is False


def test_runnable_projects_lists_only_projects_with_work_to_do(session):
    repo = TaskRepository(session)
    _task(repo, project="acme", state=TaskState.CLASSIFIED)
    _task(repo, project="globex", state=TaskState.AWAITING_APPROVAL)  # waiting on a human
    _task(repo, project="initech", state=TaskState.DONE)  # terminal
    assert repo.runnable_projects() == ["acme"]


def test_a_leased_task_is_not_runnable_again_while_the_lease_is_live(session):
    repo = TaskRepository(session)
    task = _task(repo, project="acme")
    repo.claim_lease(task.id, owner="w1", ttl_seconds=30)
    assert repo.runnable_projects() == []
    assert repo.next_runnable("acme") is None


def test_an_expired_lease_makes_the_task_runnable_again(session):
    """This is what makes EXECUTING recoverable — the reason advance_stalled
    could never touch it before."""
    repo = TaskRepository(session)
    task = _task(repo, project="acme", state=TaskState.EXECUTING)
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    repo.claim_lease(task.id, owner="dead-worker", ttl_seconds=30, now=past)
    assert repo.runnable_projects() == ["acme"]
    assert repo.next_runnable("acme").id == task.id


def test_next_runnable_is_fifo_within_a_project(session):
    repo = TaskRepository(session)
    first = _task(repo, project="acme")
    _task(repo, project="acme")
    assert repo.next_runnable("acme").id == first.id
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_task_leases.py -q`
Expected: FAIL — `AttributeError: 'TaskRepository' object has no attribute 'claim_lease'`.

- [ ] **Step 3: Move `WAITING` to `domain/states.py`**

Two definitions of "which states block on a person" would drift, and the one that drifted would
either stall tasks forever or drive a task out from under a human. Add to
`backend/ley_khaa/domain/states.py`, after `_ALLOWED`:

```python
# Where a task comes to rest on its own: finished, or a human owes it something.
# Lives here rather than in the driver because the dispatcher needs the same
# answer, and two copies of this set would drift.
WAITING: frozenset[TaskState] = frozenset(
    {
        TaskState.AWAITING_APPROVAL,
        TaskState.NEEDS_CLARIFICATION,
        TaskState.DONE,
        TaskState.FAILED,
    }
)

# Nothing moves a task out of these.
TERMINAL: frozenset[TaskState] = frozenset({TaskState.DONE, TaskState.FAILED})
```

In `backend/ley_khaa/orchestrator/driver.py`, delete the local `_WAITING` block and import the
shared one:

```python
from ..domain.states import WAITING as _WAITING, InvalidTransition, TaskState
```

Keeping the `_WAITING` name inside the driver means the rest of that module is unchanged.

- [ ] **Step 4: Implement the lease operations**

In `backend/ley_khaa/persistence/repository.py`, extend the imports:

```python
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, or_, select, update

from ..domain.states import TERMINAL, WAITING, TaskState, ensure_transition
```

Add to `TaskRepository`:

```python
    # --- the lease that makes this table the queue (spec §3.2) --------------

    def claim_lease(
        self, task_id: str, *, owner: str, ttl_seconds: int, now: datetime | None = None
    ) -> bool:
        """Take the lease on a task. True if we won it.

        Wins only when the lease is free or has EXPIRED — an unexpired lease
        means another worker is genuinely mid-flight, and stealing it would run
        two lanes over one workspace, which is exactly what advance_stalled()
        excluded EXECUTING to avoid.

        The CASE is load-bearing: lease_attempts counts reclaims of an expired
        lease, never ordinary claims. Incrementing unconditionally would count
        every hand-off between states, so a healthy task would trip the attempt
        cap just by making normal progress.
        """
        moment = now or datetime.now(timezone.utc)
        result = self.session.execute(
            update(TaskRow)
            .where(
                TaskRow.id == task_id,
                or_(
                    TaskRow.lease_owner.is_(None),
                    TaskRow.lease_expires_at < moment,
                ),
            )
            .values(
                lease_owner=owner,
                lease_expires_at=moment + timedelta(seconds=ttl_seconds),
                lease_attempts=TaskRow.lease_attempts
                + case((TaskRow.lease_owner.is_(None), 0), else_=1),
            )
        )
        self.session.commit()
        return result.rowcount == 1

    def heartbeat_lease(
        self, task_id: str, *, owner: str, ttl_seconds: int, now: datetime | None = None
    ) -> bool:
        """Push the expiry out while work is still in flight.

        Guarded on ownership: a worker whose lease already expired and was taken
        over must not be able to extend it back out from under its successor.
        A False here tells the caller it has lost the task and must stop.
        """
        moment = now or datetime.now(timezone.utc)
        result = self.session.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.lease_owner == owner)
            .values(lease_expires_at=moment + timedelta(seconds=ttl_seconds))
        )
        self.session.commit()
        return result.rowcount == 1

    def release_lease(self, task_id: str, *, owner: str) -> bool:
        """Hand the task back. Guarded on ownership for the same reason as the
        heartbeat: releasing a lease you no longer hold would hand a live task
        to a third worker."""
        result = self.session.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.lease_owner == owner)
            .values(lease_owner=None, lease_expires_at=None)
        )
        self.session.commit()
        return result.rowcount == 1

    # --- what is waiting to run --------------------------------------------

    def _runnable_where(self, moment: datetime):
        """A task is runnable when nothing else is driving it and nobody is
        waiting on a human: state is neither terminal nor human-waiting, and the
        lease is free or expired."""
        blocked = [s.value for s in WAITING | TERMINAL]
        return (
            TaskRow.state.not_in(blocked),
            or_(TaskRow.lease_owner.is_(None), TaskRow.lease_expires_at < moment),
        )

    def runnable_projects(self, now: datetime | None = None) -> list[str]:
        moment = now or datetime.now(timezone.utc)
        rows = self.session.scalars(
            select(TaskRow.project)
            .where(*self._runnable_where(moment))
            .group_by(TaskRow.project)
            .order_by(TaskRow.project)
        )
        return list(rows)

    def next_runnable(self, project: str, now: datetime | None = None) -> TaskRow | None:
        """The oldest runnable task in a project. FIFO: urgency-based reordering
        is deliberately out of scope (spec §7) because urgency lives in the spec,
        which is only known after the task has already been dequeued."""
        moment = now or datetime.now(timezone.utc)
        return self.session.scalars(
            select(TaskRow)
            .where(TaskRow.project == project, *self._runnable_where(moment))
            .order_by(TaskRow.created_at)
        ).first()
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_task_leases.py -q`
Expected: 11 passed.

**If `test_a_live_lease_cannot_be_stolen` and `test_an_expired_lease_can_be_reclaimed_once`
disagree with each other**, the SQLite datetime assumption above is wrong. Switch
`lease_expires_at` to an epoch `Float` on both the ORM and migration 0005, and record why in the
migration docstring. Do not paper over it with string casts.

- [ ] **Step 6: Prove they fail for the right reason**

- Delete the `case(...)` and increment unconditionally →
  `test_an_ordinary_claim_does_not_count_as_an_attempt` must fail with `5 != 0`.
- Delete the `or_(...)` expiry clause from `claim_lease` →
  `test_an_expired_lease_can_be_reclaimed_once` must fail.
- Delete the `TaskRow.lease_owner == owner` guard from `release_lease` →
  `test_a_worker_cannot_release_a_lease_it_no_longer_holds` must fail. Do the same for
  `heartbeat_lease` and its own test — **each guard needs its own failing test**, or one can be
  deleted silently later.
- Remove the lease clause from `_runnable_where` →
  `test_a_leased_task_is_not_runnable_again_while_the_lease_is_live` must fail.
- Remove `WAITING` from `blocked` → `test_runnable_projects_lists_only_projects_with_work_to_do`
  must fail by including `globex`.

- [ ] **Step 7: Run the whole suite**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 561 passed, 0 skipped, 0 warnings.

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa/domain/states.py backend/ley_khaa/orchestrator/driver.py \
        backend/ley_khaa/persistence/repository.py backend/tests/test_task_leases.py
git commit -m "feat(persistence): lease tasks so the tasks table can act as the queue"
```

---

### Task 6: The `Dispatcher` — one worker per project

**Files:**
- Create: `backend/ley_khaa/orchestrator/dispatcher.py`
- Modify: `backend/ley_khaa/config.py`
- Test: `backend/tests/test_dispatcher.py`

**Interfaces:**
- Consumes: `TaskRepository.claim_lease / heartbeat_lease / release_lease / runnable_projects /
  next_runnable` (Task 5).
- Produces:
  - `Dispatcher(session_factory, *, drive, owner=None)` where `drive(session, task_id) -> None`
  - `await dispatcher.tick() -> list[str]` — task ids driven this tick
  - `await dispatcher.run_forever(interval)`
  - settings: `dispatch_mode`, `lease_ttl_seconds`, `lease_heartbeat_seconds`,
    `max_concurrent_projects`, `max_lease_attempts`

**`pytest-asyncio` is NOT a dependency of this project.** Do not write `async def test_...` — pytest
will not run it, and the resulting warning breaks the zero-warnings bar. Drive the coroutines with
`asyncio.run(...)` from ordinary sync tests, as the tests below do.

- [ ] **Step 1: Add the settings**

In `backend/ley_khaa/config.py`, add to `Settings`:

```python
    # "workers" runs tasks on the background dispatcher; "inline" drives them on
    # the calling thread, which is what every test and a single-operator CLI run
    # wants. See spec §3.4 — inline is a real supported mode, not a test shim.
    dispatch_mode: str = os.getenv("LEY_KHAA_DISPATCH", "workers")
    # How long a worker's claim on a task stays valid without a heartbeat. Long
    # enough that a slow sandbox run is not mistaken for a dead worker.
    lease_ttl_seconds: int = int(os.getenv("LEY_KHAA_LEASE_TTL", "120"))
    lease_heartbeat_seconds: int = int(os.getenv("LEY_KHAA_LEASE_HEARTBEAT", "30"))
    # How many projects may run at once. Each one is a full lane: two Opus calls
    # and a sandbox run.
    max_concurrent_projects: int = int(os.getenv("LEY_KHAA_MAX_PROJECTS", "4"))
    # Past this many reclaims of an expired lease, a task is poison and fails
    # visibly rather than being re-run forever.
    max_lease_attempts: int = int(os.getenv("LEY_KHAA_MAX_LEASE_ATTEMPTS", "3"))
```

- [ ] **Step 2: Write the failing tests**

```python
# backend/tests/test_dispatcher.py
import asyncio
import threading

from ley_khaa.domain.states import TaskState
from ley_khaa.orchestrator.dispatcher import Dispatcher
from ley_khaa.persistence.repository import TaskRepository


def _task(session, *, project, state=TaskState.CLASSIFIED):
    repo = TaskRepository(session)
    row = repo.create(project=project, title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=state)
    return row.id


def test_a_tick_drives_one_task_per_project(session):
    a = _task(session, project="acme")
    g = _task(session, project="globex")
    driven = []

    def drive(_session, task_id):
        driven.append(task_id)

    dispatcher = Dispatcher(lambda: session, drive=drive)
    result = asyncio.run(dispatcher.tick())

    assert sorted(result) == sorted([a, g])
    assert sorted(driven) == sorted([a, g])


def test_a_tick_takes_only_the_oldest_task_in_a_project(session):
    first = _task(session, project="acme")
    _task(session, project="acme")

    def drive(_session, task_id):
        pass

    assert asyncio.run(Dispatcher(lambda: session, drive=drive).tick()) == [first]


def test_the_lease_is_released_after_the_work_finishes(session):
    task_id = _task(session, project="acme")

    def drive(_session, _task_id):
        pass

    asyncio.run(Dispatcher(lambda: session, drive=drive).tick())
    assert TaskRepository(session).get(task_id).lease_owner is None


def test_the_lease_is_released_even_when_the_work_raises(session):
    """A dispatcher that leaks leases on failure is worse than inline execution:
    the task stays invisible until its TTL expires, every time it fails."""
    task_id = _task(session, project="acme")

    def drive(_session, _task_id):
        raise RuntimeError("boom")

    asyncio.run(Dispatcher(lambda: session, drive=drive).tick())
    assert TaskRepository(session).get(task_id).lease_owner is None


def test_one_bad_task_does_not_stop_the_other_projects(session):
    _task(session, project="acme")
    good = _task(session, project="globex")
    driven = []

    def drive(_session, task_id):
        if task_id != good:
            raise RuntimeError("boom")
        driven.append(task_id)

    asyncio.run(Dispatcher(lambda: session, drive=drive).tick())
    assert driven == [good]


def test_a_task_past_the_attempt_cap_fails_instead_of_running_again(session):
    """The poison-task ceiling. Without it a task that kills its worker every
    time is re-run forever, at two Opus calls a go."""
    from dataclasses import replace

    import ley_khaa.config as config

    task_id = _task(session, project="acme")
    repo = TaskRepository(session)
    row = repo.get(task_id)
    row.lease_attempts = 99
    session.commit()

    original = config.settings
    config.settings = replace(original, max_lease_attempts=3)
    try:
        driven = []
        asyncio.run(
            Dispatcher(lambda: session, drive=lambda s, t: driven.append(t)).tick()
        )
    finally:
        config.settings = original

    assert driven == []
    row = repo.get(task_id)
    assert TaskState(row.state) is TaskState.FAILED
    assert "lease" in (row.failure_reason or "")


def test_two_dispatchers_ticking_at_once_do_not_both_take_the_same_task(session):
    """The claim is what makes one worker per project true under concurrency."""
    task_id = _task(session, project="acme")
    driven = []
    lock = threading.Lock()

    def drive(_session, tid):
        with lock:
            driven.append(tid)

    async def both():
        one = Dispatcher(lambda: session, drive=drive, owner="w1")
        two = Dispatcher(lambda: session, drive=drive, owner="w2")
        return await asyncio.gather(one.tick(), two.tick())

    results = asyncio.run(both())
    assert sorted(sum(results, [])) == [task_id]
    assert driven == [task_id]


def test_a_project_with_nothing_runnable_is_skipped(session):
    _task(session, project="acme", state=TaskState.AWAITING_APPROVAL)
    assert asyncio.run(Dispatcher(lambda: session, drive=lambda s, t: None).tick()) == []
```

- [ ] **Step 3: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_dispatcher.py -q`
Expected: FAIL — `ModuleNotFoundError: ley_khaa.orchestrator.dispatcher`.

- [ ] **Step 4: Implement the dispatcher**

```python
# backend/ley_khaa/orchestrator/dispatcher.py
"""One worker per project (spec §3.3).

The queue is the tasks table; this is the thing that drains it. Serial within a
project, concurrent across projects — which is the direct reading of §5.4's
"each project has its own task queue", and what keeps the set of tasks an
amendment could target small and stable.

Nothing here knows how a task is driven. `drive` is injected, so this module can
be tested without an LLM, a sandbox or an orchestrator.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from contextlib import suppress

from ..config import settings
from ..domain.states import TaskState
from ..persistence.repository import TaskRepository

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], object]
Drive = Callable[[object, str], None]


class Dispatcher:
    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        drive: Drive,
        owner: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.drive = drive
        # Identifies this dispatcher in the lease. Distinct per process so a
        # second backend cannot silently believe it holds another's tasks.
        self.owner = owner or f"dispatcher-{uuid.uuid4().hex[:8]}"

    async def run_forever(self, interval: float) -> None:
        """Tick until cancelled. A failing tick is logged, never fatal —
        the same contract as _periodic_sweeper."""
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("dispatcher tick failed")
            await asyncio.sleep(interval)

    async def tick(self) -> list[str]:
        """Give every project with runnable work one worker. Returns the task
        ids actually driven."""
        projects = await asyncio.to_thread(self._runnable_projects)
        if not projects:
            return []

        limit = asyncio.Semaphore(settings.max_concurrent_projects)

        async def guarded(project: str) -> str | None:
            async with limit:
                return await self._work_one(project)

        results = await asyncio.gather(
            *(guarded(project) for project in projects), return_exceptions=True
        )
        driven: list[str] = []
        for result in results:
            if isinstance(result, BaseException):
                # Already logged in _work_one; one project's failure must never
                # take the others' results with it.
                logger.exception("dispatching a project failed", exc_info=result)
            elif result is not None:
                driven.append(result)
        return driven

    async def _work_one(self, project: str) -> str | None:
        task_id = await asyncio.to_thread(self._claim_next, project)
        if task_id is None:
            return None

        beat = asyncio.create_task(self._heartbeat(task_id))
        try:
            await asyncio.to_thread(self._drive, task_id)
        except Exception:
            logger.exception("driving task %s failed", task_id)
        finally:
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat
            await asyncio.to_thread(self._release, task_id)
        return task_id

    async def _heartbeat(self, task_id: str) -> None:
        """Keep the lease alive while the worker thread is busy.

        A False from heartbeat_lease means this worker no longer holds the task
        (its lease expired and someone reclaimed it). Stop beating: continuing
        would extend a lease we do not own.
        """
        while True:
            await asyncio.sleep(settings.lease_heartbeat_seconds)
            held = await asyncio.to_thread(self._beat, task_id)
            if not held:
                logger.warning("lost the lease on task %s mid-flight", task_id)
                return

    # --- the synchronous half, each call on its own session ----------------

    def _runnable_projects(self) -> list[str]:
        session = self.session_factory()
        try:
            return TaskRepository(session).runnable_projects()
        finally:
            self._close(session)

    def _claim_next(self, project: str) -> str | None:
        session = self.session_factory()
        try:
            repo = TaskRepository(session)
            row = repo.next_runnable(project)
            if row is None:
                return None
            # Read the attempt count BEFORE claiming: claim_lease increments it
            # when it takes over an expired lease, so checking afterwards would
            # be off by one and let a poison task have one extra run.
            attempts = row.lease_attempts or 0
            if attempts >= settings.max_lease_attempts:
                self._fail_poison(repo, row.id, attempts)
                return None
            if not repo.claim_lease(
                row.id, owner=self.owner, ttl_seconds=settings.lease_ttl_seconds
            ):
                return None
            return row.id
        finally:
            self._close(session)

    def _fail_poison(self, repo: TaskRepository, task_id: str, attempts: int) -> None:
        """A task that has outlived its workers this many times is not going to
        finish. Fail it visibly rather than paying for it forever."""
        row = repo.get(task_id)
        if row is None:
            return
        state = TaskState(row.state)
        # Claim before recording, the ordering c043c46 established: writing the
        # reason first stamps it onto a task whose transition then lost a race.
        if repo.claim(task_id, expected=state, target=TaskState.FAILED):
            repo.record_failure(
                task_id, f"abandoned after {attempts} lease attempts; no worker finished it"
            )

    def _drive(self, task_id: str) -> None:
        session = self.session_factory()
        try:
            self.drive(session, task_id)
        finally:
            self._close(session)

    def _beat(self, task_id: str) -> bool:
        session = self.session_factory()
        try:
            return TaskRepository(session).heartbeat_lease(
                task_id, owner=self.owner, ttl_seconds=settings.lease_ttl_seconds
            )
        finally:
            self._close(session)

    def _release(self, task_id: str) -> None:
        session = self.session_factory()
        try:
            TaskRepository(session).release_lease(task_id, owner=self.owner)
        finally:
            self._close(session)

    def _close(self, session) -> None:
        """Tests hand in one long-lived session and must keep it; the app hands
        in SessionLocal and wants each unit of work to close its own."""
        if getattr(session, "_ley_khaa_shared", False):
            return
        session.close()
```

Note the `_close` seam: `conftest`'s `session` fixture is a single shared session that the test
keeps using after the dispatcher runs. Mark it in `conftest.py` so the dispatcher does not close it
underneath the test:

```python
    s = TestingSession()
    # The dispatcher opens a session per unit of work and closes it; this one is
    # shared with the test that yields it, so it must survive.
    s._ley_khaa_shared = True
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_dispatcher.py -q`
Expected: 8 passed.

- [ ] **Step 6: Prove they fail for the right reason**

- Delete the `finally:` release in `_work_one` →
  `test_the_lease_is_released_even_when_the_work_raises` must fail.
- Move the attempt check to after `claim_lease` →
  `test_a_task_past_the_attempt_cap_fails_instead_of_running_again` must still pass, but change the
  fixture's `lease_attempts` to exactly `3` and confirm the off-by-one shows up. Keep the check
  before the claim.
- Delete the `if not repo.claim_lease(...)` guard →
  `test_two_dispatchers_ticking_at_once_do_not_both_take_the_same_task` must fail with two entries.
- Replace `return_exceptions=True` with `False` →
  `test_one_bad_task_does_not_stop_the_other_projects` must fail.

- [ ] **Step 7: Run the whole suite**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 569 passed, 0 skipped, 0 warnings.

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa/orchestrator/dispatcher.py backend/ley_khaa/config.py \
        backend/tests/conftest.py backend/tests/test_dispatcher.py
git commit -m "feat(orchestrator): add a per-project dispatcher over leased tasks"
```

---

### Task 7: Dispatch mode — call sites hand off instead of driving

**Files:**
- Modify: `backend/ley_khaa/orchestrator/driver.py`,
  `backend/ley_khaa/orchestrator/orchestrator.py`, `backend/ley_khaa/api/app.py`,
  `backend/tests/conftest.py`
- Test: `backend/tests/test_dispatch_modes.py`

**Interfaces:**
- Produces: `TaskDriver.hand_off(task_id) -> TaskRow`; `build_dispatcher()` in `api/app.py`.

**The seam.** `advance()` keeps its meaning — "drive this task as far as it goes" — and the
dispatcher calls it directly. What changes is the *human-action* path: `approve`, `override`,
`edit_spec`, and the orchestrator's promote / reply routes now call `hand_off`, which drives inline
or returns immediately and lets the dispatcher pick the task up.

**The cross-module trap in this task.** `_sweep_once` calls `orchestrator.advance_stalled()`, which
drives tasks **with no lease**. Left alone in workers mode, the sweeper would drive the same task
the dispatcher is driving — reintroducing exactly the double-lane bug the lease exists to prevent.
`advance_stalled` must run only in inline mode.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_dispatch_modes.py
from dataclasses import replace

import ley_khaa.config as config
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.repository import TaskRepository


def _pin(mode: str):
    original = config.settings
    config.settings = replace(original, dispatch_mode=mode)
    return original


def test_inline_mode_drives_the_task_on_the_calling_thread(session, stub_execution):
    from ley_khaa.api.app import build_orchestrator
    from ley_khaa.projects.seeds import ensure_default_project

    original = _pin("inline")
    try:
        ensure_default_project(session)
        orchestrator = build_orchestrator(session)
        orchestrator.ingest(
            {"text": "compare the bloomberg universe against the factset universe, csv"}
        )
        orchestrator.sweep()
        states = {TaskState(t.state) for t in orchestrator.repo.list()}
    finally:
        config.settings = original

    assert states <= {TaskState.DONE, TaskState.AWAITING_APPROVAL, TaskState.NEEDS_CLARIFICATION}
    assert TaskState.RECEIVED not in states


def test_workers_mode_returns_before_the_task_runs(session, stub_execution):
    """The visible win of this phase: intake stops blocking through a sandbox run."""
    from ley_khaa.api.app import build_orchestrator
    from ley_khaa.projects.seeds import ensure_default_project

    original = _pin("workers")
    try:
        ensure_default_project(session)
        orchestrator = build_orchestrator(session)
        orchestrator.ingest(
            {"text": "compare the bloomberg universe against the factset universe, csv"}
        )
        orchestrator.sweep()
        tasks = orchestrator.repo.list()
    finally:
        config.settings = original

    assert tasks, "the candidate never became a task"
    # Created and left runnable — not driven.
    assert {TaskState(t.state) for t in tasks} == {TaskState.RECEIVED}
    assert TaskRepository(session).runnable_projects() == ["default"]


def test_workers_mode_leaves_an_approved_task_for_the_dispatcher(session, stub_execution):
    from ley_khaa.orchestrator.driver import TaskDriver
    from ley_khaa.persistence.candidate_repository import CandidateRepository
    from ley_khaa.persistence.message_repository import MessageRepository
    from ley_khaa.llm.heuristic import HeuristicLLM

    repo = TaskRepository(session)
    row = repo.create(project="acme", title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    repo.claim(row.id, expected=TaskState.INTERPRETED, target=TaskState.AWAITING_APPROVAL)

    driver = TaskDriver(
        repo,
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
    )
    original = _pin("workers")
    try:
        result = driver.approve(row.id)
    finally:
        config.settings = original

    # Approval performed its own transition and stopped. It did NOT run the task.
    assert TaskState(result.state) is TaskState.EXECUTING


def test_the_sweeper_does_not_drive_stalled_tasks_in_workers_mode(session):
    """Otherwise the sweeper drives tasks with no lease, in parallel with the
    dispatcher — exactly the double-lane bug the lease exists to prevent."""
    from ley_khaa.api.app import _sweep_once
    from ley_khaa.orchestrator.orchestrator import Orchestrator

    called: list[str] = []
    original_advance = Orchestrator.advance_stalled
    Orchestrator.advance_stalled = lambda self: called.append("advanced") or []
    original = _pin("workers")
    try:
        import ley_khaa.api.app as app_module

        original_factory = app_module.SessionLocal
        app_module.SessionLocal = lambda: session
        try:
            _sweep_once()
        finally:
            app_module.SessionLocal = original_factory
    finally:
        config.settings = original
        Orchestrator.advance_stalled = original_advance

    assert called == []
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_dispatch_modes.py -q`
Expected: FAIL — `test_workers_mode_returns_before_the_task_runs` finds a task already driven to
`done`, because nothing distinguishes the modes yet.

- [ ] **Step 3: Add `hand_off` to the driver**

In `backend/ley_khaa/orchestrator/driver.py`, add `from ..config import settings` and:

```python
    def hand_off(self, task_id: str) -> TaskRow:
        """Carry on after something made this task runnable.

        Inline mode drives it here, on the caller's thread — the behaviour every
        release before 0.6.0 had. In workers mode the task is now runnable and
        the dispatcher will lease it, so this returns immediately: an HTTP
        request must not block through two Opus calls and a sandbox run.

        advance() itself is unchanged and is what the dispatcher calls. The split
        is only about who does the driving, never about what driving means.
        """
        if settings.dispatch_mode == "inline":
            return self.advance(task_id)
        return self.repo.get(task_id)
```

Then replace the trailing `return self.advance(task_id)` in `approve`, `override` and `edit_spec`
with `return self.hand_off(task_id)`. Leave `advance()` itself alone.

- [ ] **Step 4: Hand off from the orchestrator too**

In `backend/ley_khaa/orchestrator/orchestrator.py`, replace both `self.driver.advance(task.id)` in
`_route_reply` and `self.driver.advance(task_id)` in `_promote` with `self.driver.hand_off(...)`.

- [ ] **Step 5: Stop the sweeper double-driving, and start the dispatcher**

In `backend/ley_khaa/api/app.py`, guard `advance_stalled` inside `_sweep_once`:

```python
        promoted = len(orchestrator.sweep())
        if settings.dispatch_mode == "inline":
            # In workers mode the dispatcher owns re-driving, and it does so
            # under a lease. Driving here as well would run a second lane over
            # a task a worker already holds.
            orchestrator.advance_stalled()
        return promoted
```

Add the dispatcher builder and wire it into `lifespan`:

```python
def _drive_task(session: Session, task_id: str) -> None:
    """What the dispatcher does with a leased task: exactly what every release
    before 0.6.0 did inline."""
    build_orchestrator(session).driver.advance(task_id)


def build_dispatcher() -> Dispatcher:
    return Dispatcher(SessionLocal, drive=_drive_task)
```

In `lifespan`, after the sweeper is created:

```python
    app.state.dispatcher = None
    if settings.dispatch_mode == "workers":
        app.state.dispatcher = asyncio.create_task(
            build_dispatcher().run_forever(settings.sweep_interval_seconds)
        )
```

and cancel it in the `finally`, exactly as the sweeper is cancelled:

```python
        for task in (app.state.sweeper, app.state.dispatcher):
            if task is None:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        app.state.sweeper = None
        app.state.dispatcher = None
```

- [ ] **Step 6: Pin inline mode in `conftest.py`**

Beside the other environment pins at the top:

```python
# The existing suite asserts on tasks that have already run. Workers mode is
# covered on its own terms in test_dispatcher.py and test_concurrency.py.
os.environ["LEY_KHAA_DISPATCH"] = "inline"
```

- [ ] **Step 7: Run the tests**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_dispatch_modes.py -q`
Expected: 4 passed.

- [ ] **Step 8: Prove they fail for the right reason**

- Make `hand_off` always call `advance` → `test_workers_mode_returns_before_the_task_runs` and
  `test_workers_mode_leaves_an_approved_task_for_the_dispatcher` must both fail.
- Make `hand_off` never call `advance` → `test_inline_mode_drives_the_task_on_the_calling_thread`
  must fail. **Both directions need a test**, or the mode switch can be broken one way in silence.
- Remove the `dispatch_mode == "inline"` guard around `advance_stalled` →
  `test_the_sweeper_does_not_drive_stalled_tasks_in_workers_mode` must fail.

- [ ] **Step 9: Run the whole suite**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 573 passed, 0 skipped, 0 warnings.

- [ ] **Step 10: Commit**

```bash
git add backend/ley_khaa/orchestrator/driver.py backend/ley_khaa/orchestrator/orchestrator.py \
        backend/ley_khaa/api/app.py backend/tests/conftest.py backend/tests/test_dispatch_modes.py
git commit -m "feat(orchestrator): hand tasks to the dispatcher instead of driving them inline"
```

---

### Task 8: The concurrency proof

The phase's headline claim, proven by a barrier rather than a sleep. A sleep-based test passes when
the code is serial and the machine is slow; a barrier deadlocks, so it cannot pass for the wrong
reason.

**Files:**
- Test: `backend/tests/test_concurrency.py`

- [ ] **Step 1: Write the test**

```python
# backend/tests/test_concurrency.py
"""Two projects run at once; one project runs one task at a time (spec §8).

The barrier is the point. `drive` for project A waits on a barrier that only
project B's `drive` can release, so if the dispatcher were serial this test
would block until the barrier's own timeout and fail — it cannot pass by
accident of timing the way a sleep-based test can.
"""
import asyncio
import threading

from ley_khaa.domain.states import TaskState
from ley_khaa.orchestrator.dispatcher import Dispatcher
from ley_khaa.persistence.repository import TaskRepository


def _task(session, project):
    repo = TaskRepository(session)
    row = repo.create(project=project, title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    return row.id


def test_two_projects_genuinely_run_at_the_same_time(session):
    _task(session, "acme")
    _task(session, "globex")

    met = threading.Barrier(2, timeout=5)

    def drive(_session, _task_id):
        # Blocks until BOTH workers arrive. Serial execution never gets a second
        # arrival, so this raises BrokenBarrierError on timeout.
        met.wait()

    driven = asyncio.run(Dispatcher(lambda: session, drive=drive).tick())
    assert len(driven) == 2
    assert not met.broken


def test_one_project_runs_one_task_at_a_time(session):
    """The other half of the claim. Two tasks in ONE project must not overlap,
    so a barrier expecting two arrivals must break."""
    _task(session, "acme")
    _task(session, "acme")

    met = threading.Barrier(2, timeout=1)
    overlapped: list[bool] = []

    def drive(_session, _task_id):
        try:
            met.wait()
            overlapped.append(True)
        except threading.BrokenBarrierError:
            overlapped.append(False)

    async def two_ticks():
        first = await Dispatcher(lambda: session, drive=drive).tick()
        second = await Dispatcher(lambda: session, drive=drive).tick()
        return first + second

    driven = asyncio.run(two_ticks())

    assert len(driven) == 2, "both tasks should eventually run"
    assert overlapped == [False, False], "two tasks in one project must never overlap"
```

- [ ] **Step 2: Run it**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_concurrency.py -q`
Expected: 2 passed, in about a second.

- [ ] **Step 3: Prove it fails for the right reason**

Force the dispatcher serial — replace `asyncio.gather(...)` in `tick()` with a plain `for` loop
awaiting each project in turn. `test_two_projects_genuinely_run_at_the_same_time` must fail with
`BrokenBarrierError`. Restore `gather`.

This is the single most important mutation in the plan: if that edit does not turn this test red,
the test is not proving concurrency and must be fixed before the task is accepted.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_concurrency.py
git commit -m "test(dispatcher): prove cross-project concurrency with a barrier"
```

---

### Task 9: `AmendmentDetector`

**Files:**
- Create: `backend/ley_khaa/orchestrator/amendment.py`
- Modify: `backend/ley_khaa/llm/router.py`, `backend/ley_khaa/llm/heuristic.py`
- Test: `backend/tests/test_amendment_detector.py`

**Interfaces:**
- Produces:
  - `AmendmentChoice(task_id: str | None, confidence: float, reason: str)` — stage-2 output model
  - `AmendmentProposal(task_id: str, confidence: float, reason: str)` — frozen dataclass
  - `AmendmentDetector(repo, llm).detect(project, title, summary, exclude_task_ids=()) -> AmendmentProposal | None`
  - `Stage.AMENDMENT_MATCH`
  - `AMENDMENT_CONFIDENCE_FLOOR = 0.8`
  - `TaskRepository.active_in_project(project) -> list[TaskRow]`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_amendment_detector.py
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.client import FakeLLM
from ley_khaa.orchestrator.amendment import AmendmentChoice, AmendmentDetector
from ley_khaa.persistence.repository import TaskRepository


def _task(session, *, project="acme", state=TaskState.AWAITING_APPROVAL, title="universe check"):
    repo = TaskRepository(session)
    row = repo.create(project=project, title=title, source_message_ids=[])
    if state is not TaskState.RECEIVED:
        repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    if state not in (TaskState.RECEIVED, TaskState.CLASSIFIED):
        repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    if state not in (TaskState.RECEIVED, TaskState.CLASSIFIED, TaskState.INTERPRETED):
        repo.claim(row.id, expected=TaskState.INTERPRETED, target=state)
    return repo.get(row.id)


def test_a_project_with_no_active_tasks_costs_nothing(session):
    """Stage 1 is free and is the common case — almost every request arrives
    into a project with nothing running."""
    llm = FakeLLM(responses=[])
    detector = AmendmentDetector(TaskRepository(session), llm)
    assert detector.detect(project="acme", title="t", summary="s") is None
    assert llm.calls == []


def test_a_done_task_is_not_active(session):
    _task(session, state=TaskState.DONE)
    llm = FakeLLM(responses=[])
    assert AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s"
    ) is None
    assert llm.calls == []


def test_a_task_parked_for_a_human_IS_active(session):
    """Deliberate: a task waiting in front of a person is the one a follow-up
    message is most likely to be amending."""
    target = _task(session, state=TaskState.AWAITING_APPROVAL)
    llm = FakeLLM(
        responses=[AmendmentChoice(task_id=target.id, confidence=0.9, reason="also flag dupes")]
    )
    proposal = AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="also flag duplicates", summary="s"
    )
    assert proposal is not None
    assert proposal.task_id == target.id
    assert len(llm.calls) == 1


def test_a_low_confidence_answer_is_no_proposal(session):
    target = _task(session)
    llm = FakeLLM(responses=[AmendmentChoice(task_id=target.id, confidence=0.5, reason="maybe")])
    assert AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s"
    ) is None


def test_a_hallucinated_task_id_is_discarded(session):
    _task(session)
    llm = FakeLLM(responses=[AmendmentChoice(task_id="not-a-task", confidence=0.99, reason="x")])
    assert AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s"
    ) is None


def test_a_task_in_another_project_is_never_proposed(session):
    """Cross-project amendment is out of scope, and a task from another client's
    project must never be offered as a fold target."""
    other = _task(session, project="globex")
    _task(session, project="acme")
    llm = FakeLLM(responses=[AmendmentChoice(task_id=other.id, confidence=0.99, reason="x")])
    proposal = AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s"
    )
    assert proposal is None


def test_the_candidate_s_own_task_is_excluded(session):
    """Guards against a task proposing itself as its own amendment target."""
    mine = _task(session)
    llm = FakeLLM(responses=[])
    assert AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s", exclude_task_ids=(mine.id,)
    ) is None
    assert llm.calls == []


def test_a_failing_model_call_yields_no_proposal(session):
    """A missed amendment costs one duplicate task a human can see and reject.
    A detector that raises would take intake down with it."""
    _task(session)
    llm = FakeLLM(responses=[RuntimeError("transport exploded")])
    assert AmendmentDetector(TaskRepository(session), llm).detect(
        project="acme", title="t", summary="s"
    ) is None
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_amendment_detector.py -q`
Expected: FAIL — `ModuleNotFoundError: ley_khaa.orchestrator.amendment`.

- [ ] **Step 3: Add the stage and the repository query**

`backend/ley_khaa/llm/router.py`: add `AMENDMENT_MATCH = "amendment_match"` to `Stage`,
`Stage.AMENDMENT_MATCH: {"routine": HAIKU, "hard": HAIKU}` to `_POLICY`, and
`Stage.AMENDMENT_MATCH: 1024` to `_MAX_TOKENS`.

`backend/ley_khaa/persistence/repository.py`, on `TaskRepository`:

```python
    def active_in_project(self, project: str) -> list[TaskRow]:
        """Tasks in this project that are not finished.

        Deliberately includes AWAITING_APPROVAL and NEEDS_CLARIFICATION: a task
        parked in front of a person is exactly the one a follow-up message is
        most likely to be amending.
        """
        finished = [s.value for s in TERMINAL]
        return list(
            self.session.scalars(
                select(TaskRow)
                .where(TaskRow.project == project, TaskRow.state.not_in(finished))
                .order_by(TaskRow.created_at)
            )
        )
```

- [ ] **Step 4: Write the detector**

```python
# backend/ley_khaa/orchestrator/amendment.py
"""Is this new request a follow-up to something already running? (spec §5.9)

Two stages, like every other matcher here. Stage 1 is free and answers "does
this project have anything active at all?" — almost always no, so the common
path costs nothing. Stage 2 is one Haiku call, and its answer is untrusted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from collections.abc import Iterable

from pydantic import BaseModel

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.orm import TaskRow
from ..persistence.repository import TaskRepository

logger = logging.getLogger(__name__)

# Same floor and same reasoning as the other two matchers.
AMENDMENT_CONFIDENCE_FLOOR = 0.8

SYSTEM = """You decide whether a new request modifies a task that is already underway.

You are given the new request and a list of active tasks with their ids and specifications.
Answer with the id of the task this request MODIFIES — adds to, narrows, corrects — or null
if it is a separate piece of work.

Say null unless you are confident. Folding a separate request into a running task loses the
separate request. A null costs only that a duplicate-looking task appears, which a human can
see and reject."""


class AmendmentChoice(BaseModel):
    """Stage 2's answer. `task_id` is an id from the list shown, or null."""

    task_id: str | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class AmendmentProposal:
    task_id: str
    confidence: float
    # The model's own sentence, shown to whoever decides.
    reason: str


class AmendmentDetector:
    def __init__(self, repo: TaskRepository, llm: LLMClient) -> None:
        self.repo = repo
        self.llm = llm

    def detect(
        self,
        *,
        project: str,
        title: str,
        summary: str,
        exclude_task_ids: Iterable[str] = (),
    ) -> AmendmentProposal | None:
        try:
            return self._detect(project, title, summary, set(exclude_task_ids))
        except Exception:
            # A detector that raises takes intake down. A detector that misses
            # costs one duplicate task a human can reject.
            logger.exception("amendment detection failed; treating this as a new request")
            return None

    def _detect(
        self, project: str, title: str, summary: str, exclude: set[str]
    ) -> AmendmentProposal | None:
        active = [t for t in self.repo.active_in_project(project) if t.id not in exclude]
        if not active:
            return None

        choice = self.llm.parse(
            choice=model_for(Stage.AMENDMENT_MATCH),
            system=SYSTEM,
            user=_prompt(title, summary, active),
            output_format=AmendmentChoice,
        )
        if not choice.task_id or choice.confidence < AMENDMENT_CONFIDENCE_FLOOR:
            return None

        # Untrusted output: the id must be one we actually showed it, which is
        # also what keeps another project's task from ever being named.
        target = next((t for t in active if t.id == choice.task_id), None)
        if target is None:
            logger.info("amendment detector named an unknown task %r", choice.task_id)
            return None
        return AmendmentProposal(
            task_id=target.id, confidence=choice.confidence, reason=choice.reason
        )


def _prompt(title: str, summary: str, active: list[TaskRow]) -> str:
    lines = ["## New request", f"title: {title}", f"summary: {summary}", "", "## Active tasks"]
    for task in active:
        spec = task.spec or {}
        lines.append(
            f"- [{task.id}] {task.title} (state: {task.state}; "
            f"intent: {spec.get('intent', 'not yet interpreted')})"
        )
    return "\n".join(lines)
```

- [ ] **Step 5: Add the offline rule**

In `backend/ley_khaa/llm/heuristic.py`, import `AmendmentChoice` and add to `parse`:

```python
        if output_format is AmendmentChoice:
            # A regex cannot tell "also flag duplicates" (an amendment) from
            # "also run the credit book" (a new request), and folding a separate
            # request into a running task LOSES it. Offline, everything is new.
            return AmendmentChoice(task_id=None, confidence=0.0, reason="offline: no detection")
```

- [ ] **Step 6: Run, then prove each test fails for the right reason**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_amendment_detector.py -q`
Expected: 8 passed.

- Delete the `if not active: return None` early return →
  `test_a_project_with_no_active_tasks_costs_nothing` must fail on `llm.calls == []`.
- Include `TERMINAL` states in `active_in_project` → `test_a_done_task_is_not_active` must fail.
- Delete the `target is None` guard → `test_a_hallucinated_task_id_is_discarded` and
  `test_a_task_in_another_project_is_never_proposed` must both fail.
- Drop the `exclude` filter → `test_the_candidate_s_own_task_is_excluded` must fail.

- [ ] **Step 7: Whole suite, then commit**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 583 passed, 0 skipped, 0 warnings.

```bash
git add backend/ley_khaa/orchestrator/amendment.py backend/ley_khaa/llm/router.py \
        backend/ley_khaa/llm/heuristic.py backend/ley_khaa/persistence/repository.py \
        backend/tests/test_amendment_detector.py
git commit -m "feat(orchestrator): detect amendments to active tasks in a project"
```

---

### Task 10: `recommend_fold` — the dial decides, the guard overrides

**Files:**
- Modify: `backend/ley_khaa/autonomy/engine.py`
- Test: `backend/tests/test_autonomy_fold.py`

**Interfaces:**
- Produces: `FoldDecision(fold: bool, reason: str)`;
  `recommend_fold(*, mode, detector_confidence, target_state, target_missing_fields) -> FoldDecision`;
  `PAST_NO_RETURN: frozenset[TaskState]`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_autonomy_fold.py
from ley_khaa.autonomy.engine import recommend_fold
from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.domain.states import TaskState


def test_auto_and_a_confident_detector_folds():
    decision = recommend_fold(
        mode=AutonomyMode.AUTO,
        detector_confidence=0.95,
        target_state=TaskState.AWAITING_APPROVAL,
        target_missing_fields=[],
    )
    assert decision.fold is True


def test_suggest_never_folds_on_its_own():
    decision = recommend_fold(
        mode=AutonomyMode.SUGGEST,
        detector_confidence=0.99,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=[],
    )
    assert decision.fold is False
    assert "suggest" in decision.reason.lower()


def test_copilot_never_folds_on_its_own():
    decision = recommend_fold(
        mode=AutonomyMode.COPILOT,
        detector_confidence=0.99,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=[],
    )
    assert decision.fold is False


def test_an_executing_target_never_folds_even_on_auto():
    """The structural guard (spec §3.8). A live sandbox workspace and a
    half-written bundle are facts, not thresholds — no confidence and no mode
    may reach past this."""
    for state in (TaskState.EXECUTING, TaskState.VALIDATING):
        decision = recommend_fold(
            mode=AutonomyMode.AUTO,
            detector_confidence=1.0,
            target_state=state,
            target_missing_fields=[],
        )
        assert decision.fold is False, state
        assert "under way" in decision.reason


def test_a_target_with_gaps_does_not_fold_automatically():
    decision = recommend_fold(
        mode=AutonomyMode.AUTO,
        detector_confidence=1.0,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=["output_format"],
    )
    assert decision.fold is False


def test_a_marginal_detector_confidence_does_not_fold():
    decision = recommend_fold(
        mode=AutonomyMode.AUTO,
        detector_confidence=0.85,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=[],
    )
    assert decision.fold is False


def test_no_mode_at_all_does_not_fold():
    """effective_mode is None until the dial has scored the target. Absence of
    a mode is not permission."""
    decision = recommend_fold(
        mode=None,
        detector_confidence=1.0,
        target_state=TaskState.CLASSIFIED,
        target_missing_fields=[],
    )
    assert decision.fold is False
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_autonomy_fold.py -q`
Expected: FAIL — `ImportError: cannot import name 'recommend_fold'`.

- [ ] **Step 3: Implement**

Append to `backend/ley_khaa/autonomy/engine.py`:

```python
# --- folding an amendment (spec §3.8) -------------------------------------
# Past these states a task owns a live sandbox workspace and a half-written
# bundle. Folding would mutate work already in flight, so these ALWAYS go to a
# human — this is a structural fact about the executor, not a threshold, and it
# is checked before any scoring so no tuning can reach past it.
PAST_NO_RETURN: frozenset[TaskState] = frozenset(
    {TaskState.EXECUTING, TaskState.VALIDATING}
)

# Folding is destructive in one direction: a separate request folded into a task
# is no longer separate. Auto needs more than the detector's own floor.
_AUTO_FOLD_CONFIDENCE = 0.9


@dataclass(frozen=True)
class FoldDecision:
    fold: bool
    reason: str


def recommend_fold(
    *,
    mode: AutonomyMode | None,
    detector_confidence: float,
    target_state: TaskState,
    target_missing_fields: list[str],
) -> FoldDecision:
    """Should this amendment be folded in without asking?

    Pure and deterministic, like recommend(). The dial governs the decision; the
    guard above governs the dial.
    """
    if target_state in PAST_NO_RETURN:
        return FoldDecision(
            fold=False,
            reason=f"the task is already under way ({target_state.value}) — asking first",
        )
    if mode is not AutonomyMode.AUTO:
        label = mode.value if mode is not None else "unscored"
        return FoldDecision(fold=False, reason=f"the task is in {label} — asking first")
    if target_missing_fields:
        return FoldDecision(
            fold=False,
            reason=f"the task still has {len(target_missing_fields)} unknown field(s) — asking first",
        )
    if detector_confidence < _AUTO_FOLD_CONFIDENCE:
        return FoldDecision(
            fold=False,
            reason=f"only {detector_confidence:.0%} sure this is an amendment — asking first",
        )
    return FoldDecision(
        fold=True, reason=f"{detector_confidence:.0%} sure, and the task is in Auto — folding it in"
    )
```

Add `from ..domain.states import TaskState` to the module's imports.

- [ ] **Step 4: Run, then prove each branch fails for the right reason**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_autonomy_fold.py -q`
Expected: 7 passed.

Delete each of the four guards in turn — `PAST_NO_RETURN`, the mode check, the missing-fields
check, the confidence check — and confirm that **exactly one** test turns red for each. Four
guards, four tests: if deleting one guard breaks nothing, that guard is unpinned and can be removed
silently later.

Then move the `PAST_NO_RETURN` check to *after* the mode check and confirm
`test_an_executing_target_never_folds_even_on_auto` still passes — it will, because the mode is
`AUTO` there. That is why the guard's position needs stating in the docstring rather than being
inferred from behaviour: **its ordering is not observable from the outside**, so it is a review
item, not a test item. Restore it to first.

- [ ] **Step 5: Whole suite, then commit**

```bash
git add backend/ley_khaa/autonomy/engine.py backend/tests/test_autonomy_fold.py
git commit -m "feat(autonomy): let the dial decide amendment folds behind a structural guard"
```

---

### Task 11: Folding — new state edges, triage claims, and `fold_into`

**Files:**
- Modify: `backend/ley_khaa/domain/states.py`, `backend/ley_khaa/crystallizer/candidate.py`,
  `backend/ley_khaa/persistence/candidate_repository.py`,
  `backend/ley_khaa/persistence/repository.py`
- Test: `backend/tests/test_folding.py`

**Interfaces:**
- Produces:
  - `CandidateState.AWAITING_TRIAGE`
  - `CandidateRepository.claim_for_triage(candidate_id, *, task_id, reason, confidence) -> bool`
  - `CandidateRepository.claim_for_fold(candidate_id) -> bool`
  - `TaskRepository.fold_into(task_id, *, message_ids, expected: TaskState) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_folding.py
import pytest

from ley_khaa.crystallizer.candidate import CandidateState, InvalidCandidateTransition
from ley_khaa.domain.states import TaskState, can_transition
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.repository import TaskRepository


def _candidate(session, state=CandidateState.READY):
    return CandidateRepository(session).upsert(
        conversation_id="C1",
        candidate_key="k",
        title="also flag duplicates",
        summary="s",
        state=state,
        message_ids=["m2"],
        missing_fields=[],
        open_question=None,
    )


def _task(session, state=TaskState.AWAITING_APPROVAL):
    repo = TaskRepository(session)
    row = repo.create(project="acme", title="t", source_message_ids=["m1"])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    if state is not TaskState.CLASSIFIED:
        repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    if state not in (TaskState.CLASSIFIED, TaskState.INTERPRETED):
        repo.claim(row.id, expected=TaskState.INTERPRETED, target=state)
    return repo.get(row.id)


def test_an_interpreted_task_can_go_back_to_classified():
    """Folding re-interprets over the enlarged message set, so it needs this
    edge. Declared because it is reachable — phase 2 removed one that was not."""
    assert can_transition(TaskState.INTERPRETED, TaskState.CLASSIFIED)


def test_a_parked_task_can_go_back_to_classified():
    assert can_transition(TaskState.AWAITING_APPROVAL, TaskState.CLASSIFIED)


def test_executing_still_cannot_go_back_to_classified():
    """The structural guard's counterpart in the state table: even if some caller
    tried, the machine refuses."""
    assert not can_transition(TaskState.EXECUTING, TaskState.CLASSIFIED)


def test_a_ready_candidate_can_be_claimed_for_triage(session):
    candidate = _candidate(session)
    repo = CandidateRepository(session)
    assert repo.claim_for_triage(
        candidate.id, task_id="t1", reason="also flag dupes", confidence=0.9
    ) is True
    row = repo.get(candidate.id)
    assert CandidateState(row.state) is CandidateState.AWAITING_TRIAGE
    assert row.amends_task_id == "t1"
    assert row.amendment_confidence == 0.9


def test_only_one_caller_wins_the_triage_claim(session):
    candidate = _candidate(session)
    repo = CandidateRepository(session)
    assert repo.claim_for_triage(candidate.id, task_id="t1", reason="r", confidence=0.9) is True
    assert repo.claim_for_triage(candidate.id, task_id="t2", reason="r", confidence=0.9) is False
    assert repo.get(candidate.id).amends_task_id == "t1"


def test_a_triaged_candidate_can_be_claimed_for_folding(session):
    candidate = _candidate(session)
    repo = CandidateRepository(session)
    repo.claim_for_triage(candidate.id, task_id="t1", reason="r", confidence=0.9)
    assert repo.claim_for_fold(candidate.id) is True
    assert CandidateState(repo.get(candidate.id).state) is CandidateState.PROMOTED
    assert repo.claim_for_fold(candidate.id) is False


def test_a_promoted_candidate_cannot_slide_back_to_triage(session):
    """PROMOTED stays terminal. A folded candidate is done."""
    repo = CandidateRepository(session)
    candidate = _candidate(session)
    repo.claim_for_promotion(candidate.id)
    with pytest.raises(InvalidCandidateTransition):
        _candidate(session, state=CandidateState.AWAITING_TRIAGE)


def test_fold_into_appends_messages_and_reopens_the_task(session):
    task = _task(session, TaskState.AWAITING_APPROVAL)
    repo = TaskRepository(session)
    assert repo.fold_into(
        task.id, message_ids=["m2"], expected=TaskState.AWAITING_APPROVAL
    ) is True
    row = repo.get(task.id)
    assert row.source_message_ids == ["m1", "m2"]
    assert TaskState(row.state) is TaskState.CLASSIFIED
    assert row.open_question is None


def test_fold_into_loses_when_the_task_has_already_moved(session):
    """The race the spec names: the target can move between the decision and the
    fold. The loser must change nothing at all."""
    task = _task(session, TaskState.EXECUTING)
    repo = TaskRepository(session)
    assert repo.fold_into(
        task.id, message_ids=["m2"], expected=TaskState.AWAITING_APPROVAL
    ) is False
    row = repo.get(task.id)
    assert row.source_message_ids == ["m1"], "a lost fold must not append messages"
    assert TaskState(row.state) is TaskState.EXECUTING
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_folding.py -q`
Expected: FAIL — the first two on `can_transition`, the rest on missing attributes.

- [ ] **Step 3: Add the two task-state edges**

In `backend/ley_khaa/domain/states.py`, extend `_ALLOWED`. Add `TaskState.CLASSIFIED` to the
`INTERPRETED` and `AWAITING_APPROVAL` sets, with the reason recorded:

```python
    # Folding an amendment re-interprets the task over its enlarged message set,
    # so both of these lead back to CLASSIFIED. EXECUTING and VALIDATING
    # deliberately do NOT: a task with a live sandbox workspace is past the
    # point where its inputs can change (autonomy/engine.py::PAST_NO_RETURN).
    TaskState.INTERPRETED: {
        TaskState.AWAITING_APPROVAL,
        TaskState.EXECUTING,
        TaskState.CLASSIFIED,
        TaskState.FAILED,
    },
    TaskState.AWAITING_APPROVAL: {
        TaskState.EXECUTING,
        TaskState.INTERPRETED,
        TaskState.CLASSIFIED,
        TaskState.NEEDS_CLARIFICATION,
        TaskState.FAILED,
    },
```

- [ ] **Step 4: Add the candidate state**

In `backend/ley_khaa/crystallizer/candidate.py`, add `AWAITING_TRIAGE = "awaiting_triage"` to
`CandidateState`, add it to `READY`'s allowed set, and give it its own entry:

```python
    # A candidate whose amendment proposal is waiting on a human. Reachable only
    # from READY, and it ends the same two ways any candidate ends: PROMOTED (the
    # human folded it or ran it separately) or ABANDONED.
    CandidateState.AWAITING_TRIAGE: {
        CandidateState.PROMOTED,
        CandidateState.ABANDONED,
    },
```

`TERMINAL_STATES` is unchanged: `AWAITING_TRIAGE` is not terminal.

- [ ] **Step 5: Add the two candidate claims**

In `backend/ley_khaa/persistence/candidate_repository.py`:

```python
    def claim_for_triage(
        self, candidate_id: str, *, task_id: str, reason: str, confidence: float
    ) -> bool:
        """Park a candidate on an amendment decision. True if we won it.

        Same conditional-update discipline as claim_for_promotion, and for the
        same reason: two workers can read the same candidate as READY, and only
        one of them may attach a proposal to it.
        """
        result = self.session.execute(
            update(CandidateRow)
            .where(
                CandidateRow.id == candidate_id,
                CandidateRow.state == CandidateState.READY.value,
            )
            .values(
                state=CandidateState.AWAITING_TRIAGE.value,
                amends_task_id=task_id,
                amendment_reason=reason,
                amendment_confidence=confidence,
                updated_at=datetime.now(timezone.utc),
            )
        )
        self.session.commit()
        return result.rowcount == 1

    def claim_for_fold(self, candidate_id: str) -> bool:
        """Take a triaged candidate out of AWAITING_TRIAGE.

        It goes to PROMOTED, not to a state of its own: PROMOTED means "this
        candidate's request is now carried by a task", which is exactly true of
        a folded candidate — the task is simply one that already existed.
        """
        result = self.session.execute(
            update(CandidateRow)
            .where(
                CandidateRow.id == candidate_id,
                CandidateRow.state == CandidateState.AWAITING_TRIAGE.value,
            )
            .values(
                state=CandidateState.PROMOTED.value, updated_at=datetime.now(timezone.utc)
            )
        )
        self.session.commit()
        return result.rowcount == 1
```

- [ ] **Step 6: Add `fold_into`**

In `backend/ley_khaa/persistence/repository.py`:

```python
    def fold_into(
        self, task_id: str, *, message_ids: list[str], expected: TaskState
    ) -> bool:
        """Merge an amendment's messages into a task and send it back to be
        re-interpreted. True if we won the race.

        Conditional on `expected` — the state observed when the fold was decided
        — because the target can move on in between. A loser must change NOTHING:
        the claim therefore comes first, and the messages are appended only after
        it wins. Appending first would leave a foreign message on a task that is
        already executing, where nothing will ever re-read it.

        This is the same shape as _route_reply's answered-clarification path: the
        amendment is re-INTERPRETED over the enlarged message set, never stapled
        onto the old spec.
        """
        if not self.claim(task_id, expected=expected, target=TaskState.CLASSIFIED):
            return False
        self.append_source_messages(task_id, message_ids)
        self.set_open_question(task_id, None)
        return True
```

- [ ] **Step 7: Run, then prove they fail for the right reason**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_folding.py -q`
Expected: 9 passed.

- Remove `TaskState.CLASSIFIED` from `INTERPRETED`'s set → the first edge test must fail; remove it
  from `AWAITING_APPROVAL`'s set → the second must. **Each edge needs its own test.**
- Reverse `fold_into` so it appends before claiming →
  `test_fold_into_loses_when_the_task_has_already_moved` must fail on the `["m1"]` assertion. This
  is the assertion that makes the ordering load-bearing rather than stylistic.
- Drop the `state == READY` guard from `claim_for_triage` →
  `test_only_one_caller_wins_the_triage_claim` must fail.

- [ ] **Step 8: Whole suite, then commit**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 592 passed, 0 skipped, 0 warnings.

```bash
git add backend/ley_khaa/domain/states.py backend/ley_khaa/crystallizer/candidate.py \
        backend/ley_khaa/persistence/candidate_repository.py \
        backend/ley_khaa/persistence/repository.py backend/tests/test_folding.py
git commit -m "feat(orchestrator): add triage states and the fold-into-a-task operation"
```

---

### Task 12: Wire detection into promotion

**Files:**
- Modify: `backend/ley_khaa/orchestrator/orchestrator.py`
- Test: `backend/tests/test_amendment_flow.py`

**Interfaces:**
- Consumes: `AmendmentDetector` (Task 9), `recommend_fold` (Task 10), `fold_into` /
  `claim_for_triage` / `claim_for_fold` (Task 11).
- Produces: `Orchestrator.fold(candidate_id) -> str`, `Orchestrator.separate(candidate_id) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_amendment_flow.py
from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.crystallizer.candidate import CandidateState
from ley_khaa.domain.states import TaskState
from ley_khaa.orchestrator.amendment import AmendmentProposal


class _Detector:
    """Stands in for the model. Returns a fixed proposal, and records that it
    was consulted so a test can prove stage 1 short-circuited."""

    def __init__(self, proposal=None):
        self.proposal = proposal
        self.calls = 0

    def detect(self, **kwargs):
        self.calls += 1
        return self.proposal


def _orchestrator(session):
    from ley_khaa.api.app import build_orchestrator
    from ley_khaa.projects.seeds import ensure_default_project

    ensure_default_project(session)
    return build_orchestrator(session)


def _running_task(orchestrator, *, mode, state=TaskState.AWAITING_APPROVAL):
    repo = orchestrator.repo
    row = repo.create(project="default", title="universe check", source_message_ids=["m1"])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    if state is not TaskState.INTERPRETED:
        repo.claim(row.id, expected=TaskState.INTERPRETED, target=state)
    repo.save_recommendation(row.id, mode=mode.value, confidence=0.9, risk=0.1, reason="x")
    return repo.get(row.id)


def _ready_candidate(orchestrator):
    return orchestrator.candidates.upsert(
        conversation_id="C1",
        candidate_key="k2",
        title="also flag duplicates",
        summary="s",
        state=CandidateState.READY,
        message_ids=["m2"],
        missing_fields=[],
        open_question=None,
    )


def test_auto_folds_the_amendment_without_creating_a_second_task(session, stub_execution):
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.AUTO)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="also flag dupes")
    )

    result = orchestrator._promote(candidate)

    assert result == target.id, "folding must reuse the target task, not create one"
    assert len(orchestrator.repo.list()) == 1
    folded = orchestrator.repo.get(target.id)
    assert "m2" in folded.source_message_ids
    assert orchestrator.candidates.get(candidate.id).task_id == target.id


def test_suggest_parks_the_amendment_for_a_human(session, stub_execution):
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.SUGGEST)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="also flag dupes")
    )

    assert orchestrator._promote(candidate) is None
    parked = orchestrator.candidates.get(candidate.id)
    assert CandidateState(parked.state) is CandidateState.AWAITING_TRIAGE
    assert parked.amends_task_id == target.id
    assert len(orchestrator.repo.list()) == 1, "parking must not create a task"


def test_an_executing_target_parks_even_on_auto(session, stub_execution):
    """The structural guard, end to end rather than in the engine alone."""
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.AUTO, state=TaskState.EXECUTING)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=1.0, reason="also flag dupes")
    )

    assert orchestrator._promote(candidate) is None
    parked = orchestrator.candidates.get(candidate.id)
    assert CandidateState(parked.state) is CandidateState.AWAITING_TRIAGE
    assert TaskState(orchestrator.repo.get(target.id).state) is TaskState.EXECUTING


def test_no_proposal_promotes_normally(session, stub_execution):
    orchestrator = _orchestrator(session)
    _running_task(orchestrator, mode=AutonomyMode.AUTO)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(None)

    task_id = orchestrator._promote(candidate)
    assert task_id is not None
    assert len(orchestrator.repo.list()) == 2


def test_a_human_fold_merges_the_parked_candidate(session, stub_execution):
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.SUGGEST)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="r")
    )
    orchestrator._promote(candidate)

    assert orchestrator.fold(candidate.id) == target.id
    assert "m2" in orchestrator.repo.get(target.id).source_message_ids
    assert CandidateState(orchestrator.candidates.get(candidate.id).state) is CandidateState.PROMOTED


def test_a_human_separate_promotes_it_as_its_own_task(session, stub_execution):
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.SUGGEST)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="r")
    )
    orchestrator._promote(candidate)

    new_id = orchestrator.separate(candidate.id)
    assert new_id != target.id
    assert len(orchestrator.repo.list()) == 2
    assert "m2" not in orchestrator.repo.get(target.id).source_message_ids


def test_a_fold_that_loses_the_race_returns_the_candidate_to_triage(session, stub_execution):
    """The target moved between the decision and the fold. Nothing is lost: the
    candidate stays parked and a human sees it again."""
    orchestrator = _orchestrator(session)
    target = _running_task(orchestrator, mode=AutonomyMode.SUGGEST)
    candidate = _ready_candidate(orchestrator)
    orchestrator.amendments = _Detector(
        AmendmentProposal(task_id=target.id, confidence=0.95, reason="r")
    )
    orchestrator._promote(candidate)

    # The target races ahead into a state a fold may not touch.
    orchestrator.repo.claim(
        target.id, expected=TaskState.AWAITING_APPROVAL, target=TaskState.EXECUTING
    )

    assert orchestrator.fold(candidate.id) is None
    still_parked = orchestrator.candidates.get(candidate.id)
    assert CandidateState(still_parked.state) is CandidateState.AWAITING_TRIAGE
    assert "m2" not in orchestrator.repo.get(target.id).source_message_ids
```

- [ ] **Step 2: Run them and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_amendment_flow.py -q`
Expected: FAIL — `AttributeError: 'Orchestrator' object has no attribute 'amendments'`.

- [ ] **Step 3: Implement**

In `backend/ley_khaa/orchestrator/orchestrator.py`, add the imports:

```python
from ..autonomy.engine import recommend_fold
from ..autonomy.modes import AutonomyMode
from .amendment import AmendmentDetector
```

In `__init__`, after the router:

```python
        self.amendments = AmendmentDetector(repo, llm)
```

Replace `_promote` with the amendment-aware version:

```python
    def _promote(self, candidate: CandidateRow) -> str | None:
        """Turn a settled candidate into work: a new task, a fold into a running
        one, or a decision parked for a human.

        Returns the task id the candidate's request now lives in — which for a
        fold is the EXISTING task — or None when it created no work (a lost
        claim, or a proposal parked for triage).

        Routing and detection run BEFORE any claim, which is forced rather than
        careless: the two outcomes take different claims (claim_for_promotion vs
        claim_for_triage), so which one to attempt is not known until the
        proposal exists. The cost is that a caller who then loses the race has
        paid for a routing call; the claims still guarantee exactly one of them
        creates work.
        """
        project = self._route(candidate)
        proposal = self.amendments.detect(
            project=project, title=candidate.title, summary=candidate.summary
        )
        if proposal is not None:
            return self._handle_amendment(candidate, proposal)

        if not self.candidates.claim_for_promotion(candidate.id):
            return None
        task = self.repo.create(
            project=project,
            title=candidate.title,
            source_message_ids=list(candidate.message_ids),
            candidate_id=candidate.id,
        )
        self.candidates.attach_task(candidate.id, task.id)
        self.driver.hand_off(task.id)
        return task.id

    def _handle_amendment(
        self, candidate: CandidateRow, proposal: AmendmentProposal
    ) -> str | None:
        target = self.repo.get(proposal.task_id)
        if target is None:
            return None
        spec = target.spec or {}
        decision = recommend_fold(
            mode=AutonomyMode(target.effective_mode) if target.effective_mode else None,
            detector_confidence=proposal.confidence,
            target_state=TaskState(target.state),
            target_missing_fields=list(spec.get("missing_fields") or []),
        )
        if not decision.fold:
            # Park it. The candidate holds the decision, so no placeholder task
            # is created for work nobody has agreed to do yet.
            if not self.candidates.claim_for_triage(
                candidate.id,
                task_id=target.id,
                reason=f"{proposal.reason} — {decision.reason}",
                confidence=proposal.confidence,
            ):
                return None
            logger.info("candidate %s parked as an amendment to %s", candidate.id, target.id)
            return None

        return self._fold(candidate, target, claim=self.candidates.claim_for_promotion)

    def _fold(self, candidate: CandidateRow, target: TaskRow, *, claim) -> str | None:
        """Merge a candidate into a task. Shared by the automatic and human paths.

        The candidate is claimed FIRST: a fold that loses the candidate race must
        not have already changed the target.
        """
        if not claim(candidate.id):
            return None
        if not self.repo.fold_into(
            target.id,
            message_ids=list(candidate.message_ids),
            expected=TaskState(target.state),
        ):
            # The target moved on. Put the candidate back where a human can see
            # it rather than dropping the request.
            self.candidates.return_to_triage(candidate.id)
            return None
        self.candidates.attach_task(candidate.id, target.id)
        self.driver.hand_off(target.id)
        return target.id

    # --- human triage actions ----------------------------------------------

    def fold(self, candidate_id: str) -> str | None:
        """Fold a parked candidate into the task it amends."""
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        target = self.repo.get(candidate.amends_task_id or "")
        if target is None:
            raise KeyError(candidate.amends_task_id)
        return self._fold(candidate, target, claim=self.candidates.claim_for_fold)

    def separate(self, candidate_id: str) -> str | None:
        """Promote a parked candidate as its own task after all."""
        candidate = self.candidates.get(candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        if not self.candidates.claim_for_fold(candidate.id):
            return None
        task = self.repo.create(
            project=self._route(candidate),
            title=candidate.title,
            source_message_ids=list(candidate.message_ids),
            candidate_id=candidate.id,
        )
        self.candidates.attach_task(candidate.id, task.id)
        self.driver.hand_off(task.id)
        return task.id
```

Add `AmendmentProposal` and `TaskRow` to the module's imports.

- [ ] **Step 4: Add `return_to_triage`**

In `backend/ley_khaa/persistence/candidate_repository.py`:

```python
    def return_to_triage(self, candidate_id: str) -> bool:
        """Undo a claim whose fold then lost the race on the target task.

        PROMOTED is terminal for a candidate, so this is written as a direct
        state write rather than through ensure_transition: the candidate never
        actually became a task, and leaving it PROMOTED would hide a request
        nobody ever ran.
        """
        result = self.session.execute(
            update(CandidateRow)
            .where(
                CandidateRow.id == candidate_id,
                CandidateRow.state == CandidateState.PROMOTED.value,
                CandidateRow.task_id.is_(None),
            )
            .values(
                state=CandidateState.AWAITING_TRIAGE.value,
                updated_at=datetime.now(timezone.utc),
            )
        )
        self.session.commit()
        return result.rowcount == 1
```

The `task_id IS NULL` guard is what keeps this from resurrecting a candidate that genuinely did
become a task.

- [ ] **Step 5: Run, then prove they fail for the right reason**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_amendment_flow.py -q`
Expected: 7 passed.

- Make `_handle_amendment` always fold → `test_suggest_parks_the_amendment_for_a_human` and
  `test_an_executing_target_parks_even_on_auto` must both fail.
- Make it always park → `test_auto_folds_the_amendment_without_creating_a_second_task` must fail.
- Delete the `return_to_triage` call in `_fold` →
  `test_a_fold_that_loses_the_race_returns_the_candidate_to_triage` must fail on the candidate's
  state, proving the recovery path is real and not incidental.

- [ ] **Step 6: Whole suite, then commit**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 599 passed, 0 skipped, 0 warnings.

```bash
git add backend/ley_khaa/orchestrator/orchestrator.py \
        backend/ley_khaa/persistence/candidate_repository.py \
        backend/tests/test_amendment_flow.py
git commit -m "feat(orchestrator): fold or park amendments at promotion"
```

---

### Task 13: Projects and triage API

**Files:**
- Modify: `backend/ley_khaa/api/app.py`, `backend/ley_khaa/api/schemas.py`
- Test: `backend/tests/test_projects_api.py`

**Interfaces:**
- Produces: `GET /projects`, `POST /projects`, `GET /projects/{name}/queue`, `GET /triage`,
  `POST /candidates/{id}/fold`, `POST /candidates/{id}/separate`; schemas `ProjectIn`,
  `ProjectOut`, `TriageOut`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_projects_api.py
from ley_khaa.crystallizer.candidate import CandidateState
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.project_repository import ProjectRepository
from ley_khaa.persistence.repository import TaskRepository
from ley_khaa.projects.seeds import ensure_default_project


def test_projects_lists_queue_depth(client, session):
    ensure_default_project(session)
    ProjectRepository(session).create("acme", description="Acme's books")
    repo = TaskRepository(session)
    row = repo.create(project="acme", title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)

    body = client.get("/projects").json()
    acme = next(p for p in body if p["name"] == "acme")
    assert acme["queue_depth"] == 1
    assert acme["in_flight"] is None


def test_projects_shows_which_task_is_leased(client, session):
    ensure_default_project(session)
    ProjectRepository(session).create("acme", description="Acme's books")
    repo = TaskRepository(session)
    row = repo.create(project="acme", title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim_lease(row.id, owner="w1", ttl_seconds=60)

    acme = next(p for p in client.get("/projects").json() if p["name"] == "acme")
    assert acme["in_flight"] == row.id
    assert acme["queue_depth"] == 0, "a leased task is being worked, not queued"


def test_creating_a_project_without_a_description_is_refused(client, session):
    ensure_default_project(session)
    response = client.post("/projects", json={"name": "acme", "description": "  "})
    assert response.status_code == 422
    assert "description" in response.json()["detail"].lower()


def test_creating_a_duplicate_project_is_a_conflict(client, session):
    ensure_default_project(session)
    body = {"name": "acme", "description": "Acme's books"}
    assert client.post("/projects", json=body).status_code == 201
    assert client.post("/projects", json=body).status_code == 409


def test_the_project_queue_is_in_fifo_order(client, session):
    ensure_default_project(session)
    ProjectRepository(session).create("acme", description="Acme's books")
    repo = TaskRepository(session)
    first = repo.create(project="acme", title="first", source_message_ids=[])
    second = repo.create(project="acme", title="second", source_message_ids=[])

    ids = [t["id"] for t in client.get("/projects/acme/queue").json()]
    assert ids == [first.id, second.id]


def _parked(session):
    from ley_khaa.persistence.candidate_repository import CandidateRepository

    repo = TaskRepository(session)
    target = repo.create(project="default", title="universe check", source_message_ids=["m1"])
    candidates = CandidateRepository(session)
    candidate = candidates.upsert(
        conversation_id="C1",
        candidate_key="k",
        title="also flag duplicates",
        summary="s",
        state=CandidateState.READY,
        message_ids=["m2"],
        missing_fields=[],
        open_question=None,
    )
    candidates.claim_for_triage(
        candidate.id, task_id=target.id, reason="also flag dupes", confidence=0.9
    )
    return candidate, target


def test_triage_lists_parked_proposals_with_their_reason(client, session):
    candidate, target = _parked(session)
    body = client.get("/triage").json()
    assert len(body) == 1
    assert body[0]["candidate_id"] == candidate.id
    assert body[0]["amends_task_id"] == target.id
    assert body[0]["amends_task_title"] == "universe check"
    assert "also flag dupes" in body[0]["reason"]


def test_folding_from_the_api_merges_the_messages(client, session):
    candidate, target = _parked(session)
    TaskRepository(session).claim(
        target.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED
    )
    response = client.post(f"/candidates/{candidate.id}/fold")
    assert response.status_code == 200
    assert response.json()["id"] == target.id
    assert "m2" in TaskRepository(session).get(target.id).source_message_ids


def test_separating_from_the_api_creates_its_own_task(client, session):
    ensure_default_project(session)
    candidate, target = _parked(session)
    response = client.post(f"/candidates/{candidate.id}/separate")
    assert response.status_code == 200
    assert response.json()["id"] != target.id


def test_posting_a_message_reports_where_the_work_went(client, session):
    """Spec §4: /messages no longer returns a task that has finished, so it has
    to say what it DID do — which project the work landed in, and whether it was
    queued rather than run."""
    ensure_default_project(session)
    body = client.post(
        "/messages",
        json={
            "text": "compare the bloomberg universe against the factset universe, csv",
            "conversation_id": "C1",
        },
    ).json()
    assert "project" in body
    assert "queued" in body
    assert body["project"] == "default"


def test_folding_a_candidate_that_is_not_parked_is_a_conflict(client, session):
    from ley_khaa.persistence.candidate_repository import CandidateRepository

    candidate = CandidateRepository(session).upsert(
        conversation_id="C1",
        candidate_key="k",
        title="t",
        summary="s",
        state=CandidateState.READY,
        message_ids=[],
        missing_fields=[],
        open_question=None,
    )
    assert client.post(f"/candidates/{candidate.id}/fold").status_code == 409
```

- [ ] **Step 2: Run and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_projects_api.py -q`
Expected: FAIL — 404s, because none of the routes exist.

- [ ] **Step 3: Add the schemas**

In `backend/ley_khaa/api/schemas.py`:

```python
class ProjectIn(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""


class ProjectOut(BaseModel):
    name: str
    display_name: str
    description: str
    active: bool
    # Runnable tasks waiting for a worker. A leased task is being worked on and
    # is reported in in_flight instead, so the two never double-count.
    queue_depth: int
    in_flight: str | None


class TriageOut(BaseModel):
    candidate_id: str
    title: str
    summary: str
    amends_task_id: str
    amends_task_title: str
    reason: str
    confidence: float
```

- [ ] **Step 4: Add the routes**

In `backend/ley_khaa/api/app.py`:

```python
@app.get("/projects", response_model=list[ProjectOut])
def list_projects(session: Session = Depends(get_session)) -> list[ProjectOut]:
    projects = ProjectRepository(session)
    repo = TaskRepository(session)
    now = datetime.now(timezone.utc)
    out: list[ProjectOut] = []
    for project in projects.active():
        runnable = [
            t for t in repo.list() if t.project == project.name
        ]
        leased = next(
            (
                t.id
                for t in runnable
                if t.lease_owner is not None
                and t.lease_expires_at is not None
                and t.lease_expires_at > now
            ),
            None,
        )
        queued = repo.runnable_count(project.name, now=now)
        out.append(
            ProjectOut(
                name=project.name,
                display_name=project.display_name,
                description=project.description,
                active=project.active,
                queue_depth=queued,
                in_flight=leased,
            )
        )
    return out


@app.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectIn, session: Session = Depends(get_session)) -> ProjectOut:
    if not body.description.strip():
        # A project with no description is invisible to stage-2 routing, so
        # creating one silently would produce a project nothing can ever route
        # into. Refuse it here rather than let it look like it works.
        raise HTTPException(
            status_code=422,
            detail="a project needs a description — it is what routing reasons over",
        )
    projects = ProjectRepository(session)
    if projects.get(body.name) is not None:
        raise HTTPException(status_code=409, detail=f"project {body.name!r} already exists")
    row = projects.create(
        body.name, display_name=body.display_name, description=body.description
    )
    return ProjectOut(
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        active=row.active,
        queue_depth=0,
        in_flight=None,
    )


@app.get("/projects/{name}/queue", response_model=list[TaskOut])
def project_queue(name: str, session: Session = Depends(get_session)) -> list:
    return [t for t in TaskRepository(session).list() if t.project == name]


@app.get("/triage", response_model=list[TriageOut])
def list_triage(session: Session = Depends(get_session)) -> list[TriageOut]:
    candidates = CandidateRepository(session)
    repo = TaskRepository(session)
    out: list[TriageOut] = []
    for row in candidates.list_by_state(CandidateState.AWAITING_TRIAGE):
        target = repo.get(row.amends_task_id or "")
        out.append(
            TriageOut(
                candidate_id=row.id,
                title=row.title,
                summary=row.summary,
                amends_task_id=row.amends_task_id or "",
                # A target that vanished is still worth showing: the human needs
                # to see the parked request, not a 500.
                amends_task_title=target.title if target else "(task not found)",
                reason=row.amendment_reason or "",
                confidence=row.amendment_confidence or 0.0,
            )
        )
    return out


@app.post("/candidates/{candidate_id}/fold", response_model=TaskOut)
def fold_candidate(candidate_id: str, session: Session = Depends(get_session)):
    task_id = build_orchestrator(session).fold(candidate_id)
    if task_id is None:
        raise HTTPException(
            status_code=409,
            detail="this candidate is not waiting on an amendment decision, "
                   "or the task it amends has moved on",
        )
    return TaskRepository(session).get(task_id)


@app.post("/candidates/{candidate_id}/separate", response_model=TaskOut)
def separate_candidate(candidate_id: str, session: Session = Depends(get_session)):
    task_id = build_orchestrator(session).separate(candidate_id)
    if task_id is None:
        raise HTTPException(
            status_code=409, detail="this candidate is not waiting on an amendment decision"
        )
    return TaskRepository(session).get(task_id)
```

Add the imports this needs: `datetime`, `timezone`, `HTTPException`, `CandidateState`,
`ProjectRepository`, and the three new schemas.

- [ ] **Step 5: Report the routing decision from `/messages`**

Intake no longer returns a finished task, so it must say what it did instead. In
`backend/ley_khaa/api/schemas.py`, extend `IntakeOut`:

```python
class IntakeOut(BaseModel):
    message_id: str
    conversation_id: str
    candidate_ids: list[str]
    task_ids: list[str]
    # Where the work went, and whether it is waiting for a worker rather than
    # already done. Before 0.6.0 the caller could infer both from the task it got
    # back; in workers mode there is nothing finished to infer from.
    project: str | None = None
    queued: bool = False
```

Add `project` to `IntakeResult` in `orchestrator.py` (set it in `_promote` from the routing
decision), and populate both fields in `post_message`:

```python
    result = build_orchestrator(session).ingest(body.model_dump())
    return IntakeOut(
        message_id=result.message_id,
        conversation_id=result.conversation_id,
        candidate_ids=[c.id for c in result.candidates],
        task_ids=result.task_ids,
        project=result.project,
        queued=settings.dispatch_mode == "workers" and bool(result.task_ids),
    )
```

- [ ] **Step 6: Add `runnable_count`**

In `backend/ley_khaa/persistence/repository.py`:

```python
    def runnable_count(self, project: str, now: datetime | None = None) -> int:
        """How many tasks in this project are waiting for a worker.

        A leased task is excluded: it is being worked on, and the dashboard
        reports it as in-flight instead. Counting it in both places would make
        one task look like two.
        """
        moment = now or datetime.now(timezone.utc)
        return len(
            list(
                self.session.scalars(
                    select(TaskRow.id).where(
                        TaskRow.project == project, *self._runnable_where(moment)
                    )
                )
            )
        )
```

- [ ] **Step 7: Run, then prove they fail for the right reason**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_projects_api.py -q`
Expected: 11 passed.

- Delete the `description.strip()` check → `test_creating_a_project_without_a_description_is_refused`
  must fail with a 201.
- Delete the `projects.get(...) is not None` check → the duplicate test must fail (it would return
  201 and the existing row, because `create` is idempotent — which is exactly why the endpoint needs
  its own conflict check rather than relying on the repository).
- Make `runnable_count` ignore the lease → `test_projects_shows_which_task_is_leased` must fail on
  `queue_depth == 0`.

- [ ] **Step 8: Whole suite, then commit**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 610 passed, 0 skipped, 0 warnings.

```bash
git add backend/ley_khaa/api/app.py backend/ley_khaa/api/schemas.py \
        backend/ley_khaa/orchestrator/orchestrator.py \
        backend/ley_khaa/persistence/repository.py backend/tests/test_projects_api.py
git commit -m "feat(api): expose project queues and amendment triage"
```

---

### Task 14: Dashboard — projects view and triage tray

Follow `frontend/src/Registry.tsx` for structure and Tailwind idiom. **`npx tsc --noEmit` is part
of this task's definition of done** — `vite build` is transpile-only and will not catch a type
error.

**Files:**
- Create: `frontend/src/Projects.tsx`, `frontend/src/Projects.test.tsx`,
  `frontend/src/Triage.tsx`, `frontend/src/Triage.test.tsx`
- Modify: `frontend/src/api.ts`, `frontend/src/App.tsx`

**Interfaces:**
- Produces: `fetchProjects()`, `fetchTriage()`, `foldCandidate(id)`, `separateCandidate(id)`;
  types `Project`, `TriageItem`.

- [ ] **Step 1: Write the failing tests**

```tsx
// frontend/src/Projects.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import Projects from "./Projects";
import * as api from "./api";

beforeEach(() => vi.restoreAllMocks());

test("it shows each project's queue depth", async () => {
  vi.spyOn(api, "fetchProjects").mockResolvedValue([
    { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 2, in_flight: null },
  ]);
  render(<Projects />);
  expect(await screen.findByText("Acme")).toBeTruthy();
  expect(screen.getByText(/2 queued/)).toBeTruthy();
});

test("it names the task currently in flight", async () => {
  vi.spyOn(api, "fetchProjects").mockResolvedValue([
    { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 0, in_flight: "task-7" },
  ]);
  render(<Projects />);
  await waitFor(() => expect(screen.getByText(/running/i)).toBeTruthy());
});

test("it says so when there is nothing queued", async () => {
  vi.spyOn(api, "fetchProjects").mockResolvedValue([
    { name: "acme", display_name: "Acme", description: "d", active: true, queue_depth: 0, in_flight: null },
  ]);
  render(<Projects />);
  expect(await screen.findByText(/idle/i)).toBeTruthy();
});
```

```tsx
// frontend/src/Triage.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import Triage from "./Triage";
import * as api from "./api";

const ITEM = {
  candidate_id: "c1",
  title: "also flag duplicates",
  summary: "s",
  amends_task_id: "t1",
  amends_task_title: "universe check",
  reason: "adds a check to the running task",
  confidence: 0.92,
};

beforeEach(() => vi.restoreAllMocks());

test("it shows the proposal and why it was made", async () => {
  vi.spyOn(api, "fetchTriage").mockResolvedValue([ITEM]);
  render(<Triage />);
  expect(await screen.findByText(/also flag duplicates/)).toBeTruthy();
  expect(screen.getByText(/universe check/)).toBeTruthy();
  expect(screen.getByText(/adds a check to the running task/)).toBeTruthy();
});

test("folding calls the API and refreshes", async () => {
  const fetchTriage = vi.spyOn(api, "fetchTriage").mockResolvedValue([ITEM]);
  const fold = vi.spyOn(api, "foldCandidate").mockResolvedValue(undefined);
  render(<Triage />);
  fireEvent.click(await screen.findByRole("button", { name: /fold in/i }));
  await waitFor(() => expect(fold).toHaveBeenCalledWith("c1"));
  // Two loads: the initial one and the refresh after the mutation. Without the
  // refresh the tray keeps showing a decision that has already been made.
  await waitFor(() => expect(fetchTriage).toHaveBeenCalledTimes(2));
});

test("separating calls the API", async () => {
  vi.spyOn(api, "fetchTriage").mockResolvedValue([ITEM]);
  const separate = vi.spyOn(api, "separateCandidate").mockResolvedValue(undefined);
  render(<Triage />);
  fireEvent.click(await screen.findByRole("button", { name: /keep separate/i }));
  await waitFor(() => expect(separate).toHaveBeenCalledWith("c1"));
});

test("a failed fold shows the reason instead of silently doing nothing", async () => {
  vi.spyOn(api, "fetchTriage").mockResolvedValue([ITEM]);
  vi.spyOn(api, "foldCandidate").mockRejectedValue(new Error("409: the task has moved on"));
  render(<Triage />);
  fireEvent.click(await screen.findByRole("button", { name: /fold in/i }));
  expect(await screen.findByText(/moved on/)).toBeTruthy();
});
```

- [ ] **Step 2: Run and watch them fail**

Run: `cd frontend && npm test -- Projects Triage`
Expected: FAIL — the modules do not exist.

- [ ] **Step 3: Add the API client functions**

In `frontend/src/api.ts`:

```ts
export type Project = {
  name: string;
  display_name: string;
  description: string;
  active: boolean;
  queue_depth: number;
  in_flight: string | null;
};

export type TriageItem = {
  candidate_id: string;
  title: string;
  summary: string;
  amends_task_id: string;
  amends_task_title: string;
  reason: string;
  confidence: number;
};

export async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(`${BASE}/projects`);
  if (!res.ok) throw new Error(`fetchProjects failed: ${res.status}`);
  return res.json();
}

export async function fetchTriage(): Promise<TriageItem[]> {
  const res = await fetch(`${BASE}/triage`);
  if (!res.ok) throw new Error(`fetchTriage failed: ${res.status}`);
  return res.json();
}

// These deliberately do NOT reuse send(): it discards the response body, and a
// 409 here carries the only explanation the human gets for why a fold failed.
async function mutateCandidate(id: string, action: string): Promise<void> {
  const res = await fetch(`${BASE}/candidates/${id}/${action}`, { method: "POST" });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `${action} failed: ${res.status}`);
  }
}

export const foldCandidate = (id: string) => mutateCandidate(id, "fold");
export const separateCandidate = (id: string) => mutateCandidate(id, "separate");
```

- [ ] **Step 4: Write the two components**

```tsx
// frontend/src/Projects.tsx
import { useCallback, useEffect, useState } from "react";
import { fetchProjects, type Project } from "./api";

export default function Projects() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => fetchProjects().then(setProjects).catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, [load]);

  if (error) return <p className="text-red-600 text-sm">{error}</p>;

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {projects.map((p) => (
        <div key={p.name} className="rounded border border-gray-200 p-3">
          <div className="flex items-baseline justify-between">
            <span className="font-medium">{p.display_name || p.name}</span>
            <span className="text-xs text-gray-500">{p.name}</span>
          </div>
          <p className="mt-1 text-sm text-gray-600">
            {p.in_flight
              ? `running ${p.in_flight.slice(0, 8)}…`
              : p.queue_depth === 0
                ? "idle"
                : "waiting"}
            {p.queue_depth > 0 ? ` · ${p.queue_depth} queued` : ""}
          </p>
        </div>
      ))}
    </div>
  );
}
```

```tsx
// frontend/src/Triage.tsx
import { useCallback, useEffect, useState } from "react";
import { fetchTriage, foldCandidate, separateCandidate, type TriageItem } from "./api";

export default function Triage() {
  const [items, setItems] = useState<TriageItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => fetchTriage().then(setItems).catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    load();
  }, [load]);

  // The tray must disappear when it is empty rather than sit there as a
  // permanent empty heading — most of the time there is nothing to triage.
  if (items.length === 0 && !error) return null;

  const act = (action: (id: string) => Promise<void>, id: string) =>
    action(id)
      .then(() => {
        setError(null);
        return load();
      })
      .catch((e) => setError(String(e)));

  return (
    <div>
      {error && <p className="text-red-600 text-sm mb-2">{error}</p>}
      <ul className="space-y-2">
        {items.map((item) => (
          <li key={item.candidate_id} className="rounded border border-amber-300 bg-amber-50 p-3">
            <p className="font-medium">{item.title}</p>
            <p className="mt-1 text-sm text-gray-700">
              Looks like an amendment to <span className="font-medium">{item.amends_task_title}</span>{" "}
              ({Math.round(item.confidence * 100)}% sure) — {item.reason}
            </p>
            <div className="mt-2 flex gap-2">
              <button
                className="rounded bg-amber-600 px-2 py-1 text-sm text-white"
                onClick={() => act(foldCandidate, item.candidate_id)}
              >
                Fold in
              </button>
              <button
                className="rounded border border-gray-300 px-2 py-1 text-sm"
                onClick={() => act(separateCandidate, item.candidate_id)}
              >
                Keep separate
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

- [ ] **Step 5: Mount both in `App.tsx`**

Import them and add above "Forming":

```tsx
      <h2 className="text-lg font-semibold mb-2">Projects</h2>
      <Projects />

      <h2 className="text-lg font-semibold mb-2 mt-8">Needs a decision</h2>
      <Triage />
```

- [ ] **Step 6: Run the frontend checks**

Run: `cd frontend && npm test && npx tsc --noEmit -p tsconfig.json`
Expected: 44 tests passing (37 baseline + 7 new), typecheck clean.

- [ ] **Step 7: Prove the tests fail for the right reason**

- Delete the `return load()` after a successful fold in `Triage.act` → "folding calls the API and
  refreshes" must fail on the call count of 2. This is the assertion that catches backlog item 8's
  first entry (a list that does not refresh after a mutation) in the new code.
- Delete the `.catch` in `act` → "a failed fold shows the reason" must fail.
- Make `mutateCandidate` throw only the status → the same test must fail on `/moved on/`, proving
  the detail is genuinely surfaced rather than swallowed.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/Projects.tsx frontend/src/Projects.test.tsx frontend/src/Triage.tsx \
        frontend/src/Triage.test.tsx frontend/src/api.ts frontend/src/App.tsx
git commit -m "feat(frontend): add the projects view and the amendment triage tray"
```

---

### Task 15: Backlog items 5 and 6 — atomic counters and write ordering

Both were cosmetic races on a threadpool before this phase. With a dispatcher running projects in
parallel they are reachable, which is why they ship here rather than staying on the backlog.

**Files:**
- Modify: `backend/ley_khaa/persistence/workflow_repository.py`,
  `backend/ley_khaa/orchestrator/driver.py`
- Test: `backend/tests/test_concurrent_counters.py`, `backend/tests/test_write_ordering.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_concurrent_counters.py
"""Backlog item 5: read-modify-write counters lose increments under concurrency."""
import threading

from ley_khaa.persistence.workflow_repository import WorkflowRepository


def test_concurrent_successes_do_not_lose_an_increment(session_factory, seed_workflow):
    """Ten threads, ten increments. A read-modify-write loses some of them; an
    atomic UPDATE does not."""
    name = seed_workflow
    barrier = threading.Barrier(10, timeout=5)

    def bump():
        local = session_factory()
        try:
            barrier.wait()
            WorkflowRepository(local).record_success(name)
        finally:
            local.close()

    threads = [threading.Thread(target=bump) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    session = session_factory()
    try:
        assert WorkflowRepository(session).get(name).runs_ok == 10
    finally:
        session.close()


def test_a_learned_alias_is_not_lost_to_a_concurrent_success(session_factory, seed_workflow):
    name = seed_workflow
    barrier = threading.Barrier(2, timeout=5)

    def bump(alias):
        local = session_factory()
        try:
            barrier.wait()
            WorkflowRepository(local).record_success(name, learned_alias=alias)
        finally:
            local.close()

    threads = [
        threading.Thread(target=bump, args=("compare the books",)),
        threading.Thread(target=bump, args=("reconcile the books",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    session = session_factory()
    try:
        aliases = WorkflowRepository(session).get(name).operation_aliases
        assert "compare the books" in aliases
        assert "reconcile the books" in aliases
    finally:
        session.close()
```

This needs a `session_factory` fixture (a real file-backed SQLite database, since the in-memory
`StaticPool` session in `conftest` is one connection shared by every thread and cannot show a lost
update) and a `seed_workflow` fixture. Add both to `backend/tests/conftest.py`:

```python
@pytest.fixture
def session_factory(tmp_path):
    """Independent sessions over one real database file.

    The `session` fixture is a single in-memory connection shared by every
    caller, which cannot exhibit a lost update — a concurrency test written
    against it would pass no matter what the code does.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    return factory


@pytest.fixture
def seed_workflow(session_factory):
    from ley_khaa.registry.seeds import ensure_seed_workflows

    session = session_factory()
    try:
        ensure_seed_workflows(session)
        return "set_difference"
    finally:
        session.close()
```

```python
# backend/tests/test_write_ordering.py
"""Backlog item 6: two writes landed before the state claim that authorises them."""
from ley_khaa.domain.states import TaskState
from ley_khaa.persistence.repository import TaskRepository


def test_a_task_that_loses_the_interpretation_race_keeps_no_spec(session, monkeypatch):
    """A task whose CLASSIFIED -> INTERPRETED claim loses must not be left
    carrying the spec (or the memory attribution) for a path it did not take."""
    from ley_khaa.orchestrator.driver import TaskDriver
    from ley_khaa.llm.heuristic import HeuristicLLM
    from ley_khaa.persistence.candidate_repository import CandidateRepository
    from ley_khaa.persistence.message_repository import MessageRepository

    repo = TaskRepository(session)
    row = repo.create(project="default", title="t", source_message_ids=[])
    repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)

    driver = TaskDriver(
        repo,
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
    )

    # Simulate another worker winning the claim first: every claim from here on
    # loses, which is exactly what the real race looks like from this side.
    monkeypatch.setattr(TaskRepository, "claim", lambda *a, **k: False)

    driver._after_spec(repo.get(row.id), _spec())

    assert repo.get(row.id).spec is None, "a lost claim must leave no spec behind"


def _spec():
    from ley_khaa.interpreter.spec import TaskSpec

    return TaskSpec(
        intent="compare",
        inputs=["a", "b"],
        operation="set_difference",
        output_format="csv",
        recipient=None,
        urgency="normal",
        missing_fields=[],
        source_message_ids=[],
        certainty=0.9,
    )
```

- [ ] **Step 2: Run and watch them fail**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_concurrent_counters.py tests/test_write_ordering.py -q`
Expected: FAIL — the counter test with `runs_ok` below 10, and the ordering test with a spec that
was saved despite the lost claim. **If the counter test passes before the fix, it is not
reproducing the race** — check that `session_factory` really hands out independent sessions over a
file-backed database.

- [ ] **Step 3: Make the counters atomic**

In `backend/ley_khaa/persistence/workflow_repository.py`:

```python
    def record_success(self, name: str, *, learned_alias: str | None = None) -> WorkflowRow:
        """Count a successful run, and learn the phrasing that found it.

        The counter is an atomic UPDATE rather than a read-modify-write: the
        dispatcher runs projects in parallel, so two cached runs of the same
        workflow can interleave and a Python-side increment loses one of them.

        The alias list cannot be incremented, so it is a compare-and-swap on the
        value we read, retried once. A lost retry costs one extra Haiku call the
        next time that phrasing appears; it never corrupts the list.
        """
        self.session.execute(
            update(WorkflowRow)
            .where(WorkflowRow.name == name)
            .values(runs_ok=WorkflowRow.runs_ok + 1, last_used_at=_now())
        )
        self.session.commit()

        if learned_alias:
            for _ in range(2):
                row = self._row(name)
                current = list(row.operation_aliases or [])
                if learned_alias in current:
                    break
                result = self.session.execute(
                    update(WorkflowRow)
                    .where(
                        WorkflowRow.name == name,
                        WorkflowRow.operation_aliases == current,
                    )
                    .values(operation_aliases=current + [learned_alias])
                )
                self.session.commit()
                if result.rowcount == 1:
                    break
                self.session.expire_all()
        self.session.expire_all()
        return self._row(name)

    def record_failure(self, name: str) -> WorkflowRow:
        self.session.execute(
            update(WorkflowRow)
            .where(WorkflowRow.name == name)
            .values(runs_failed=WorkflowRow.runs_failed + 1, quarantined=True)
        )
        self.session.commit()
        self.session.expire_all()
        return self._row(name)
```

Add `update` to the module's `sqlalchemy` import.

**If the compare-and-swap on a JSON column does not work on SQLite** (a JSON equality comparison in
a WHERE clause is dialect-dependent), fall back to comparing a `json.dumps(current, sort_keys=True)`
of the column via `sa.func.json(...)`, or serialise alias learning behind a short retry on
`IntegrityError`. Do not silently drop the guard — the second test exists to catch that.

- [ ] **Step 4: Claim before writing in the driver**

In `backend/ley_khaa/orchestrator/driver.py`, restructure `_after_spec` so the claim comes first:

```python
    def _after_spec(self, row: TaskRow, spec: TaskSpec) -> bool:
        """Everything that happens once a spec exists, however it was obtained.

        The claim comes FIRST. Writing the spec before winning the transition
        left a task that lost the race carrying a spec for a path it never took
        — the same inversion c043c46 fixed in reject(), and backlog item 6.
        _remember already gets this right and is the model followed here.
        """
        asking = bool(spec.missing_fields) and (
            (row.clarification_rounds or 0) < _MAX_CLARIFICATION_ROUNDS
        )
        target = TaskState.NEEDS_CLARIFICATION if asking else TaskState.INTERPRETED
        if not self.repo.claim(row.id, expected=TaskState.CLASSIFIED, target=target):
            return False

        self.repo.save_spec(row.id, spec)
        self.repo.set_open_question(
            row.id, _question_for(spec.missing_fields) if asking else None
        )
        return True
```

And in `_interpret`, move `save_memory_hit` so it runs only after `_after_spec` reports a won claim:

```python
        if remembered is not None:
            spec = TaskSpec.model_validate(remembered.spec).model_copy(
                update={"source_message_ids": list(row.source_message_ids or [])}
            )
            won = self._after_spec(row, spec)
            if won:
                # Same ordering rule: attribution belongs to the caller that
                # actually took the task down the remembered path.
                self.repo.save_memory_hit(
                    row.id,
                    source_task_id=remembered.source_task_id,
                    familiarity=remembered.times_seen,
                )
            return won
```

- [ ] **Step 5: Run, then prove they fail for the right reason**

Run: `cd backend && TMPDIR="$HOME/tmp" python -m pytest tests/test_concurrent_counters.py tests/test_write_ordering.py -q`
Expected: 3 passed.

- Restore `row.runs_ok += 1` → the ten-thread test must fail with a count below 10. If it still
  passes, the test is not reproducing the race and must be fixed before this task is accepted.
- Move `save_spec` back above the claim → `test_a_task_that_loses_the_interpretation_race...`
  must fail.

- [ ] **Step 6: Whole suite, then commit**

Run: `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q`
Expected: 613 passed, 0 skipped, 0 warnings.

```bash
git add backend/ley_khaa/persistence/workflow_repository.py \
        backend/ley_khaa/orchestrator/driver.py backend/tests/conftest.py \
        backend/tests/test_concurrent_counters.py backend/tests/test_write_ordering.py
git commit -m "fix(persistence): make workflow counters atomic and claim before writing"
```

---

### Task 16: Documentation, and a pass for statements this phase made false

The phase's last act, and the one with the worst failure mode: a false line in a spec is the
binding authority every later plan argues from. Four separate statements were corrected this way in
Phase 4; this task looks for the ones Phase 5 has just created.

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `docs/superpowers/specs/2026-08-28-phase-5-backlog.md`

- [ ] **Step 1: Hunt the statements this phase falsified**

Run each of these and read every hit, rather than trusting the list:

```bash
grep -rn "project=\"default\"\|project='default'" --include=*.py --include=*.md .
grep -rn "every task is\|shared across every client\|until §5.4\|until 5.4" --include=*.py --include=*.md .
grep -rn "inline\|blocks\|synchronous" README.md
grep -rn "advance_stalled\|EXECUTING is deliberately" --include=*.py .
```

Known hits to fix, each with what is now true:

1. `Orchestrator.advance_stalled`'s docstring says a task in `EXECUTING` "stays in EXECUTING and
   stays visible, rather than being silently re-run at cost; recovering it automatically needs a
   lease on the row, not a timer." **The lease now exists.** Rewrite it to say that recovery is the
   dispatcher's job, under a lease, and that `advance_stalled` remains lease-free and therefore
   inline-mode only.
2. Backlog item 2 says `RECALL_CANDIDATE_LIMIT` is load-bearing because memory grows per phrasing.
   Still true — item 1 was not done. **Do not "tidy" this line.**
3. Backlog items 4, 5 and 6 are now done. Mark them so in the backlog file, with the commit that
   closed each, rather than deleting them — a deferred finding nobody records is a silent discard,
   and so is a closed one nobody can trace.

- [ ] **Step 2: Update the README**

Add a **Projects and queues** section covering: what a project is, how routing decides one (two
stages, and that a stage-2 match writes a binding), `LEY_KHAA_DISPATCH=inline|workers` and when to
use each, and the lease settings. Say plainly that inline is the right mode for a single-operator
run and is what the test suite uses — it is a supported mode, not a test shim.

Add the `TMPDIR` gotcha to the dev-loop section if it is still missing:

```markdown
Docker-parametrized tests need a temp directory Colima can see:
`mkdir -p "$HOME/tmp" && TMPDIR="$HOME/tmp" python -m pytest -q`. The directory must exist first —
otherwise pytest silently falls back to `/private/tmp` and the sandbox tests fail with a misleading
"can't open file".
```

- [ ] **Step 3: Write the CHANGELOG entry**

Under `## [Unreleased]`, an `### Added` / `### Changed` / `### Fixed` set covering routing, the
dispatcher, amendments, and backlog items 5 and 6. **Claim only what a test proves.** Specifically:
say that tasks in different projects run concurrently (proven by `test_concurrency.py`'s barrier),
and do **not** claim memory now isolates clients in general — say that routing assigns projects per
client, so memory's project scoping is now a real boundary wherever bindings exist, and that
everything unrouted still shares `default`.

- [ ] **Step 4: Verify the whole thing**

```bash
mkdir -p "$HOME/tmp"
cd backend && TMPDIR="$HOME/tmp" python -m pytest -q
cd ../frontend && npm test && npx tsc --noEmit -p tsconfig.json
```

Expected: 613 backend passed / 0 skipped / 0 warnings; 44 frontend; typecheck clean.

- [ ] **Step 5: Commit**

```bash
git add README.md CHANGELOG.md docs/superpowers/specs/2026-08-28-phase-5-backlog.md \
        backend/ley_khaa/orchestrator/orchestrator.py
git commit -m "docs: document routing, queues and amendments for 0.6.0"
```

---

## Definition of done for the phase

Every box above ticked, plus:

- `mkdir -p "$HOME/tmp" && cd backend && TMPDIR="$HOME/tmp" python -m pytest -q` → **613 passed, 0
  skipped, 0 warnings**, including all 9 `[docker]` contract params against a real image.
- `cd frontend && npm test && npx tsc --noEmit -p tsconfig.json` → 44 passed, typecheck clean.
- `docker compose up --build` from a clean tree: dashboard live, demo task seeded, **no
  `ANTHROPIC_API_KEY` set** — the offline rules for `ProjectChoice` and `AmendmentChoice` are what
  keep this true.
- A whole-branch review on Opus before the PR. Phases 3 and 4 both found defects at cross-module
  seams that no single task's diff could reveal — five and three respectively. Budget for it.

## Known limits, stated so nobody has to rediscover them

- **Queue reordering by urgency is not built** (spec §7). One worker per project, FIFO within it.
  Urgency lives in the `TaskSpec`, which is only known after the task has been dequeued.
- **Amendment detection is within a project only.** A follow-up that lands in a different project is
  a new task.
- **Memory still forks a row per phrasing** (backlog item 1). Untouched by this phase, and
  `RECALL_CANDIDATE_LIMIT = 50` stays load-bearing because of it.
- **A task's project is decided once, at promotion.** Nothing re-routes an existing task; moving one
  between projects has no API.
