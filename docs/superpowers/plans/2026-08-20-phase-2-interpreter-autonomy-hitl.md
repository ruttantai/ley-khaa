# Phase 2 — Interpreter + Autonomy Engine + Human-in-the-Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Break open the Phase 1 stub path so a promoted task is really interpreted into a validated `TaskSpec`, scored by a deterministic autonomy engine that recommends Suggest/Co-pilot/Auto, and then either parks for a human decision or — on Auto — runs straight through.

**Architecture:** `Orchestrator._promote()` stops walking `STUB_PATH` and hands the task to a new **`TaskDriver`**, a single re-entrant `advance(task_id)` that pushes a task as far as it can go unattended (`received → classified → interpret → score → gate`) and stops at the one place a human is needed. The four human actions (`approve`, `reject`, `override`, `edit_spec`) each perform one small transition and re-enter `advance()`, so the automatic path is defined in exactly one place and cannot drift between entry points. A clarification answer is **not** a special dashboard path: it is posted as a real `Message` carrying `reply_to_task_id`, which intake routes directly to the owning task, bypassing stage A and stage B so it can never spawn a duplicate candidate.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (typed `Mapped`), Alembic, Postgres 16 / SQLite for tests, `anthropic` Python SDK, pytest; React + TypeScript + Vite + Tailwind v4, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-18-ley-khaa-design.md` — this plan implements §5.5 (Interpreter), §5.7 (Autonomy engine, headline #2), §5.8 (Human-in-the-loop layer), and the §5.9 orchestrator states that Phase 1 declared but never entered. Design decisions specific to this phase were settled in the 2026-08-20 brainstorming session and are recorded under "Phase 2 Decisions" below.

## Global Constraints

- **Python** `>=3.12`; **Pydantic** `v2`; **SQLAlchemy** `2.0` typed `Mapped` style. DB access **only** through repositories.
- **Model IDs are exact strings, never date-suffixed:** `claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-5`. Writing `claude-haiku-4-5-20251001` or similar is a bug.
- **Structured output:** always `client.messages.parse(model=…, output_format=SomePydanticModel, …)` and read `response.parsed_output`. Do not hand-roll JSON parsing.
- **Thinking is model-gated.** Pass `thinking={"type": "adaptive"}` **only** for `claude-opus-5` / `claude-sonnet-5`. `claude-haiku-4-5` is a pre-4.6 model: pass **no** `thinking` and **no** `output_config.effort` — both error. The router carries this as a flag so call sites never guess.
- **Tests never make network calls.** Every LLM-touching test injects `FakeLLM`. `AnthropicLLM` must not be constructed anywhere under `backend/tests/`.
- **The offline path must stay runnable.** `docker compose up` on a fresh clone with **no `ANTHROPIC_API_KEY`** must still reach a task parked at `awaiting_approval`. Any new LLM stage needs a `HeuristicLLM` rule or the fresh-clone demo breaks.
- **Data is synthetic only.** No real employer data, credentials, or infrastructure — ever.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`). Commit after every task.
- **Versioning:** SemVer; this phase is released as tag **`0.3.0`**.
- **Package name:** backend Python package is `ley_khaa` (underscore); repo/product name is `ley-khaa`.
- **Orchestrator stays synchronous** this phase. Per-project async concurrency is Phase `0.5.0`.
- **The executor is still a stub.** `executing → validating → done` does no real work. The real executor is Phase `0.4.0`. Do not build it here.

## Phase 2 Decisions

These were settled in brainstorming and are not open for re-litigation during execution:

1. **A task parks at `awaiting_approval` by default, but Auto skips the gate.** When the effective autonomy mode is `auto`, the driver walks the task straight through the stub without stopping. This is how the dial demonstrably *changes behaviour* rather than just displaying a label.
2. **Autonomy scoring is deterministic Python**, not an LLM call. Its inputs are signals the LLM already produced (`spec.certainty`, `spec.missing_fields`, the candidate's readiness). Identical behaviour online and offline, one test per policy row, and the plain-English reason is templated from the rules that fired.
3. **A clarification answer re-enters as a `Message`**, taking the same route a Slack thread reply will take in Phase 4 — not a direct spec patch.
4. **All four HITL actions ship:** approve/reject, override the mode, answer a clarification, edit the spec inline.
5. **The amendment detector is out of scope**, deferred to Phase 4 where it belongs alongside project routing. This phase builds the message-routing mechanism it will later reuse.
6. **Suggest and Co-pilot are indistinguishable in 0.3.0.** Both park at the single gate that exists. They diverge in Phase 3 when the executor has mid-run checkpoints for Co-pilot to stop at. Ship this honestly; do not invent a fake checkpoint.
7. **Alembic lands in this phase.** Two consecutive releases telling users to drop their database is the signal that migration tooling is overdue.

## File Structure

| File | Responsibility |
|---|---|
| `backend/alembic.ini`, `backend/alembic/env.py` | migration tooling wired to `settings.database_url` |
| `backend/alembic/versions/0001_baseline.py` | the 0.2.0 schema as a baseline revision |
| `backend/alembic/versions/0002_autonomy.py` | phase-2 columns |
| `backend/ley_khaa/domain/states.py` | extend: transitions into and out of `needs_clarification` |
| `backend/ley_khaa/domain/models.py` | extend: `Message.reply_to_task_id` |
| `backend/ley_khaa/persistence/orm.py` | extend: `TaskRow` autonomy columns, `MessageRow.reply_to_task_id` |
| `backend/ley_khaa/persistence/repository.py` | extend: spec/recommendation writes, conditional `claim()` |
| `backend/ley_khaa/persistence/candidate_repository.py` | extend: `get()` by id |
| `backend/ley_khaa/interpreter/spec.py` | `TaskSpec` — the validated §5.5 output |
| `backend/ley_khaa/interpreter/interpreter.py` | candidate messages → `TaskSpec`, retry-once-then-escalate |
| `backend/ley_khaa/autonomy/modes.py` | `AutonomyMode` enum |
| `backend/ley_khaa/autonomy/engine.py` | deterministic confidence/risk policy → `Recommendation` |
| `backend/ley_khaa/llm/heuristic.py` | extend: offline `TaskSpec` rule |
| `backend/ley_khaa/orchestrator/driver.py` | `TaskDriver` — the automatic path and the four human actions |
| `backend/ley_khaa/orchestrator/orchestrator.py` | rewire: drop `STUB_PATH`, hand off to driver, route replies |
| `backend/ley_khaa/intake/gateway.py` | extend: accept `reply_to_task_id` |
| `backend/ley_khaa/api/app.py` | HITL endpoints, `InvalidTransition` → 409, sweeper advances tasks |
| `backend/ley_khaa/api/schemas.py` | extend: `TaskOut`, action request bodies |
| `backend/ley_khaa/fixtures/conversations/ambiguous_report_request.json` | golden conversation with a deliberate gap |
| `frontend/src/api.ts` | extend: `Task` type + the five action calls |
| `frontend/src/TaskDetail.tsx` | spec, dial, reason, and the four actions |
| `frontend/src/App.tsx` | rewire: task rows open the detail view |

---

### Task 1: Alembic migrations with a model-drift guard

**Files:**
- Modify: `backend/pyproject.toml`
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/alembic/script.py.mako`
- Create: `backend/alembic/versions/0001_baseline.py`
- Modify: `backend/ley_khaa/db.py`
- Modify: `backend/ley_khaa/api/app.py:70` (the `init_db()` call in `lifespan`)
- Test: `backend/tests/test_migrations.py`

**Interfaces:**
- Consumes: nothing (first task of the phase).
- Produces: `ley_khaa.db.run_migrations() -> None`, which every later schema change flows through. `Base.metadata` stays the source of truth for tests; migrations are checked against it by the drift test.

**Why the drift guard matters:** the moment migrations and models can disagree, they eventually will, and the failure shows up as a production-only crash. The test below runs the migrations onto a scratch database and asks Alembic to autogenerate a diff against `Base.metadata`. Any difference is a failing test, so a forgotten migration cannot merge.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_migrations.py
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from ley_khaa.db import Base
from ley_khaa.persistence import orm  # noqa: F401 — register models on Base

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _upgraded_engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'drift.db'}"
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return create_engine(url, future=True)


def test_migrations_create_the_0_2_0_tables(tmp_path):
    engine = _upgraded_engine(tmp_path)
    tables = set(inspect(engine).get_table_names())
    assert {"tasks", "messages", "task_candidates"} <= tables


def test_migrations_match_the_models(tmp_path):
    """A schema change with no migration is a failing test, not a prod crash."""
    engine = _upgraded_engine(tmp_path)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_migrations.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'alembic'`.

- [ ] **Step 3: Add the dependency**

In `backend/pyproject.toml`, add to `dependencies`:

```toml
    "alembic>=1.13",
```

Then `cd backend && pip install -e ".[dev]"`.

- [ ] **Step 4: Create `backend/alembic.ini`**

```ini
[alembic]
script_location = alembic
prepend_sys_path = .
version_path_separator = os
# No sqlalchemy.url here on purpose: env.py reads the app's own settings, so
# `alembic upgrade head` and the running app can never disagree about which
# database they mean. Tests override it via Config.set_main_option.
```

- [ ] **Step 5: Create `backend/alembic/env.py`**

```python
from alembic import context
from sqlalchemy import create_engine, pool

from ley_khaa.config import settings
from ley_khaa.db import Base
from ley_khaa.persistence import orm  # noqa: F401 — register models on Base

# No fileConfig() call: alembic.ini deliberately carries no logging sections,
# and the app configures its own logging.
config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url", None) or settings.database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER TABLE ADD CONSTRAINT; batch mode rewrites the
            # table instead. The no-Docker dev path runs on SQLite, so this is
            # not optional.
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 6: Create `backend/alembic/script.py.mako`**

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 7: Create the baseline revision**

This is the schema exactly as 0.2.0 shipped it. Write it by hand rather than autogenerating, so the baseline is reviewable.

```python
# backend/alembic/versions/0001_baseline.py
"""baseline: the 0.2.0 schema

Revision ID: 0001_baseline
Revises:
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("source_message_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("relevant", sa.Boolean(), nullable=True),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
    )
    op.create_index("ix_messages_external_id", "messages", ["external_id"], unique=True)
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_table(
        "task_candidates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("candidate_key", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("message_ids", sa.JSON(), nullable=False),
        sa.Column("missing_fields", sa.JSON(), nullable=False),
        sa.Column("open_question", sa.String(), nullable=True),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "conversation_id", "candidate_key", name="uq_candidate_per_conversation"
        ),
    )
    op.create_index("ix_task_candidates_conversation_id", "task_candidates", ["conversation_id"])
    op.create_index("ix_task_candidates_candidate_key", "task_candidates", ["candidate_key"])


def downgrade() -> None:
    op.drop_table("task_candidates")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_index("ix_messages_external_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("tasks")
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_migrations.py -v`
Expected: PASS, both tests. If `test_migrations_match_the_models` fails, the baseline does not match `orm.py` — fix the baseline, not the test.

- [ ] **Step 9: Route the app through migrations**

In `backend/ley_khaa/db.py`, replace `init_db()`:

```python
from pathlib import Path

# ... existing imports, Base, engine, SessionLocal unchanged ...

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def run_migrations(url: str | None = None) -> None:
    """Bring a database up to head. Defaults to the configured one.

    Replaces the old Base.metadata.create_all(): create_all silently ignores a
    table that already exists but lacks a newly added column, which is exactly
    how 0.2.0 ended up telling everyone to drop their database.

    `url` is a parameter rather than read-only config because Settings is a
    frozen dataclass — this is the seam the upgrade-path test needs.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from .persistence import orm  # noqa: F401 — register models on Base

    url = url or settings.database_url
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)

    # A database created by 0.2.0's create_all() already has the tables but no
    # alembic_version row, so `upgrade head` would try to create them again and
    # crash on first start. Stamp it at the baseline instead, then upgrade
    # normally — this is what lets 0.3.0 be the release that stops asking people
    # to drop their database.
    target = create_engine(url, future=True)
    try:
        tables = set(inspect(target).get_table_names())
    finally:
        target.dispose()
    if "tasks" in tables and "alembic_version" not in tables:
        command.stamp(config, "0001_baseline")

    command.upgrade(config, "head")
```

Add a test for that upgrade path to `backend/tests/test_migrations.py`:

```python
def test_a_pre_alembic_database_is_stamped_rather_than_recreated(tmp_path):
    """The 0.2.0 upgrade path: the tables already exist, alembic_version does not."""
    from ley_khaa.db import run_migrations

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    legacy = create_engine(url, future=True)
    Base.metadata.create_all(legacy)  # exactly what 0.2.0 did
    assert "alembic_version" not in set(inspect(legacy).get_table_names())

    run_migrations(url)  # must not raise "table tasks already exists"

    assert "alembic_version" in set(inspect(legacy).get_table_names())
    # And the phase-2 columns really did get applied on top.
    assert "spec" in {c["name"] for c in inspect(legacy).get_columns("tasks")}
```

Note this test only passes once `0002_autonomy` exists (Task 2). Write it now and expect it to assert on `spec` only after Task 2 lands — or add the `spec` assertion in Task 2 Step 15. Prefer the latter: keep Task 1's version of this test ending at the `alembic_version` assertion.

In `backend/ley_khaa/api/app.py`, change the import and the `lifespan` call:

```python
from ..db import SessionLocal, run_migrations
```

```python
    run_migrations()
```

- [ ] **Step 10: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: PASS, all tests, no new warnings. `conftest.py` builds its schema with `Base.metadata.create_all(test_engine)` and is unaffected — that stays, because per-test migration runs would be slow for no benefit now that the drift test guards the gap.

- [ ] **Step 11: Commit**

```bash
git add backend/pyproject.toml backend/alembic.ini backend/alembic backend/ley_khaa/db.py backend/ley_khaa/api/app.py backend/tests/test_migrations.py
git commit -m "chore: add alembic migrations with a model-drift guard"
```

---

### Task 2: Schema and state machine for an interruptible task

**Files:**
- Modify: `backend/ley_khaa/domain/states.py`
- Modify: `backend/ley_khaa/domain/models.py`
- Modify: `backend/ley_khaa/persistence/orm.py`
- Modify: `backend/ley_khaa/persistence/repository.py`
- Modify: `backend/ley_khaa/persistence/candidate_repository.py`
- Create: `backend/ley_khaa/interpreter/__init__.py` (empty)
- Create: `backend/ley_khaa/interpreter/spec.py`
- Create: `backend/alembic/versions/0002_autonomy.py`
- Test: `backend/tests/test_states.py` (extend)
- Test: `backend/tests/test_task_spec.py`
- Test: `backend/tests/test_repository.py` (extend)

**Interfaces:**
- Consumes: `run_migrations()` from Task 1.
- Produces:
  - `TaskSpec` (Pydantic v2, `extra="forbid"`) with fields `intent, inputs, operation, output_format, recipient, urgency, missing_fields, source_message_ids, certainty`.
  - New `TaskState` transitions: `CLASSIFIED → NEEDS_CLARIFICATION`, `INTERPRETED → NEEDS_CLARIFICATION`, `NEEDS_CLARIFICATION → CLASSIFIED`, `AWAITING_APPROVAL → INTERPRETED`.
  - `TaskRepository.claim(task_id, *, expected: TaskState, target: TaskState) -> bool`, `.save_spec(task_id, spec) -> TaskRow`, `.save_recommendation(task_id, *, mode: str, confidence: float, risk: float, reason: str) -> TaskRow`, `.set_override(task_id, mode: str | None) -> TaskRow`, `.set_open_question(task_id, question: str | None) -> TaskRow`, `.append_source_messages(task_id, message_ids: list[str]) -> TaskRow`, `.record_failure(task_id, reason: str) -> TaskRow`, `.increment_interpret_attempts(task_id) -> int`, `.increment_clarification_rounds(task_id) -> int`, `.list_by_state(state: TaskState) -> list[TaskRow]`, and `create(..., candidate_id: str | None = None)`.
  - `CandidateRepository.get(candidate_id) -> CandidateRow | None`.
  - `TaskRow.effective_mode` property.
  - `MessageRow.reply_to_task_id`, `Message.reply_to_task_id`.

**Why the new transitions:** Phase 1 declared `NEEDS_CLARIFICATION` but the table only allowed reaching it from `AWAITING_APPROVAL` and `VALIDATING`. The interpreter discovers gaps at `classified`, and an answer has to send the task back to `classified` to be re-interpreted. Without these four edges the clarification loop cannot be built at all.

- [ ] **Step 1: Write the failing state-machine test**

Append to `backend/tests/test_states.py`:

```python
def test_interpreter_can_escalate_to_clarification():
    assert can_transition(TaskState.CLASSIFIED, TaskState.NEEDS_CLARIFICATION)
    assert can_transition(TaskState.INTERPRETED, TaskState.NEEDS_CLARIFICATION)


def test_an_answered_clarification_goes_back_to_be_re_interpreted():
    assert can_transition(TaskState.NEEDS_CLARIFICATION, TaskState.CLASSIFIED)


def test_editing_a_parked_spec_re_enters_scoring():
    assert can_transition(TaskState.AWAITING_APPROVAL, TaskState.INTERPRETED)


def test_terminal_states_stay_terminal():
    for state in TaskState:
        assert not can_transition(TaskState.DONE, state)
        assert not can_transition(TaskState.FAILED, state)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_states.py -v`
Expected: FAIL on `test_interpreter_can_escalate_to_clarification` — `assert False`.

- [ ] **Step 3: Extend the transition table**

In `backend/ley_khaa/domain/states.py`, replace `_ALLOWED` with:

```python
# A task now pauses for a human, so the table gained four edges Phase 1 declared
# but never wired: the interpreter can discover gaps at CLASSIFIED or INTERPRETED
# and escalate; an answered clarification goes back to CLASSIFIED to be
# re-interpreted over the enlarged message set; and editing a parked spec
# re-enters scoring at INTERPRETED so the recommendation is recomputed.
_ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.RECEIVED: {TaskState.CLASSIFIED, TaskState.FAILED},
    TaskState.CLASSIFIED: {
        TaskState.INTERPRETED,
        TaskState.NEEDS_CLARIFICATION,
        TaskState.FAILED,
    },
    TaskState.INTERPRETED: {
        TaskState.AWAITING_APPROVAL,
        TaskState.EXECUTING,
        TaskState.NEEDS_CLARIFICATION,
        TaskState.FAILED,
    },
    TaskState.AWAITING_APPROVAL: {
        TaskState.EXECUTING,
        TaskState.INTERPRETED,
        TaskState.NEEDS_CLARIFICATION,
        TaskState.FAILED,
    },
    TaskState.EXECUTING: {TaskState.VALIDATING, TaskState.FAILED},
    TaskState.VALIDATING: {TaskState.DONE, TaskState.NEEDS_CLARIFICATION, TaskState.FAILED},
    TaskState.NEEDS_CLARIFICATION: {
        TaskState.CLASSIFIED,
        TaskState.INTERPRETED,
        TaskState.AWAITING_APPROVAL,
        TaskState.EXECUTING,
        TaskState.FAILED,
    },
    TaskState.DONE: set(),
    TaskState.FAILED: set(),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_states.py -v`
Expected: PASS.

- [ ] **Step 5: Write the failing `TaskSpec` test**

```python
# backend/tests/test_task_spec.py
import pytest
from pydantic import ValidationError

from ley_khaa.interpreter.spec import TaskSpec


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two security universes",
        inputs=["bloomberg_universe", "factset_universe"],
        operation="set_difference",
        output_format="xlsx",
        recipient="boss",
        urgency="normal",
        missing_fields=[],
        source_message_ids=["m1", "m2"],
        certainty=0.9,
    )
    return TaskSpec(**{**base, **overrides})


def test_a_complete_spec_validates():
    spec = _spec()
    assert spec.operation == "set_difference"
    assert spec.certainty == 0.9


def test_certainty_is_bounded():
    with pytest.raises(ValidationError):
        _spec(certainty=1.4)


def test_urgency_is_constrained():
    with pytest.raises(ValidationError):
        _spec(urgency="whenever")


def test_unknown_fields_are_rejected():
    """A typo in an edit_spec patch must 422, not vanish silently."""
    with pytest.raises(ValidationError):
        _spec(outupt_format="xlsx")


def test_optional_fields_default_sensibly():
    spec = TaskSpec(intent="x", operation="y", output_format="z", certainty=0.5)
    assert spec.inputs == []
    assert spec.recipient is None
    assert spec.urgency == "normal"
    assert spec.missing_fields == []
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_task_spec.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.interpreter'`.

- [ ] **Step 7: Write `TaskSpec`**

```python
# backend/ley_khaa/interpreter/spec.py
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Urgency = Literal["low", "normal", "high"]


class TaskSpec(BaseModel):
    """The validated interpretation of a crystallized request (spec §5.5).

    `extra="forbid"` is deliberate: edit_spec merges a caller-supplied patch into
    this model, and a misspelled key must be a loud 422 rather than a silently
    discarded edit the human believes they made.
    """

    model_config = ConfigDict(extra="forbid")

    intent: str
    inputs: list[str] = Field(default_factory=list)
    operation: str
    output_format: str
    recipient: str | None = None
    urgency: Urgency = "normal"
    missing_fields: list[str] = Field(default_factory=list)
    source_message_ids: list[str] = Field(default_factory=list)
    # The model's own confidence in this interpretation. §5.7 names "interpreter
    # certainty" as an autonomy input but §5.5 gives it nowhere to live, so it
    # lives here.
    certainty: float = Field(ge=0.0, le=1.0)
```

Also create an empty `backend/ley_khaa/interpreter/__init__.py`.

- [ ] **Step 8: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_task_spec.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 9: Write the failing repository tests**

Append to `backend/tests/test_repository.py`:

```python
from ley_khaa.autonomy.modes import AutonomyMode  # noqa: F401 — added in Task 4
from ley_khaa.domain.states import TaskState
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.persistence.repository import TaskRepository


def _task(repo):
    return repo.create(project="default", title="t", source_message_ids=["m1"])


def test_claim_wins_once_and_only_once(session):
    repo = TaskRepository(session)
    task = _task(repo)
    assert repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    # The loser of the race must get False, not an exception: two concurrent
    # sweeps advancing the same task is normal, not an error.
    assert not repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    assert repo.get(task.id).state == TaskState.CLASSIFIED.value


def test_save_spec_round_trips(session):
    repo = TaskRepository(session)
    task = _task(repo)
    spec = TaskSpec(intent="i", operation="o", output_format="f", certainty=0.7)
    repo.save_spec(task.id, spec)
    assert TaskSpec.model_validate(repo.get(task.id).spec).intent == "i"


def test_effective_mode_prefers_the_human_override(session):
    repo = TaskRepository(session)
    task = _task(repo)
    repo.save_recommendation(task.id, mode="suggest", confidence=0.4, risk=0.7, reason="r")
    assert repo.get(task.id).effective_mode == "suggest"
    repo.set_override(task.id, "auto")
    assert repo.get(task.id).effective_mode == "auto"
    # Clearing the override falls back to the recommendation rather than sticking.
    repo.set_override(task.id, None)
    assert repo.get(task.id).effective_mode == "suggest"


def test_append_source_messages_does_not_duplicate(session):
    repo = TaskRepository(session)
    task = _task(repo)
    repo.append_source_messages(task.id, ["m2", "m1"])
    assert repo.get(task.id).source_message_ids == ["m1", "m2"]


def test_counters_increment_and_return_the_new_value(session):
    repo = TaskRepository(session)
    task = _task(repo)
    assert repo.increment_interpret_attempts(task.id) == 1
    assert repo.increment_interpret_attempts(task.id) == 2
    assert repo.increment_clarification_rounds(task.id) == 1


def test_list_by_state_filters(session):
    repo = TaskRepository(session)
    a, b = _task(repo), _task(repo)
    repo.claim(a.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    ids = [t.id for t in repo.list_by_state(TaskState.CLASSIFIED)]
    assert ids == [a.id]
    assert b.id not in ids
```

- [ ] **Step 10: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_repository.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.autonomy'`. Temporarily comment out that import line to see the real failures (`AttributeError: 'TaskRepository' object has no attribute 'claim'`); restore it once Task 4 lands. If the subagent prefers, drop the unused `AutonomyMode` import — it is only there to document that modes are stored as their `.value` strings.

- [ ] **Step 11: Extend `TaskRow` and `MessageRow`**

In `backend/ley_khaa/persistence/orm.py`, add to `TaskRow`:

```python
    # Set when the task came from a crystallized candidate. The back-link exists
    # because the driver needs to read the candidate's readiness when scoring,
    # and CandidateRow.task_id only points the other way.
    candidate_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recommended_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    mode_override: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk: Mapped[float | None] = mapped_column(Float, nullable=True)
    autonomy_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    open_question: Mapped[str | None] = mapped_column(String, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    interpret_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    clarification_rounds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    @property
    def effective_mode(self) -> str | None:
        """The mode actually in force. Computed, never stored, so a human's
        override cannot go stale against a later re-score."""
        return self.mode_override or self.recommended_mode
```

Add `Integer` to the `sqlalchemy` import line. Add to `MessageRow`:

```python
    # Set when this message is a reply to an existing task rather than raw intake.
    # Intake routes such a message straight to that task and skips candidate
    # formation, so it can never spawn a duplicate candidate (spec §5.8).
    reply_to_task_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
```

In `backend/ley_khaa/domain/models.py`, add to `Message`:

```python
    reply_to_task_id: str | None = None
```

- [ ] **Step 12: Extend `TaskRepository`**

In `backend/ley_khaa/persistence/repository.py`, add the `update`/`datetime` imports and these methods:

```python
    def create(
        self,
        *,
        project: str,
        title: str,
        source_message_ids: list[str],
        candidate_id: str | None = None,
    ) -> TaskRow:
        row = TaskRow(
            id=str(uuid.uuid4()),
            project=project,
            state=TaskState.RECEIVED.value,
            title=title,
            source_message_ids=source_message_ids,
            candidate_id=candidate_id,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def claim(self, task_id: str, *, expected: TaskState, target: TaskState) -> bool:
        """Atomically move a task from `expected` to `target`. True if we won it.

        The driver is re-entrant and the sweeper runs concurrently with HTTP
        handlers, so two callers can read the same task in the same state. The
        WHERE guard means exactly one of them performs the transition; the loser
        gets False and must simply stop, the same contract as
        CandidateRepository.claim_for_promotion.
        """
        ensure_transition(expected, target)
        result = self.session.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id, TaskRow.state == expected.value)
            .values(state=target.value, updated_at=datetime.now(timezone.utc))
        )
        self.session.commit()
        return result.rowcount == 1

    def _row(self, task_id: str) -> TaskRow:
        row = self.session.get(TaskRow, task_id)
        if row is None:
            raise KeyError(task_id)
        return row

    def save_spec(self, task_id: str, spec: TaskSpec) -> TaskRow:
        row = self._row(task_id)
        row.spec = spec.model_dump(mode="json")
        self.session.commit()
        self.session.refresh(row)
        return row

    def save_recommendation(
        self, task_id: str, *, mode: str, confidence: float, risk: float, reason: str
    ) -> TaskRow:
        row = self._row(task_id)
        row.recommended_mode = mode
        row.confidence = confidence
        row.risk = risk
        row.autonomy_reason = reason
        self.session.commit()
        self.session.refresh(row)
        return row

    def set_override(self, task_id: str, mode: str | None) -> TaskRow:
        row = self._row(task_id)
        row.mode_override = mode
        self.session.commit()
        self.session.refresh(row)
        return row

    def set_open_question(self, task_id: str, question: str | None) -> TaskRow:
        row = self._row(task_id)
        row.open_question = question
        self.session.commit()
        self.session.refresh(row)
        return row

    def append_source_messages(self, task_id: str, message_ids: list[str]) -> TaskRow:
        row = self._row(task_id)
        existing = list(row.source_message_ids or [])
        # Re-assigning rather than mutating: SQLAlchemy does not track in-place
        # edits to a JSON column, so row.source_message_ids.append() would not
        # be persisted.
        row.source_message_ids = existing + [m for m in message_ids if m not in existing]
        self.session.commit()
        self.session.refresh(row)
        return row

    def record_failure(self, task_id: str, reason: str) -> TaskRow:
        row = self._row(task_id)
        row.failure_reason = reason
        self.session.commit()
        self.session.refresh(row)
        return row

    def increment_interpret_attempts(self, task_id: str) -> int:
        row = self._row(task_id)
        row.interpret_attempts = (row.interpret_attempts or 0) + 1
        self.session.commit()
        self.session.refresh(row)
        return row.interpret_attempts

    def increment_clarification_rounds(self, task_id: str) -> int:
        row = self._row(task_id)
        row.clarification_rounds = (row.clarification_rounds or 0) + 1
        self.session.commit()
        self.session.refresh(row)
        return row.clarification_rounds

    def list_by_state(self, state: TaskState) -> list[TaskRow]:
        return list(
            self.session.scalars(
                select(TaskRow).where(TaskRow.state == state.value).order_by(TaskRow.created_at)
            )
        )
```

Add to `backend/ley_khaa/persistence/candidate_repository.py`:

```python
    def get(self, candidate_id: str) -> CandidateRow | None:
        return self.session.get(CandidateRow, candidate_id)
```

- [ ] **Step 13: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_repository.py tests/test_states.py tests/test_task_spec.py -v`
Expected: PASS.

- [ ] **Step 14: Write the migration**

```python
# backend/alembic/versions/0002_autonomy.py
"""phase 2: task spec, autonomy scoring, and task replies

Revision ID: 0002_autonomy
Revises: 0001_baseline
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_autonomy"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

_TASK_COLUMNS = [
    sa.Column("candidate_id", sa.String(), nullable=True),
    sa.Column("spec", sa.JSON(), nullable=True),
    sa.Column("recommended_mode", sa.String(), nullable=True),
    sa.Column("mode_override", sa.String(), nullable=True),
    sa.Column("confidence", sa.Float(), nullable=True),
    sa.Column("risk", sa.Float(), nullable=True),
    sa.Column("autonomy_reason", sa.String(), nullable=True),
    sa.Column("open_question", sa.String(), nullable=True),
    sa.Column("failure_reason", sa.String(), nullable=True),
    sa.Column("interpret_attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("clarification_rounds", sa.Integer(), nullable=False, server_default="0"),
]


def upgrade() -> None:
    for column in _TASK_COLUMNS:
        op.add_column("tasks", column)
    op.create_index("ix_tasks_candidate_id", "tasks", ["candidate_id"])
    op.add_column("messages", sa.Column("reply_to_task_id", sa.String(), nullable=True))
    op.create_index("ix_messages_reply_to_task_id", "messages", ["reply_to_task_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_reply_to_task_id", table_name="messages")
    op.drop_column("messages", "reply_to_task_id")
    op.drop_index("ix_tasks_candidate_id", table_name="tasks")
    for column in reversed(_TASK_COLUMNS):
        op.drop_column("tasks", column.name)
```

- [ ] **Step 15: Run the migration drift test**

Run: `cd backend && python -m pytest tests/test_migrations.py -v`
Expected: PASS. A failure here means a column in `orm.py` has no matching column in `0002_autonomy.py` — read the reported diff and fix the migration.

- [ ] **Step 16: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: PASS, all tests.

- [ ] **Step 17: Commit**

```bash
git add backend/ley_khaa backend/alembic/versions/0002_autonomy.py backend/tests
git commit -m "feat: task spec, autonomy columns, and the clarification transitions"
```

---

### Task 3: Interpreter (§5.5) and its offline stand-in

**Files:**
- Create: `backend/ley_khaa/interpreter/interpreter.py`
- Modify: `backend/ley_khaa/persistence/message_repository.py`
- Modify: `backend/ley_khaa/llm/client.py` (let `FakeLLM` raise a queued exception)
- Modify: `backend/ley_khaa/llm/heuristic.py`
- Test: `backend/tests/test_interpreter.py`
- Test: `backend/tests/test_heuristic_llm.py` (extend)

**Interfaces:**
- Consumes: `TaskSpec` and the `TaskRow` columns from Task 2; the existing `LLMClient`, `model_for(Stage.INTERPRETER)`.
- Produces:
  - `Interpreter(llm: LLMClient, messages: MessageRepository)` with `.interpret(task: TaskRow) -> TaskSpec`.
  - `MalformedSpec` — raised when the model cannot produce a valid `TaskSpec` even after one re-prompt.
  - `MessageRepository.get_many(message_ids: list[str]) -> list[MessageRow]`, oldest-first.
  - `HeuristicLLM` answers `output_format=TaskSpec`.

**Two failure classes, deliberately kept apart.** A *malformed* response is bad content: re-prompt once with an explicit schema reminder, then raise `MalformedSpec` so the driver can hand it to a human. A *transport* failure (network, 429, 5xx) is not the model's fault and must not consume the retry — it propagates out of `interpret()` untouched, and the driver leaves the task where it is for the sweeper to pick up. Conflating the two is how a recoverable task ends up dead.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_interpreter.py
import pytest
from pydantic import ValidationError

from ley_khaa.domain.models import Message
from ley_khaa.interpreter.interpreter import Interpreter, MalformedSpec
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.router import OPUS
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two universes",
        inputs=["bloomberg", "factset"],
        operation="set_difference",
        output_format="xlsx",
        certainty=0.9,
    )
    return TaskSpec(**{**base, **overrides})


def _task_with_messages(session, texts):
    messages = MessageRepository(session)
    rows = [
        messages.add(Message(source="s", client="c", conversation_id="conv-1", author="boss", text=t))
        for t in texts
    ]
    task = TaskRepository(session).create(
        project="default", title="compare universes", source_message_ids=[r.id for r in rows]
    )
    return task, rows


def test_interpret_returns_a_validated_spec(session):
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    llm = FakeLLM([_spec()])
    spec = Interpreter(llm, MessageRepository(session)).interpret(task)
    assert spec.operation == "set_difference"


def test_interpret_routes_to_opus(session):
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    llm = FakeLLM([_spec()])
    Interpreter(llm, MessageRepository(session)).interpret(task)
    assert llm.calls[0].choice.model == OPUS
    assert llm.calls[0].choice.supports_thinking is True


def test_the_prompt_carries_the_tasks_own_messages(session):
    task, rows = _task_with_messages(session, ["compare bloomberg against factset", "as excel"])
    llm = FakeLLM([_spec()])
    Interpreter(llm, MessageRepository(session)).interpret(task)
    user = llm.calls[0].user
    assert "compare bloomberg against factset" in user
    assert "as excel" in user
    assert rows[0].id in user


def test_malformed_output_is_re_prompted_once_then_succeeds(session):
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    bad = ValidationError.from_exception_data("TaskSpec", [])
    llm = FakeLLM([bad, _spec()])
    spec = Interpreter(llm, MessageRepository(session)).interpret(task)
    assert spec.operation == "set_difference"
    assert len(llm.calls) == 2
    # The retry says something the first prompt did not, or it is not a retry.
    assert llm.calls[1].system != llm.calls[0].system


def test_malformed_twice_raises_rather_than_looping(session):
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    bad = ValidationError.from_exception_data("TaskSpec", [])
    llm = FakeLLM([bad, bad])
    with pytest.raises(MalformedSpec):
        Interpreter(llm, MessageRepository(session)).interpret(task)
    assert len(llm.calls) == 2


def test_transport_failure_propagates_without_consuming_the_retry(session):
    """A network error is not bad content: the driver retries it, not the interpreter."""
    task, _ = _task_with_messages(session, ["compare bloomberg against factset"])
    llm = FakeLLM([ConnectionError("boom")])
    with pytest.raises(ConnectionError):
        Interpreter(llm, MessageRepository(session)).interpret(task)
    assert len(llm.calls) == 1


def test_hallucinated_message_ids_are_dropped(session):
    """Same lesson as the crystallizer: model-supplied ids are untrusted."""
    task, rows = _task_with_messages(session, ["compare bloomberg against factset"])
    llm = FakeLLM([_spec(source_message_ids=[rows[0].id, "not-a-real-id"])])
    spec = Interpreter(llm, MessageRepository(session)).interpret(task)
    assert spec.source_message_ids == [rows[0].id]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_interpreter.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.interpreter.interpreter'`.

- [ ] **Step 3: Let `FakeLLM` raise**

In `backend/ley_khaa/llm/client.py`, change `FakeLLM.parse`:

```python
    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        self.calls.append(RecordedCall(choice=choice, system=system, user=user, output_format=output_format))
        assert self.responses, "FakeLLM exhausted: more parse() calls than queued responses"
        response = self.responses.pop(0)
        # A queued exception is raised rather than returned, so tests can drive
        # the malformed-output and transport-failure branches without mocks.
        if isinstance(response, Exception):
            raise response
        return response
```

- [ ] **Step 4: Add `get_many` to `MessageRepository`**

```python
    def get_many(self, message_ids: list[str]) -> list[MessageRow]:
        """The named messages, oldest-first. Unknown ids are skipped."""
        if not message_ids:
            return []
        rows = self.session.scalars(select(MessageRow).where(MessageRow.id.in_(message_ids)))
        return sorted(rows, key=lambda r: (r.timestamp, r.id))
```

- [ ] **Step 5: Write the interpreter**

```python
# backend/ley_khaa/interpreter/interpreter.py
from pydantic import ValidationError

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import MessageRow, TaskRow
from .spec import TaskSpec

SYSTEM = """You turn a crystallized work request into a precise, executable specification.

You receive the messages that make up one request. Produce a single TaskSpec.

Rules:
- intent: one sentence describing what the human actually wants.
- operation: a short verb-phrase naming the transformation (e.g. set_difference,
  summary_stats, reconcile, extract). Invent one if nothing standard fits.
- inputs: the named data sources the work needs.
- output_format: the deliverable's format (xlsx, csv, docx, markdown, ...).
- recipient: who the result goes to, or null if the request does not say.
- urgency: low, normal, or high — read it from the conversation, do not guess high.
- missing_fields: name anything you genuinely cannot determine from the messages.
  Be honest here: this is what decides whether a human is asked before work starts.
  An empty list means the request is complete enough to act on.
- source_message_ids: only ids that appear in the messages you were given.
- certainty: your own confidence in this interpretation, 0.0 to 1.0."""

_RETRY_SUFFIX = """

Your previous response could not be parsed into the required schema. Return a
single, complete TaskSpec object with every required field present and correctly
typed. Do not include any commentary."""


class MalformedSpec(Exception):
    """The model could not produce a valid TaskSpec, even after a re-prompt."""


class Interpreter:
    """Crystallized request -> validated TaskSpec (spec §5.5)."""

    def __init__(self, llm: LLMClient, messages: MessageRepository) -> None:
        self.llm = llm
        self.messages = messages

    def interpret(self, task: TaskRow) -> TaskSpec:
        rows = self.messages.get_many(list(task.source_message_ids or []))
        user = _render(task, rows)

        try:
            spec = self._call(SYSTEM, user)
        except ValidationError:
            # Bad content, not a broken connection: one re-prompt with the schema
            # spelled out, then give up and let a human rescue it (§5.5).
            try:
                spec = self._call(SYSTEM + _RETRY_SUFFIX, user)
            except ValidationError as exc:
                raise MalformedSpec(str(exc)) from exc

        # Model-supplied ids are untrusted — the same lesson the crystallizer
        # learned. A hallucinated id must never reach the executor.
        known = {row.id for row in rows}
        return spec.model_copy(
            update={"source_message_ids": [m for m in spec.source_message_ids if m in known]}
        )

    def _call(self, system: str, user: str) -> TaskSpec:
        return self.llm.parse(
            choice=model_for(Stage.INTERPRETER),
            system=system,
            user=user,
            output_format=TaskSpec,
        )


def _render(task: TaskRow, rows: list[MessageRow]) -> str:
    lines = ["## Request", f"title: {task.title}", "", "## Messages"]
    for row in rows:
        lines.append(f"[{row.id}] {row.author}: {row.text}")
        for attachment in row.attachments or []:
            lines.append(f"    attachment: {attachment['kind']} named {attachment['name']}")
    return "\n".join(lines)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_interpreter.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 7: Write the failing heuristic test**

Append to `backend/tests/test_heuristic_llm.py`:

```python
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.router import Stage, model_for

_INTERPRETER = model_for(Stage.INTERPRETER)

_UNIVERSE_PROMPT = """## Request
title: compare the Bloomberg universe against FactSet

## Messages
[m1] boss: can you compare the Bloomberg universe against FactSet
[m2] boss: month end please, and send me what's missing as an Excel file"""

_VAGUE_PROMPT = """## Request
title: put together a report

## Messages
[m1] boss: can you put together a report on the holdings"""


def _interpret(prompt: str) -> TaskSpec:
    return HeuristicLLM().parse(
        choice=_INTERPRETER, system="", user=prompt, output_format=TaskSpec
    )


def test_heuristic_reads_a_complete_request(session=None):
    spec = _interpret(_UNIVERSE_PROMPT)
    assert spec.operation == "set_difference"
    assert spec.output_format == "xlsx"
    assert spec.missing_fields == []
    assert spec.source_message_ids == ["m1", "m2"]


def test_heuristic_flags_a_missing_output_format():
    spec = _interpret(_VAGUE_PROMPT)
    assert "output_format" in spec.missing_fields


def test_heuristic_never_earns_auto_on_its_own():
    """The offline stand-in is a regex, not a mind. It must not claim high
    certainty, or a fresh clone with no API key would silently run tasks
    end-to-end on keyword matching."""
    assert _interpret(_UNIVERSE_PROMPT).certainty < 0.85


def test_heuristic_reads_urgency_and_recipient():
    spec = _interpret(
        "## Messages\n[m1] boss: urgent - compare the lists and send to alice as csv"
    )
    assert spec.urgency == "high"
    assert spec.recipient == "alice"
    assert spec.output_format == "csv"
```

- [ ] **Step 8: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_heuristic_llm.py -v`
Expected: FAIL with `NotImplementedError: HeuristicLLM has no rule for TaskSpec`.

- [ ] **Step 9: Teach `HeuristicLLM` to produce a `TaskSpec`**

In `backend/ley_khaa/llm/heuristic.py`, add the import and the rule:

```python
from ..interpreter.spec import TaskSpec
```

```python
_OPERATIONS = (
    (("compare", "difference", "missing", "reconcile", "against"), "set_difference"),
    (("summar", "stats", "group by", "breakdown", "average"), "summary_stats"),
)
_FORMATS = (
    (("excel", "xlsx", "spreadsheet"), "xlsx"),
    (("csv",), "csv"),
    (("word", "docx"), "docx"),
    (("markdown", "md"), "markdown"),
)
_SOURCE_WORDS = ("bloomberg", "factset", "holdings", "universe", "portfolio", "trades")
_URGENT_WORDS = ("urgent", "asap", "right away", "eod", "immediately")
_RECIPIENT = re.compile(r"send (?:it |them |this )?to (?P<who>[a-z][\w.-]*)")

# Deliberately mediocre: a regex has not understood anything, and the autonomy
# engine must never hand Auto to keyword matching. See the threshold in
# ley_khaa/autonomy/engine.py.
_HEURISTIC_CERTAINTY = 0.55
```

```python
    def _interpret(self, user: str) -> TaskSpec:
        ids: list[str] = []
        texts: list[str] = []
        for line in user.splitlines():
            m = _MESSAGE_LINE.match(line)
            if not m:
                continue
            ids.append(m.group("id"))
            texts.append(m.group("text"))
        blob = " ".join(texts).lower()

        operation = _first_match(_OPERATIONS, blob, default="synthesize")
        output_format = _first_match(_FORMATS, blob, default="")
        inputs = [word for word in _SOURCE_WORDS if word in blob]

        recipient = None
        match = _RECIPIENT.search(blob)
        if match:
            recipient = match.group("who")
        elif "send me" in blob:
            recipient = "the requester"

        missing = []
        if not output_format:
            missing.append("output_format")
        if not inputs:
            missing.append("inputs")

        return TaskSpec(
            intent=texts[0] if texts else "unknown request",
            inputs=inputs,
            operation=operation,
            output_format=output_format or "unknown",
            recipient=recipient,
            urgency="high" if any(w in blob for w in _URGENT_WORDS) else "normal",
            missing_fields=missing,
            source_message_ids=ids,
            certainty=_HEURISTIC_CERTAINTY,
        )
```

Add the dispatch line to `parse`, above the `raise NotImplementedError`:

```python
        if output_format is TaskSpec:
            return self._interpret(user)
```

And the module-level helper:

```python
def _first_match(table, blob: str, *, default: str) -> str:
    for words, value in table:
        if any(word in blob for word in words):
            return value
    return default
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_heuristic_llm.py tests/test_interpreter.py -v`
Expected: PASS.

Note: importing `TaskSpec` into `heuristic.py` mirrors the existing import of `CrystallizerOutput` from `crystallizer.engine` — the stand-in necessarily knows the shapes it stands in for.

- [ ] **Step 11: Run the full suite and commit**

Run: `cd backend && python -m pytest -q`
Expected: PASS, all tests.

```bash
git add backend/ley_khaa/interpreter backend/ley_khaa/llm backend/ley_khaa/persistence/message_repository.py backend/tests
git commit -m "feat: interpreter turning a crystallized request into a validated spec"
```

---

### Task 4: Autonomy engine (§5.7, headline #2)

**Files:**
- Create: `backend/ley_khaa/autonomy/__init__.py` (empty)
- Create: `backend/ley_khaa/autonomy/modes.py`
- Create: `backend/ley_khaa/autonomy/engine.py`
- Test: `backend/tests/test_autonomy_engine.py`

**Interfaces:**
- Consumes: `TaskSpec` from Task 2.
- Produces: `AutonomyMode` (`SUGGEST`/`COPILOT`/`AUTO`, values `"suggest"`/`"copilot"`/`"auto"`), `Recommendation(mode: AutonomyMode, confidence: float, risk: float, reason: str)`, and `recommend(spec: TaskSpec, *, candidate_missing_fields: list[str] | None = None) -> Recommendation`.

**This is the headline feature, so the policy has to be readable as a specification.** No I/O, no LLM call, no hidden state — a pure function over data the pipeline already produced. Every constant is named, every rule contributes a clause to the plain-English reason, and there is one test per row of the table.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_autonomy_engine.py
from ley_khaa.autonomy.engine import recommend
from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.interpreter.spec import TaskSpec


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two security universes",
        inputs=["bloomberg", "factset"],
        operation="set_difference",
        output_format="xlsx",
        recipient=None,
        urgency="normal",
        missing_fields=[],
        source_message_ids=["m1"],
        certainty=0.95,
    )
    return TaskSpec(**{**base, **overrides})


def test_confident_and_harmless_earns_auto():
    rec = recommend(_spec())
    assert rec.mode is AutonomyMode.AUTO


def test_delivering_to_someone_pulls_it_back_from_auto():
    """Irreversibility is the point: a report you can re-run is not a sent email."""
    rec = recommend(_spec(recipient="boss"))
    assert rec.mode is AutonomyMode.COPILOT
    assert "delivers" in rec.reason


def test_money_and_ambiguity_stay_in_suggest():
    rec = recommend(_spec(intent="settle the invoice differences", certainty=0.6,
                          missing_fields=["output_format"]))
    assert rec.mode is AutonomyMode.SUGGEST
    assert "touches money" in rec.reason


def test_each_missing_field_costs_confidence():
    none_missing = recommend(_spec()).confidence
    one_missing = recommend(_spec(missing_fields=["output_format"])).confidence
    two_missing = recommend(_spec(missing_fields=["output_format", "inputs"])).confidence
    assert none_missing > one_missing > two_missing


def test_an_unsettled_conversation_costs_confidence():
    settled = recommend(_spec()).confidence
    unsettled = recommend(_spec(), candidate_missing_fields=["deadline"]).confidence
    assert unsettled < settled


def test_urgency_raises_risk():
    assert recommend(_spec(urgency="high")).risk > recommend(_spec()).risk


def test_scores_stay_in_range():
    hot = _spec(intent="urgent wire payment", recipient="boss", urgency="high",
                certainty=0.1, missing_fields=["a", "b", "c", "d", "e"])
    rec = recommend(hot)
    assert 0.0 <= rec.confidence <= 1.0
    assert 0.0 <= rec.risk <= 1.0


def test_the_reason_reads_like_the_spec_examples():
    assert recommend(_spec()).reason == "95% sure, low risk → I suggest Auto"
    reason = recommend(_spec(certainty=0.5, missing_fields=["output_format"])).reason
    assert reason.startswith("30% sure")
    assert reason.endswith("→ stay in Suggest")
    assert "1 field(s) still unknown" in reason


def test_recommendation_is_deterministic():
    """Same inputs, same answer — every time. This is why it is not an LLM call."""
    spec = _spec(certainty=0.7, recipient="boss")
    assert recommend(spec) == recommend(spec)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_autonomy_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.autonomy'`.

- [ ] **Step 3: Write the modes**

```python
# backend/ley_khaa/autonomy/modes.py
from enum import Enum


class AutonomyMode(str, Enum):
    """How much the system does before a human sees it (spec §5.7).

    SUGGEST and COPILOT behave identically in 0.3.0 — both park at the single
    approval gate. They diverge in 0.4.0, when the real executor has mid-run
    checkpoints for COPILOT to stop at.
    """

    SUGGEST = "suggest"
    COPILOT = "copilot"
    AUTO = "auto"
```

- [ ] **Step 4: Write the engine**

```python
# backend/ley_khaa/autonomy/engine.py
from dataclasses import dataclass

from ..interpreter.spec import TaskSpec
from .modes import AutonomyMode

# --- confidence penalties -------------------------------------------------
_MISSING_FIELD_PENALTY = 0.2
_UNSETTLED_CONVERSATION_PENALTY = 0.1

# --- risk contributions ---------------------------------------------------
# Everything carries some risk; a request that only reads data carries little.
_BASELINE_RISK = 0.1
_DELIVERY_RISK = 0.35
_MONEY_RISK = 0.4
_URGENCY_RISK = 0.15

_MONEY_TERMS = (
    "invoice", "payment", "wire", "settle", "trade", "refund", "payroll", "billing", "$",
)
_DELIVERY_OPS = (
    "send", "email", "post", "deliver", "publish", "delete", "overwrite", "submit",
)

# --- thresholds -----------------------------------------------------------
# Auto is deliberately hard to earn: it is the only mode that acts without a human.
_AUTO_CONFIDENCE, _AUTO_RISK = 0.85, 0.25
_COPILOT_CONFIDENCE, _COPILOT_RISK = 0.6, 0.6

_VERB = {
    AutonomyMode.AUTO: "I suggest Auto",
    AutonomyMode.COPILOT: "I suggest Co-pilot",
    AutonomyMode.SUGGEST: "stay in Suggest",
}


@dataclass(frozen=True)
class Recommendation:
    mode: AutonomyMode
    confidence: float
    risk: float
    reason: str


def recommend(
    spec: TaskSpec, *, candidate_missing_fields: list[str] | None = None
) -> Recommendation:
    """Score a spec and recommend a mode, with a reason a human can argue with.

    Pure and deterministic on purpose (§5.7): the dial is the feature a reader
    will poke at hardest, so its behaviour must be reproducible and its rules
    readable in one screen — not hidden inside a model call.
    """
    confidence, confidence_clauses = _confidence(spec, candidate_missing_fields or [])
    risk, risk_clauses = _risk(spec)
    mode = _mode(confidence, risk)
    return Recommendation(
        mode=mode,
        confidence=confidence,
        risk=risk,
        reason=_reason(mode, confidence, risk, confidence_clauses + risk_clauses),
    )


def _confidence(spec: TaskSpec, candidate_missing: list[str]) -> tuple[float, list[str]]:
    clauses: list[str] = []
    score = spec.certainty
    if spec.missing_fields:
        score -= _MISSING_FIELD_PENALTY * len(spec.missing_fields)
        clauses.append(f"{len(spec.missing_fields)} field(s) still unknown")
    if candidate_missing:
        score -= _UNSETTLED_CONVERSATION_PENALTY
        clauses.append("the conversation never settled the details")
    return _clamp(score), clauses


def _risk(spec: TaskSpec) -> tuple[float, list[str]]:
    clauses: list[str] = []
    score = _BASELINE_RISK
    haystack = " ".join([spec.intent, spec.operation, spec.output_format, *spec.inputs]).lower()

    if spec.recipient or any(op in haystack for op in _DELIVERY_OPS):
        score += _DELIVERY_RISK
        clauses.append("it delivers something to someone")
    if any(term in haystack for term in _MONEY_TERMS):
        score += _MONEY_RISK
        clauses.append("it touches money")
    if spec.urgency == "high":
        score += _URGENCY_RISK
        clauses.append("it is marked urgent")
    return _clamp(score), clauses


def _mode(confidence: float, risk: float) -> AutonomyMode:
    if confidence >= _AUTO_CONFIDENCE and risk <= _AUTO_RISK:
        return AutonomyMode.AUTO
    if confidence >= _COPILOT_CONFIDENCE and risk <= _COPILOT_RISK:
        return AutonomyMode.COPILOT
    return AutonomyMode.SUGGEST


def _reason(mode: AutonomyMode, confidence: float, risk: float, clauses: list[str]) -> str:
    head = f"{confidence:.0%} sure, {_risk_label(risk)} risk"
    if clauses:
        head += " — " + ", ".join(clauses)
    return f"{head} → {_VERB[mode]}"


def _risk_label(risk: float) -> str:
    if risk <= _AUTO_RISK:
        return "low"
    if risk <= _COPILOT_RISK:
        return "medium"
    return "high"


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 4)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_autonomy_engine.py -v`
Expected: PASS, 9 tests. If `test_the_reason_reads_like_the_spec_examples` fails on rounding, check `_clamp` — `0.5 - 0.2 = 0.3` must render as `30%`.

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/autonomy backend/tests/test_autonomy_engine.py
git commit -m "feat: deterministic autonomy engine recommending suggest/co-pilot/auto"
```

---

### Task 5: TaskDriver — the automatic path

**Files:**
- Create: `backend/ley_khaa/orchestrator/driver.py`
- Modify: `backend/ley_khaa/orchestrator/orchestrator.py`
- Test: `backend/tests/test_driver.py`
- Test: `backend/tests/test_orchestrator.py` (update — `STUB_PATH` is gone)

**Interfaces:**
- Consumes: `Interpreter`/`MalformedSpec` (Task 3), `recommend`/`AutonomyMode` (Task 4), the repository methods from Task 2.
- Produces: `TaskDriver(repo, *, llm, messages, candidates)` with `.advance(task_id) -> TaskRow`. Task 6 adds the four human actions to this same class. `Orchestrator._promote()` now ends with `self.driver.advance(task.id)` and `STUB_PATH` is deleted.

**Termination is structural, not hopeful.** `advance()` runs a bounded loop; each pass performs at most one transition and any step returning `False` (a lost claim, a transport failure) ends the loop immediately. A task in a `_WAITING` state returns straight away. There is no path on which this spins.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_driver.py
import pytest

from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.crystallizer.candidate import CandidateState
from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two universes",
        inputs=["bloomberg", "factset"],
        operation="set_difference",
        output_format="xlsx",
        certainty=0.95,
    )
    return TaskSpec(**{**base, **overrides})


def _setup(session, responses, *, candidate_id=None):
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(
        Message(source="s", client="c", conversation_id="conv-1", author="boss",
                text="compare bloomberg against factset")
    )
    task = repo.create(
        project="default", title="compare universes",
        source_message_ids=[row.id], candidate_id=candidate_id,
    )
    driver = TaskDriver(
        repo, llm=FakeLLM(responses), messages=messages,
        candidates=CandidateRepository(session),
    )
    return repo, driver, task


def test_a_low_risk_confident_task_runs_straight_through(session):
    """Auto skips the gate — this is the dial actually changing behaviour."""
    repo, driver, task = _setup(session, [_spec()])
    result = driver.advance(task.id)
    assert result.state == TaskState.DONE.value
    assert result.recommended_mode == AutonomyMode.AUTO.value


def test_a_risky_task_parks_for_a_human(session):
    repo, driver, task = _setup(session, [_spec(recipient="boss")])
    result = driver.advance(task.id)
    assert result.state == TaskState.AWAITING_APPROVAL.value
    assert result.recommended_mode == AutonomyMode.COPILOT.value
    assert "delivers" in result.autonomy_reason


def test_a_human_pinned_mode_beats_the_recommendation(session):
    repo, driver, task = _setup(session, [_spec(recipient="boss")])
    repo.set_override(task.id, AutonomyMode.AUTO.value)
    assert driver.advance(task.id).state == TaskState.DONE.value


def test_missing_fields_send_the_task_to_clarification(session):
    repo, driver, task = _setup(session, [_spec(missing_fields=["output_format"])])
    result = driver.advance(task.id)
    assert result.state == TaskState.NEEDS_CLARIFICATION.value
    assert "output_format" in result.open_question


def test_the_spec_is_persisted_before_the_gate(session):
    repo, driver, task = _setup(session, [_spec(recipient="boss")])
    driver.advance(task.id)
    assert TaskSpec.model_validate(repo.get(task.id).spec).operation == "set_difference"


def test_a_malformed_spec_asks_the_human_rather_than_failing(session):
    """A task a human could rescue must not be marked failed."""
    from pydantic import ValidationError

    bad = ValidationError.from_exception_data("TaskSpec", [])
    repo, driver, task = _setup(session, [bad, bad])
    result = driver.advance(task.id)
    assert result.state == TaskState.NEEDS_CLARIFICATION.value


def test_a_transport_failure_leaves_the_task_retryable(session):
    repo, driver, task = _setup(session, [ConnectionError("boom")])
    result = driver.advance(task.id)
    assert result.state == TaskState.CLASSIFIED.value
    assert result.interpret_attempts == 1


def test_repeated_transport_failures_eventually_fail_the_task(session):
    repo, driver, task = _setup(session, [ConnectionError("boom")] * 3)
    for _ in range(3):
        driver.advance(task.id)
    result = repo.get(task.id)
    assert result.state == TaskState.FAILED.value
    assert "unavailable" in result.failure_reason


def test_the_candidates_unsettled_details_lower_confidence(session):
    candidates = CandidateRepository(session)
    candidate = candidates.upsert(
        conversation_id="conv-1", candidate_key="k", title="t", summary="s",
        state=CandidateState.READY, message_ids=[], missing_fields=["deadline"],
        open_question=None,
    )
    repo, driver, task = _setup(session, [_spec()], candidate_id=candidate.id)
    result = driver.advance(task.id)
    assert result.confidence < 0.95


def test_advance_on_a_finished_task_is_a_no_op(session):
    repo, driver, task = _setup(session, [_spec()])
    driver.advance(task.id)
    # No responses left: a second advance must not call the LLM again.
    assert driver.advance(task.id).state == TaskState.DONE.value


def test_advance_on_an_unknown_task_raises(session):
    _, driver, _ = _setup(session, [])
    with pytest.raises(KeyError):
        driver.advance("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_driver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ley_khaa.orchestrator.driver'`.

- [ ] **Step 3: Write the driver**

```python
# backend/ley_khaa/orchestrator/driver.py
import logging
from collections.abc import Callable

from ..autonomy.engine import recommend
from ..autonomy.modes import AutonomyMode
from ..domain.states import TaskState
from ..interpreter.interpreter import Interpreter, MalformedSpec
from ..interpreter.spec import TaskSpec
from ..llm.client import LLMClient
from ..persistence.candidate_repository import CandidateRepository
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import TaskRow
from ..persistence.repository import TaskRepository

logger = logging.getLogger(__name__)

# Where a task comes to rest on its own: finished, or a human owes it something.
_WAITING = {
    TaskState.AWAITING_APPROVAL,
    TaskState.NEEDS_CLARIFICATION,
    TaskState.DONE,
    TaskState.FAILED,
}

# Each pass performs at most one transition, so this only has to exceed the
# longest automatic run (received → classified → interpreted → executing →
# validating → done). It exists so that a future bug cannot spin forever.
_MAX_STEPS = 10

# A transport failure is retried by the sweeper, not in a tight loop.
_MAX_INTERPRET_ATTEMPTS = 3
# After this many rounds, stop asking and let the human decide with the gaps
# visible. Without a cap, a model that keeps reporting the same gap and a human
# who keeps not answering it will ping-pong forever.
_MAX_CLARIFICATION_ROUNDS = 3


class TaskDriver:
    """The single place that knows how far a task can go without a human.

    advance() is re-entrant: each human action performs its own small transition
    and then calls it again, so "what happens after approval" and "what happens
    after a clarification answer" are the same code and cannot drift apart.
    """

    def __init__(
        self,
        repo: TaskRepository,
        *,
        llm: LLMClient,
        messages: MessageRepository,
        candidates: CandidateRepository,
    ) -> None:
        self.repo = repo
        self.messages = messages
        self.candidates = candidates
        self.interpreter = Interpreter(llm, messages)

    def advance(self, task_id: str) -> TaskRow:
        """Push a task as far as it can go unattended, then return where it landed."""
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

    # --- automatic steps --------------------------------------------------

    def _classify(self, row: TaskRow) -> bool:
        # Classification already happened: the crystallizer decided this was a
        # real work request before it became a Task. The state is kept because
        # §5.9 names it and project routing will hang off it in Phase 0.5.0.
        return self.repo.claim(row.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)

    def _interpret(self, row: TaskRow) -> bool:
        try:
            spec = self.interpreter.interpret(row)
        except MalformedSpec:
            logger.info("task %s produced no valid spec; handing it to a human", row.id)
            self.repo.set_open_question(
                row.id,
                "I could not turn this into a specification. What exactly should I do?",
            )
            return self.repo.claim(
                row.id, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION
            )
        except Exception:
            # A broken connection is not a broken request. Leave the task in
            # CLASSIFIED and let the sweeper try again — that retry loop already
            # exists, so no backoff machinery is needed here.
            attempts = self.repo.increment_interpret_attempts(row.id)
            logger.exception("interpreting task %s failed (attempt %d)", row.id, attempts)
            if attempts >= _MAX_INTERPRET_ATTEMPTS:
                self.repo.record_failure(
                    row.id, f"interpreter unavailable after {attempts} attempts"
                )
                self.repo.claim(
                    row.id, expected=TaskState.CLASSIFIED, target=TaskState.FAILED
                )
            return False

        self.repo.save_spec(row.id, spec)
        if spec.missing_fields and (row.clarification_rounds or 0) < _MAX_CLARIFICATION_ROUNDS:
            self.repo.set_open_question(row.id, _question_for(spec.missing_fields))
            return self.repo.claim(
                row.id, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION
            )

        # Either the spec is complete, or we have asked enough times. The gaps
        # stay visible in spec.missing_fields; we simply stop asking about them.
        self.repo.set_open_question(row.id, None)
        return self.repo.claim(
            row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED
        )

    def _gate(self, row: TaskRow) -> bool:
        spec = TaskSpec.model_validate(row.spec)
        candidate = self.candidates.get(row.candidate_id) if row.candidate_id else None
        recommendation = recommend(
            spec,
            candidate_missing_fields=list(candidate.missing_fields or []) if candidate else [],
        )
        self.repo.save_recommendation(
            row.id,
            mode=recommendation.mode.value,
            confidence=recommendation.confidence,
            risk=recommendation.risk,
            reason=recommendation.reason,
        )
        # Re-read: effective_mode is only meaningful once the recommendation is
        # stored, and a human's override must still beat what we just computed.
        effective = self.repo.get(row.id).effective_mode
        target = (
            TaskState.EXECUTING
            if effective == AutonomyMode.AUTO.value
            else TaskState.AWAITING_APPROVAL
        )
        return self.repo.claim(row.id, expected=TaskState.INTERPRETED, target=target)

    def _execute(self, row: TaskRow) -> bool:
        # Still a stub. Phase 0.4.0 replaces this with the synthesis-first
        # executor and the Docker sandbox (§5.10).
        return self.repo.claim(row.id, expected=TaskState.EXECUTING, target=TaskState.VALIDATING)

    def _validate(self, row: TaskRow) -> bool:
        return self.repo.claim(row.id, expected=TaskState.VALIDATING, target=TaskState.DONE)


_STEPS: dict[TaskState, Callable[[TaskDriver, TaskRow], bool]] = {
    TaskState.RECEIVED: TaskDriver._classify,
    TaskState.CLASSIFIED: TaskDriver._interpret,
    TaskState.INTERPRETED: TaskDriver._gate,
    TaskState.EXECUTING: TaskDriver._execute,
    TaskState.VALIDATING: TaskDriver._validate,
}


def _question_for(missing_fields: list[str]) -> str:
    return (
        f"Before I start, I still need: {', '.join(missing_fields)}. Can you fill those in?"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_driver.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Rewire the orchestrator**

In `backend/ley_khaa/orchestrator/orchestrator.py`: delete the `STUB_PATH` constant and the `for state in STUB_PATH:` loop, add the driver, and pass the candidate back-link through.

```python
from .driver import TaskDriver
```

In `__init__`, after `self.gate = gate or ReadinessGate()`:

```python
        self.driver = TaskDriver(repo, llm=llm, messages=messages, candidates=candidates)
```

Replace the tail of `_promote`:

```python
        task = self.repo.create(
            project="default",
            title=candidate.title,
            source_message_ids=list(candidate.message_ids),
            candidate_id=candidate.id,
        )
        self.candidates.attach_task(candidate.id, task.id)
        # The driver owns everything from here: interpret, score, and either park
        # for a human or (on Auto) run through. The orchestrator's job ends at
        # turning a settled candidate into a task.
        self.driver.advance(task.id)
        return task.id
```

Add a method the sweeper will use in Task 8:

```python
    def advance_stalled(self) -> list[str]:
        """Re-drive every task that is mid-flight but not waiting on a human.

        This is what retries a task whose interpretation hit a transport failure:
        it stays in CLASSIFIED, and the next sweep picks it up.
        """
        advanced: list[str] = []
        for state in (
            TaskState.RECEIVED,
            TaskState.CLASSIFIED,
            TaskState.INTERPRETED,
            TaskState.EXECUTING,
            TaskState.VALIDATING,
        ):
            for row in self.repo.list_by_state(state):
                self.driver.advance(row.id)
                advanced.append(row.id)
        return advanced
```

- [ ] **Step 6: Update the orchestrator tests**

In `backend/tests/test_orchestrator.py`, any test asserting a promoted task reaches `done` must change: with `HeuristicLLM` the spec scores below the Auto threshold, so a promoted task now lands in `awaiting_approval`. Update those assertions and add:

```python
def test_a_promoted_task_now_parks_instead_of_racing_to_done(session):
    """The Phase 1 stub walked every task to done. That was the point of this phase."""
    orchestrator = _orchestrator(session)  # existing helper in this file
    result = orchestrator.ingest({"text": "compare bloomberg against factset and send me an excel"})
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.AWAITING_APPROVAL.value
    assert task.autonomy_reason is not None
    assert task.candidate_id is not None
```

- [ ] **Step 7: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: PASS. Expect `test_api.py` and `test_sweeper.py` to need the same `done → awaiting_approval` correction; make it.

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa/orchestrator backend/tests
git commit -m "feat: task driver replacing the stub path with interpret-score-gate"
```

---

### Task 6: The four human-in-the-loop actions (§5.8)

**Files:**
- Modify: `backend/ley_khaa/orchestrator/driver.py`
- Test: `backend/tests/test_driver_actions.py`

**Interfaces:**
- Consumes: `TaskDriver.advance` from Task 5.
- Produces: `TaskDriver.approve(task_id) -> TaskRow`, `.reject(task_id, reason: str = ...) -> TaskRow`, `.override(task_id, mode: AutonomyMode | None) -> TaskRow`, `.edit_spec(task_id, patch: dict) -> TaskRow`. All four raise `InvalidTransition` when the task is not in a state that accepts them; `edit_spec` raises `pydantic.ValidationError` on a bad patch.

**A human edit is authoritative.** `edit_spec` writes the patched spec and re-enters *scoring*, not *interpretation* — re-running the interpreter would overwrite the human's correction with the model's original reading, which is the opposite of what the human asked for.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_driver_actions.py
import pytest
from pydantic import ValidationError

from ley_khaa.autonomy.modes import AutonomyMode
from ley_khaa.domain.states import InvalidTransition, TaskState
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.domain.models import Message
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _spec(**overrides) -> TaskSpec:
    base = dict(
        intent="compare two universes", inputs=["bloomberg", "factset"],
        operation="set_difference", output_format="xlsx", certainty=0.95,
    )
    return TaskSpec(**{**base, **overrides})


def _parked(session, responses):
    """A task sitting at awaiting_approval, which is where humans meet it."""
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(Message(source="s", client="c", conversation_id="conv-1",
                               author="boss", text="compare bloomberg against factset"))
    task = repo.create(project="default", title="t", source_message_ids=[row.id])
    driver = TaskDriver(repo, llm=FakeLLM(responses), messages=messages,
                        candidates=CandidateRepository(session))
    driver.advance(task.id)
    return repo, driver, task


def test_approve_releases_a_parked_task(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    assert repo.get(task.id).state == TaskState.AWAITING_APPROVAL.value
    assert driver.approve(task.id).state == TaskState.DONE.value


def test_reject_fails_the_task_with_a_reason(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    result = driver.reject(task.id, "not what I asked for")
    assert result.state == TaskState.FAILED.value
    assert result.failure_reason == "not what I asked for"


def test_approving_twice_is_a_conflict_not_a_crash(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    driver.approve(task.id)
    with pytest.raises(InvalidTransition):
        driver.approve(task.id)


def test_overriding_to_auto_releases_the_task_on_the_spot(session):
    """This is the dial having teeth: one click moves a parked task."""
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    result = driver.override(task.id, AutonomyMode.AUTO)
    assert result.state == TaskState.DONE.value
    assert result.mode_override == AutonomyMode.AUTO.value


def test_overriding_to_suggest_keeps_the_task_parked(session):
    repo, driver, task = _parked(session, [_spec()])  # would otherwise be Auto
    # The task already ran to done under Auto, so start a fresh one pinned first.
    fresh = repo.create(project="default", title="t2", source_message_ids=[])
    repo.set_override(fresh.id, AutonomyMode.SUGGEST.value)
    driver.interpreter.llm = FakeLLM([_spec()])
    assert driver.advance(fresh.id).state == TaskState.AWAITING_APPROVAL.value


def test_clearing_the_override_falls_back_to_the_recommendation(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    repo.set_override(task.id, AutonomyMode.SUGGEST.value)
    driver.override(task.id, None)
    assert repo.get(task.id).effective_mode == AutonomyMode.COPILOT.value


def test_editing_the_spec_rescores_and_can_change_the_recommendation(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    assert repo.get(task.id).recommended_mode == AutonomyMode.COPILOT.value
    # Removing the recipient removes the delivery risk that held it back.
    result = driver.edit_spec(task.id, {"recipient": None})
    assert result.recommended_mode == AutonomyMode.AUTO.value


def test_editing_does_not_re_run_the_interpreter(session):
    """The human's correction is authoritative; re-interpreting would undo it."""
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    driver.interpreter.llm = FakeLLM([])  # any call would assert-fail
    driver.edit_spec(task.id, {"output_format": "csv"})
    assert TaskSpec.model_validate(repo.get(task.id).spec).output_format == "csv"


def test_a_misspelled_patch_key_is_rejected(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    with pytest.raises(ValidationError):
        driver.edit_spec(task.id, {"outupt_format": "csv"})


def test_editing_a_task_with_no_spec_yet_is_a_conflict(session):
    repo, driver, task = _parked(session, [_spec(recipient="boss")])
    fresh = repo.create(project="default", title="t3", source_message_ids=[])
    with pytest.raises(InvalidTransition):
        driver.edit_spec(fresh.id, {"output_format": "csv"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_driver_actions.py -v`
Expected: FAIL with `AttributeError: 'TaskDriver' object has no attribute 'approve'`.

- [ ] **Step 3: Add the four actions to `TaskDriver`**

Add `from ..domain.states import InvalidTransition, TaskState` (extend the existing import) and these methods, after `advance()` and before the automatic steps:

```python
    # --- human actions ----------------------------------------------------

    def approve(self, task_id: str) -> TaskRow:
        if not self.repo.claim(
            task_id, expected=TaskState.AWAITING_APPROVAL, target=TaskState.EXECUTING
        ):
            raise InvalidTransition(f"task {task_id} is not awaiting approval")
        return self.advance(task_id)

    def reject(self, task_id: str, reason: str = "rejected by the human") -> TaskRow:
        self.repo.record_failure(task_id, reason)
        if not self.repo.claim(
            task_id, expected=TaskState.AWAITING_APPROVAL, target=TaskState.FAILED
        ):
            raise InvalidTransition(f"task {task_id} is not awaiting approval")
        return self.repo.get(task_id)

    def override(self, task_id: str, mode: AutonomyMode | None) -> TaskRow:
        """Pin the mode, or pass None to clear the pin and follow the recommendation."""
        self.repo.set_override(task_id, mode.value if mode is not None else None)
        row = self.repo.get(task_id)
        if row is None:
            raise KeyError(task_id)
        if TaskState(row.state) is TaskState.AWAITING_APPROVAL:
            # Send it back through the gate so the new mode is actually applied.
            # This is what makes flipping the dial to Auto release a parked task.
            self.repo.claim(
                task_id, expected=TaskState.AWAITING_APPROVAL, target=TaskState.INTERPRETED
            )
        return self.advance(task_id)

    def edit_spec(self, task_id: str, patch: dict) -> TaskRow:
        row = self.repo.get(task_id)
        if row is None:
            raise KeyError(task_id)
        if not row.spec:
            raise InvalidTransition(f"task {task_id} has no spec to edit yet")

        # extra="forbid" on TaskSpec turns a misspelled key into a ValidationError
        # here rather than a silently dropped edit. The API maps it to a 422.
        spec = TaskSpec.model_validate({**row.spec, **patch})
        self.repo.save_spec(task_id, spec)

        state = TaskState(row.state)
        if state in (TaskState.AWAITING_APPROVAL, TaskState.NEEDS_CLARIFICATION):
            # Re-enter scoring, NOT interpretation: an edit changes confidence and
            # risk, so the recommendation must be recomputed — but re-running the
            # interpreter would overwrite the human's correction with the model's
            # original reading.
            self.repo.claim(task_id, expected=state, target=TaskState.INTERPRETED)
        return self.advance(task_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_driver_actions.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Run the full suite and commit**

Run: `cd backend && python -m pytest -q`
Expected: PASS.

```bash
git add backend/ley_khaa/orchestrator/driver.py backend/tests/test_driver_actions.py
git commit -m "feat: approve, reject, override, and edit-spec human actions"
```

---

### Task 7: Clarification answers routed as replies

**Files:**
- Modify: `backend/ley_khaa/intake/gateway.py`
- Modify: `backend/ley_khaa/persistence/message_repository.py`
- Modify: `backend/ley_khaa/orchestrator/orchestrator.py`
- Test: `backend/tests/test_task_replies.py`

**Interfaces:**
- Consumes: `MessageRow.reply_to_task_id` (Task 2), `TaskDriver` (Tasks 5–6).
- Produces: `IntakeResult.replied_to_task_id: str | None`; `Orchestrator.ingest()` routes any message carrying `reply_to_task_id` to `Orchestrator._route_reply()` instead of the crystallizer.

**The collision this solves.** The original candidate is `PROMOTED`, which is terminal. If a clarification answer went through stage B, the crystallizer would see an uncovered message and form a *second* candidate for a request that already has a task — a duplicate task for every answer. Routing on `reply_to_task_id` sidesteps candidate formation entirely.

**Why the reply is recorded as stage-A noise.** `record_verdict(relevant=False, topic="task-reply")` looks like a lie — the message is obviously relevant. It is scoped to what the flag actually controls: `relevant` gates whether stage B's window may consider owning the message for a *new candidate*. This message belongs to a task that already exists, so the answer is genuinely "not material to candidate formation". Without it, the message sits unowned in the next window and invites exactly the duplicate the routing prevents.

**Why this is not a dashboard special case.** A Slack thread reply carries `thread_ts` identifying its parent. In Phase 0.5.0 the adapter maps that to the task owning the thread, sets `reply_to_task_id`, and this same branch fires. The dashboard is the first client of a general mechanism, not a bypass.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_task_replies.py
import pytest

from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.domain.states import TaskState
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.orchestrator import Orchestrator
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _orchestrator(session) -> Orchestrator:
    return Orchestrator(
        TaskRepository(session),
        llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        gate=ReadinessGate(0),
    )


def _blocked_task(session):
    """A task parked in needs_clarification: the request names no output format."""
    orchestrator = _orchestrator(session)
    result = orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "compare the holdings against the portfolio"}
    )
    task = TaskRepository(session).get(result.task_ids[0])
    assert task.state == TaskState.NEEDS_CLARIFICATION.value
    return orchestrator, task


def test_an_answer_unblocks_the_task_it_replies_to(session):
    orchestrator, task = _blocked_task(session)
    result = orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    assert result.replied_to_task_id == task.id
    refreshed = TaskRepository(session).get(task.id)
    assert refreshed.state == TaskState.AWAITING_APPROVAL.value
    assert refreshed.spec["output_format"] == "csv"


def test_an_answer_never_spawns_a_second_candidate(session):
    """The original candidate is terminal; stage B would happily start a new one."""
    orchestrator, task = _blocked_task(session)
    before = len(CandidateRepository(session).list_all())
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    assert len(CandidateRepository(session).list_all()) == before


def test_the_answer_is_attached_to_the_task(session):
    orchestrator, task = _blocked_task(session)
    result = orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    assert result.message_id in TaskRepository(session).get(task.id).source_message_ids


def test_the_answer_is_still_a_real_message_in_the_conversation(session):
    """It has to be, or a Slack-sourced answer would be invisible in the thread."""
    orchestrator, task = _blocked_task(session)
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    texts = [m.text for m in MessageRepository(session).list_for_conversation("conv-1")]
    assert "as a csv please" in texts


def test_each_answer_counts_a_clarification_round(session):
    orchestrator, task = _blocked_task(session)
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "still not sure", "reply_to_task_id": task.id}
    )
    assert TaskRepository(session).get(task.id).clarification_rounds == 1


def test_the_loop_gives_up_asking_after_three_rounds(session):
    """A model that keeps reporting the same gap must not ping-pong forever."""
    orchestrator, task = _blocked_task(session)
    for _ in range(4):
        orchestrator.ingest(
            {"conversation_id": "conv-1", "text": "no idea", "reply_to_task_id": task.id}
        )
    refreshed = TaskRepository(session).get(task.id)
    assert refreshed.state == TaskState.AWAITING_APPROVAL.value
    # The gaps stay visible even though we stopped asking about them.
    assert refreshed.spec["missing_fields"]
    assert refreshed.open_question is None


def test_a_reply_to_an_unknown_task_is_rejected(session):
    orchestrator = _orchestrator(session)
    with pytest.raises(KeyError):
        orchestrator.ingest(
            {"conversation_id": "conv-1", "text": "hello", "reply_to_task_id": "nope"}
        )


def test_a_reply_to_a_task_that_is_not_asking_is_attached_but_changes_nothing(session):
    orchestrator, task = _blocked_task(session)
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "as a csv please", "reply_to_task_id": task.id}
    )
    parked = TaskRepository(session).get(task.id)
    assert parked.state == TaskState.AWAITING_APPROVAL.value
    orchestrator.ingest(
        {"conversation_id": "conv-1", "text": "one more thought", "reply_to_task_id": task.id}
    )
    assert TaskRepository(session).get(task.id).state == TaskState.AWAITING_APPROVAL.value
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_task_replies.py -v`
Expected: FAIL — `TypeError: IntakeResult.__init__() got an unexpected keyword argument` or a duplicate candidate, depending on which assertion trips first.

- [ ] **Step 3: Carry `reply_to_task_id` through intake**

In `backend/ley_khaa/intake/gateway.py`, add to the `Message(...)` construction:

```python
            reply_to_task_id=raw.get("reply_to_task_id"),
```

In `backend/ley_khaa/persistence/message_repository.py`, add to the `MessageRow(...)` construction in `add()`:

```python
            reply_to_task_id=message.reply_to_task_id,
```

- [ ] **Step 4: Route replies in the orchestrator**

In `backend/ley_khaa/orchestrator/orchestrator.py`, extend `IntakeResult`:

```python
@dataclass
class IntakeResult:
    message_id: str
    conversation_id: str
    candidates: list[CandidateRow] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    # Set when this message answered an existing task instead of forming a candidate.
    replied_to_task_id: str | None = None
```

Add the branch at the top of `ingest()`, immediately after `row = self.gateway.accept(raw)`:

```python
        if row.reply_to_task_id:
            return self._route_reply(row)
```

And the method:

```python
    def _route_reply(self, row: MessageRow) -> IntakeResult:
        """Attach a reply to the task it answers; never form a candidate from it.

        The task's candidate is PROMOTED, which is terminal. Letting this message
        reach stage B would leave it uncovered in the window and invite a SECOND
        candidate — and so a duplicate task — for a request that already has one.

        This is deliberately the route a Slack thread reply takes in Phase 0.5.0:
        the adapter maps thread_ts to the task owning the thread, sets
        reply_to_task_id, and this branch fires unchanged.
        """
        task = self.repo.get(row.reply_to_task_id)
        if task is None:
            raise KeyError(row.reply_to_task_id)

        # Scoped honesty: `relevant` gates whether stage B may consider owning a
        # message for a NEW candidate. This one belongs to a task that already
        # exists, so it is genuinely not material to candidate formation.
        self.messages.record_verdict(
            row.id, relevant=False, topic="task-reply", confidence=1.0
        )
        self.repo.append_source_messages(task.id, [row.id])

        result = IntakeResult(
            message_id=row.id,
            conversation_id=row.conversation_id,
            replied_to_task_id=task.id,
        )
        if TaskState(task.state) is TaskState.NEEDS_CLARIFICATION:
            self.repo.increment_clarification_rounds(task.id)
            self.repo.set_open_question(task.id, None)
            self.repo.claim(
                task.id,
                expected=TaskState.NEEDS_CLARIFICATION,
                target=TaskState.CLASSIFIED,
            )
            self.driver.advance(task.id)
            result.task_ids.append(task.id)
        # A reply to a task that is not currently asking anything is still worth
        # keeping — it is context for the next interpretation — but it does not
        # restart a task the human is already reviewing.
        return result
```

Add `MessageRow` to the existing `from ..persistence.orm import CandidateRow` import.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_task_replies.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 6: Run the full suite and commit**

Run: `cd backend && python -m pytest -q`
Expected: PASS.

```bash
git add backend/ley_khaa backend/tests/test_task_replies.py
git commit -m "feat: route clarification answers to their task as replies"
```

---

### Task 8: HITL API surface

**Files:**
- Modify: `backend/ley_khaa/api/schemas.py`
- Modify: `backend/ley_khaa/api/app.py`
- Test: `backend/tests/test_api.py` (extend)

**Interfaces:**
- Consumes: everything from Tasks 5–7.
- Produces: `POST /tasks/{id}/approve`, `POST /tasks/{id}/reject`, `POST /tasks/{id}/mode`, `PATCH /tasks/{id}/spec`, `POST /tasks/{id}/answer` — all returning the updated `TaskOut`. `TaskOut` gains the autonomy fields. `InvalidTransition` maps to **409**, a bad spec patch to **422**.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_api.py`:

```python
def _parked_task(client):
    """Drive the demo conversation to a task waiting on a human."""
    client.post("/simulate/messy_universe_check")
    tasks = client.get("/tasks").json()
    assert tasks, "the simulator produced no task"
    return tasks[0]


def test_a_task_exposes_its_spec_and_recommendation(client):
    task = _parked_task(client)
    assert task["state"] == "awaiting_approval"
    assert task["spec"]["operation"] == "set_difference"
    assert task["recommended_mode"] in {"suggest", "copilot", "auto"}
    assert task["effective_mode"] == task["recommended_mode"]
    assert "→" in task["autonomy_reason"]


def test_approve_runs_the_task(client):
    task = _parked_task(client)
    response = client.post(f"/tasks/{task['id']}/approve")
    assert response.status_code == 200
    assert response.json()["state"] == "done"


def test_approving_twice_is_a_409(client):
    task = _parked_task(client)
    client.post(f"/tasks/{task['id']}/approve")
    assert client.post(f"/tasks/{task['id']}/approve").status_code == 409


def test_reject_records_the_reason(client):
    task = _parked_task(client)
    response = client.post(f"/tasks/{task['id']}/reject", json={"reason": "wrong universe"})
    assert response.json()["state"] == "failed"
    assert response.json()["failure_reason"] == "wrong universe"


def test_overriding_the_mode_to_auto_releases_the_task(client):
    task = _parked_task(client)
    response = client.post(f"/tasks/{task['id']}/mode", json={"mode": "auto"})
    assert response.status_code == 200
    assert response.json()["state"] == "done"
    assert response.json()["mode_override"] == "auto"


def test_clearing_the_override_is_accepted(client):
    task = _parked_task(client)
    client.post(f"/tasks/{task['id']}/mode", json={"mode": "suggest"})
    response = client.post(f"/tasks/{task['id']}/mode", json={"mode": None})
    assert response.json()["mode_override"] is None


def test_an_unknown_mode_is_a_422(client):
    task = _parked_task(client)
    assert client.post(f"/tasks/{task['id']}/mode", json={"mode": "yolo"}).status_code == 422


def test_editing_the_spec_rescores(client):
    task = _parked_task(client)
    response = client.patch(f"/tasks/{task['id']}/spec", json={"patch": {"output_format": "csv"}})
    assert response.status_code == 200
    assert response.json()["spec"]["output_format"] == "csv"


def test_a_misspelled_patch_key_is_a_422(client):
    task = _parked_task(client)
    response = client.patch(f"/tasks/{task['id']}/spec", json={"patch": {"outupt_format": "csv"}})
    assert response.status_code == 422


def test_answering_posts_a_real_message_and_advances_the_task(client):
    client.post("/simulate/ambiguous_report_request")
    task = next(t for t in client.get("/tasks").json() if t["state"] == "needs_clarification")
    assert task["open_question"]

    response = client.post(f"/tasks/{task['id']}/answer", json={"text": "as a csv please"})
    assert response.status_code == 200
    assert response.json()["state"] == "awaiting_approval"
    assert response.json()["spec"]["output_format"] == "csv"

    texts = [m["text"] for m in client.get("/conversations/conv-report/messages").json()]
    assert "as a csv please" in texts


def test_a_blank_answer_is_a_422(client):
    client.post("/simulate/ambiguous_report_request")
    task = next(t for t in client.get("/tasks").json() if t["state"] == "needs_clarification")
    assert client.post(f"/tasks/{task['id']}/answer", json={"text": "   "}).status_code == 422


def test_actions_on_an_unknown_task_are_404(client):
    assert client.post("/tasks/nope/approve").status_code == 404
    assert client.post("/tasks/nope/mode", json={"mode": "auto"}).status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_api.py -v`
Expected: FAIL — `KeyError: 'spec'` / 404s on the new routes. (`test_answering_...` also needs the fixture from Task 10; if it is not there yet, expect a 404 from `/simulate/` and add the fixture now — see Task 10 Step 1.)

- [ ] **Step 3: Extend the schemas**

In `backend/ley_khaa/api/schemas.py`:

```python
from typing import Any, Literal
```

```python
class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project: str
    state: str
    title: str
    source_message_ids: list[str]
    created_at: datetime
    updated_at: datetime
    candidate_id: str | None = None
    spec: dict[str, Any] | None = None
    recommended_mode: str | None = None
    mode_override: str | None = None
    # Computed on TaskRow, never stored: the override wins if set, otherwise the
    # recommendation stands.
    effective_mode: str | None = None
    confidence: float | None = None
    risk: float | None = None
    autonomy_reason: str | None = None
    open_question: str | None = None
    failure_reason: str | None = None


class RejectIn(BaseModel):
    reason: str = "rejected by the human"


class ModeIn(BaseModel):
    # None clears the pin and falls back to the engine's recommendation.
    mode: Literal["suggest", "copilot", "auto"] | None = None


class SpecPatchIn(BaseModel):
    patch: dict[str, Any]


class AnswerIn(BaseModel):
    text: str = Field(min_length=1)
    author: str = "human"

    @field_validator("text")
    @classmethod
    def _reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value
```

Also add to `MessageIn`:

```python
    reply_to_task_id: str | None = None
```

- [ ] **Step 4: Add the endpoints and the exception handlers**

In `backend/ley_khaa/api/app.py`, extend the imports:

```python
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..autonomy.modes import AutonomyMode
from ..domain.states import InvalidTransition
from .schemas import (
    AnswerIn, CandidateOut, IntakeOut, MessageIn, MessageOut, ModeIn,
    RejectIn, SpecPatchIn, TaskOut,
)
```

Add the handlers after `app.add_middleware(...)`:

```python
@app.exception_handler(InvalidTransition)
def _handle_invalid_transition(request, exc: InvalidTransition) -> JSONResponse:
    """Acting on a task another tab already moved is a conflict, not a crash."""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValidationError)
def _handle_validation_error(request, exc: ValidationError) -> JSONResponse:
    """A bad edit_spec patch is the caller's mistake, so 422 rather than 500."""
    return JSONResponse(status_code=422, content={"detail": exc.errors(include_url=False)})
```

Add the helpers and endpoints at the end of the file:

```python
def _require_task(session: Session, task_id: str):
    row = TaskRepository(session).get(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")
    return row


@app.post("/tasks/{task_id}/approve", response_model=TaskOut)
def approve_task(task_id: str, session: Session = Depends(get_session)) -> TaskOut:
    _require_task(session, task_id)
    return TaskOut.model_validate(build_orchestrator(session).driver.approve(task_id))


@app.post("/tasks/{task_id}/reject", response_model=TaskOut)
def reject_task(
    task_id: str, body: RejectIn | None = None, session: Session = Depends(get_session)
) -> TaskOut:
    _require_task(session, task_id)
    reason = (body or RejectIn()).reason
    return TaskOut.model_validate(build_orchestrator(session).driver.reject(task_id, reason))


@app.post("/tasks/{task_id}/mode", response_model=TaskOut)
def set_task_mode(
    task_id: str, body: ModeIn, session: Session = Depends(get_session)
) -> TaskOut:
    _require_task(session, task_id)
    mode = AutonomyMode(body.mode) if body.mode is not None else None
    return TaskOut.model_validate(build_orchestrator(session).driver.override(task_id, mode))


@app.patch("/tasks/{task_id}/spec", response_model=TaskOut)
def patch_task_spec(
    task_id: str, body: SpecPatchIn, session: Session = Depends(get_session)
) -> TaskOut:
    _require_task(session, task_id)
    return TaskOut.model_validate(
        build_orchestrator(session).driver.edit_spec(task_id, body.patch)
    )


@app.post("/tasks/{task_id}/answer", response_model=TaskOut)
def answer_task(
    task_id: str, body: AnswerIn, session: Session = Depends(get_session)
) -> TaskOut:
    """Answer a clarification.

    The answer is posted as a real Message carrying reply_to_task_id, so it takes
    exactly the route a Slack thread reply will take — not a private dashboard
    path into the spec.
    """
    task = _require_task(session, task_id)
    sources = MessageRepository(session).get_many(list(task.source_message_ids or []))
    if not sources:
        raise HTTPException(status_code=409, detail="task has no conversation to reply into")

    build_orchestrator(session).ingest(
        {
            "source": "dashboard",
            "client": task.project,
            "conversation_id": sources[0].conversation_id,
            "author": body.author,
            "text": body.text,
            "reply_to_task_id": task_id,
        }
    )
    return TaskOut.model_validate(TaskRepository(session).get(task_id))
```

- [ ] **Step 5: Make the sweeper re-drive stalled tasks**

Replace `_sweep_once` in `app.py`:

```python
def _sweep_once() -> int:
    """One sweep, on its own session. Synchronous: the orchestrator stays sync."""
    session = SessionLocal()
    try:
        orchestrator = build_orchestrator(session)
        promoted = len(orchestrator.sweep())
        # Also re-drive tasks that stalled mid-flight. This is what retries an
        # interpretation that hit a transport failure: the task sits in CLASSIFIED
        # and nothing else would ever pick it up.
        orchestrator.advance_stalled()
        return promoted
    finally:
        session.close()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_api.py -v`
Expected: PASS. If `test_a_task_exposes_its_spec_and_recommendation` reports `state == "needs_clarification"`, the heuristic did not read "Excel file" out of the demo fixture — check `_FORMATS` in `heuristic.py`.

- [ ] **Step 7: Run the full suite and commit**

Run: `cd backend && python -m pytest -q`
Expected: PASS.

```bash
git add backend/ley_khaa/api backend/tests/test_api.py
git commit -m "feat: approve, reject, mode, spec, and answer endpoints"
```

---

### Task 9: Dashboard — the spec, the dial, and the four actions

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/TaskDetail.tsx`
- Modify: `frontend/src/App.tsx`
- Test: `frontend/src/TaskDetail.test.tsx`
- Test: `frontend/src/App.test.tsx` (update — task fixtures need the new fields)

**Interfaces:**
- Consumes: the endpoints from Task 8.
- Produces: `TaskDetail({ task, onChanged })`; `api.ts` exports `approveTask`, `rejectTask`, `setTaskMode`, `answerTask`, `patchTaskSpec`, all returning the updated `Task`.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/TaskDetail.test.tsx
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import TaskDetail from "./TaskDetail";
import type { Task } from "./api";

const task = (overrides: Partial<Task> = {}): Task => ({
  id: "t1",
  project: "default",
  state: "awaiting_approval",
  title: "compare universes",
  spec: {
    intent: "compare two universes",
    inputs: ["bloomberg", "factset"],
    operation: "set_difference",
    output_format: "xlsx",
    recipient: "boss",
    urgency: "normal",
    missing_fields: [],
    source_message_ids: ["m1"],
    certainty: 0.9,
  },
  recommended_mode: "copilot",
  mode_override: null,
  effective_mode: "copilot",
  confidence: 0.9,
  risk: 0.45,
  autonomy_reason: "90% sure, medium risk — it delivers something to someone → I suggest Co-pilot",
  open_question: null,
  failure_reason: null,
  ...overrides,
});

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => task({ state: "done" }) })));
});
afterEach(cleanup);

test("shows the recommendation and its plain-English reason", () => {
  render(<TaskDetail task={task()} onChanged={() => {}} />);
  expect(screen.getByText(/I suggest Co-pilot/)).toBeTruthy();
});

test("shows the interpreted spec", () => {
  render(<TaskDetail task={task()} onChanged={() => {}} />);
  expect(screen.getByText("set_difference")).toBeTruthy();
  expect(screen.getByText("xlsx")).toBeTruthy();
});

test("approving calls the API and reports the change", async () => {
  const onChanged = vi.fn();
  render(<TaskDetail task={task()} onChanged={onChanged} />);
  fireEvent.click(screen.getByText("Approve"));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  expect(String((globalThis.fetch as never as { mock: { calls: string[][] } }).mock.calls[0][0]))
    .toContain("/tasks/t1/approve");
});

test("the dial marks the mode actually in force", () => {
  render(<TaskDetail task={task({ mode_override: "auto", effective_mode: "auto" })} onChanged={() => {}} />);
  expect(screen.getByLabelText("Auto").getAttribute("aria-pressed")).toBe("true");
  expect(screen.getByLabelText("Co-pilot").getAttribute("aria-pressed")).toBe("false");
});

test("a blocked task shows its question and an answer box instead of approval", () => {
  render(
    <TaskDetail
      task={task({ state: "needs_clarification", open_question: "Excel or CSV?" })}
      onChanged={() => {}}
    />,
  );
  expect(screen.getByText(/Excel or CSV\?/)).toBeTruthy();
  expect(screen.getByPlaceholderText(/answer/i)).toBeTruthy();
  expect(screen.queryByText("Approve")).toBeNull();
});

test("a finished task offers no actions", () => {
  render(<TaskDetail task={task({ state: "done" })} onChanged={() => {}} />);
  expect(screen.queryByText("Approve")).toBeNull();
  expect(screen.queryByText("Reject")).toBeNull();
});

test("editing a spec field patches it", async () => {
  const onChanged = vi.fn();
  render(<TaskDetail task={task()} onChanged={onChanged} />);
  const field = screen.getByLabelText("output_format") as HTMLInputElement;
  fireEvent.change(field, { target: { value: "csv" } });
  fireEvent.blur(field);
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npm test`
Expected: FAIL — cannot resolve `./TaskDetail`.

- [ ] **Step 3: Extend `api.ts`**

```ts
export type TaskSpec = {
  intent: string;
  inputs: string[];
  operation: string;
  output_format: string;
  recipient: string | null;
  urgency: string;
  missing_fields: string[];
  source_message_ids: string[];
  certainty: number;
};

export type Task = {
  id: string;
  project: string;
  state: string;
  title: string;
  spec: TaskSpec | null;
  recommended_mode: string | null;
  mode_override: string | null;
  effective_mode: string | null;
  confidence: number | null;
  risk: number | null;
  autonomy_reason: string | null;
  open_question: string | null;
  failure_reason: string | null;
};

async function send(path: string, method: string, body?: unknown): Promise<Task> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${method} ${path} failed: ${res.status}`);
  return res.json();
}

export const approveTask = (id: string) => send(`/tasks/${id}/approve`, "POST");
export const rejectTask = (id: string, reason: string) =>
  send(`/tasks/${id}/reject`, "POST", { reason });
export const setTaskMode = (id: string, mode: string | null) =>
  send(`/tasks/${id}/mode`, "POST", { mode });
export const answerTask = (id: string, text: string) =>
  send(`/tasks/${id}/answer`, "POST", { text });
export const patchTaskSpec = (id: string, patch: Record<string, unknown>) =>
  send(`/tasks/${id}/spec`, "PATCH", { patch });
```

- [ ] **Step 4: Write `TaskDetail.tsx`**

```tsx
import { useState } from "react";
import {
  answerTask, approveTask, patchTaskSpec, rejectTask, setTaskMode,
  type Task,
} from "./api";

const MODES = [
  { value: "suggest", label: "Suggest" },
  { value: "copilot", label: "Co-pilot" },
  { value: "auto", label: "Auto" },
];

// Which spec fields a human can correct in place. The rest are the model's
// reading of the request and are better fixed by answering, not editing.
const EDITABLE = ["operation", "output_format", "recipient", "urgency"] as const;

const pct = (value: number | null) => (value === null ? "—" : `${Math.round(value * 100)}%`);

export default function TaskDetail({
  task,
  onChanged,
}: {
  task: Task;
  onChanged: (task: Task) => void;
}) {
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);

  const run = (work: Promise<Task>) =>
    work.then(onChanged).catch((e) => setError(String(e)));

  const waiting = task.state === "awaiting_approval";
  const blocked = task.state === "needs_clarification";

  return (
    <div className="rounded border border-gray-200 p-4 space-y-4">
      {error && <p className="text-red-600 text-sm">{error}</p>}

      <div>
        <p className="text-sm text-gray-500">
          confidence {pct(task.confidence)} · risk {pct(task.risk)}
        </p>
        {task.autonomy_reason && <p className="mt-1">{task.autonomy_reason}</p>}
      </div>

      <div className="flex gap-2">
        {MODES.map((mode) => (
          <button
            key={mode.value}
            aria-label={mode.label}
            aria-pressed={task.effective_mode === mode.value}
            onClick={() => run(setTaskMode(task.id, mode.value))}
            className={`rounded px-3 py-1 text-sm border ${
              task.effective_mode === mode.value
                ? "border-blue-500 bg-blue-50 text-blue-800"
                : "border-gray-200 text-gray-600"
            }`}
          >
            {mode.label}
          </button>
        ))}
        {task.mode_override && (
          <button
            onClick={() => run(setTaskMode(task.id, null))}
            className="text-sm text-gray-500 underline"
          >
            follow the recommendation
          </button>
        )}
      </div>

      {task.spec && (
        <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
          <dt className="text-gray-500">intent</dt>
          <dd>{task.spec.intent}</dd>
          {EDITABLE.map((field) => (
            <FieldRow key={field} task={task} field={field} onChanged={onChanged} onError={setError} />
          ))}
          {task.spec.missing_fields.length > 0 && (
            <>
              <dt className="text-gray-500">missing</dt>
              <dd className="text-amber-700">{task.spec.missing_fields.join(", ")}</dd>
            </>
          )}
        </dl>
      )}

      {blocked && (
        <div className="space-y-2">
          <p className="text-amber-700">❓ {task.open_question}</p>
          <div className="flex gap-2">
            <input
              className="flex-1 rounded border border-gray-200 px-2 py-1 text-sm"
              placeholder="Type your answer…"
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
            />
            <button
              className="rounded bg-blue-600 px-3 py-1 text-sm text-white"
              onClick={() => run(answerTask(task.id, answer)).then(() => setAnswer(""))}
            >
              Answer
            </button>
          </div>
        </div>
      )}

      {waiting && (
        <div className="flex gap-2">
          <button
            className="rounded bg-emerald-600 px-3 py-1 text-sm text-white"
            onClick={() => run(approveTask(task.id))}
          >
            Approve
          </button>
          <button
            className="rounded border border-gray-300 px-3 py-1 text-sm"
            onClick={() => run(rejectTask(task.id, "rejected from the dashboard"))}
          >
            Reject
          </button>
        </div>
      )}
    </div>
  );
}

function FieldRow({
  task, field, onChanged, onError,
}: {
  task: Task;
  field: string;
  onChanged: (task: Task) => void;
  onError: (message: string) => void;
}) {
  const current = String((task.spec as Record<string, unknown>)[field] ?? "");
  const [value, setValue] = useState(current);
  const editable = task.state === "awaiting_approval" || task.state === "needs_clarification";

  if (!editable) {
    return (
      <>
        <dt className="text-gray-500">{field}</dt>
        <dd>{current || "—"}</dd>
      </>
    );
  }
  return (
    <>
      <dt className="text-gray-500">
        <label htmlFor={`${task.id}-${field}`}>{field}</label>
      </dt>
      <dd>
        <input
          id={`${task.id}-${field}`}
          aria-label={field}
          className="w-full rounded border border-gray-200 px-2 py-0.5"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          // Patch on blur, not on every keystroke: each patch re-scores the task
          // on the server, and doing that per character is both wasteful and
          // visibly jumpy.
          onBlur={() => {
            if (value === current) return;
            patchTaskSpec(task.id, { [field]: value || null })
              .then(onChanged)
              .catch((e) => onError(String(e)));
          }}
        />
      </dd>
    </>
  );
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd frontend && npm test`
Expected: `TaskDetail.test.tsx` PASS, 7 tests. `App.test.tsx` will now fail — its task fixture lacks the new fields.

- [ ] **Step 6: Wire the detail view into `App.tsx`**

Make each task row expandable. Add `const [openTask, setOpenTask] = useState<string | null>(null);` and replace the task `<li>` body:

```tsx
        {tasks.map((t) => (
          <li key={t.id} className="rounded border border-gray-200 p-3">
            <button
              className="flex w-full justify-between text-left"
              onClick={() => setOpenTask(openTask === t.id ? null : t.id)}
            >
              <span>{t.title}</span>
              <span className="text-sm text-gray-500">
                {t.project} · {t.state}
                {t.effective_mode ? ` · ${t.effective_mode}` : ""}
              </span>
            </button>
            {openTask === t.id && (
              <div className="mt-3">
                <TaskDetail
                  task={t}
                  onChanged={(updated) =>
                    setTasks((prev) => prev.map((x) => (x.id === updated.id ? updated : x)))
                  }
                />
              </div>
            )}
          </li>
        ))}
```

Update the fixture in `App.test.tsx` to include the new fields (copy the `task()` helper shape from `TaskDetail.test.tsx`), and add:

```tsx
test("opening a task shows its recommendation", async () => {
  render(<App />);
  await waitFor(() => expect(screen.getByText("compare universes")).toBeTruthy());
  fireEvent.click(screen.getByText("compare universes"));
  expect(screen.getByText(/I suggest Co-pilot/)).toBeTruthy();
});
```

- [ ] **Step 7: Run the frontend suite and commit**

Run: `cd frontend && npm test`
Expected: PASS, all files.

```bash
git add frontend/src
git commit -m "feat: dashboard task detail with the autonomy dial and hitl actions"
```

---

### Task 10: End-to-end proof of the phase

**Files:**
- Create: `backend/ley_khaa/fixtures/conversations/ambiguous_report_request.json`
- Test: `backend/tests/test_end_to_end.py`

**Interfaces:**
- Consumes: everything above. Adds no production interfaces.

**These two tests are the phase.** One proves the dial changes behaviour; the other proves the clarification loop closes. If both pass against the offline heuristic, `docker compose up` on a fresh clone with no API key demonstrates the whole phase.

- [ ] **Step 1: Create the gap fixture**

```json
{
  "conversation_id": "conv-report",
  "client": "demo",
  "messages": [
    {"author": "alice", "text": "hey quick one"},
    {"author": "alice", "text": "can you compare the holdings against the portfolio for me"},
    {"author": "bob", "text": "haha sure thing"},
    {"author": "alice", "text": "thanks!"}
  ]
}
```

The gap is deliberate and singular: the request names its inputs and its operation but never says what format the answer should come back in, so the interpreter reports exactly one missing field.

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_end_to_end.py
from ley_khaa.domain.states import TaskState


def test_a_messy_conversation_parks_for_a_human_and_the_dial_releases_it(client):
    """Headline #2: the autonomy dial changes what the system does, not just what it says."""
    client.post("/simulate/messy_universe_check")

    tasks = client.get("/tasks").json()
    assert len(tasks) == 1, "the noisy conversation should yield exactly one task"
    task = tasks[0]

    # It stopped on its own and can explain why.
    assert task["state"] == TaskState.AWAITING_APPROVAL.value
    assert task["spec"]["operation"] == "set_difference"
    assert task["spec"]["output_format"] == "xlsx"
    assert task["recommended_mode"] != "auto"
    assert "→" in task["autonomy_reason"]

    # One click on the dial, and the same task runs without further approval.
    released = client.post(f"/tasks/{task['id']}/mode", json={"mode": "auto"}).json()
    assert released["state"] == TaskState.DONE.value
    assert released["mode_override"] == "auto"


def test_a_gap_is_asked_about_answered_in_the_conversation_and_closed(client):
    """The clarification loop, over the same message path a Slack reply will take."""
    client.post("/simulate/ambiguous_report_request")

    task = next(
        t for t in client.get("/tasks").json()
        if t["state"] == TaskState.NEEDS_CLARIFICATION.value
    )
    assert task["spec"]["missing_fields"] == ["output_format"]
    assert "output_format" in task["open_question"]

    answered = client.post(f"/tasks/{task['id']}/answer", json={"text": "as a csv please"}).json()
    assert answered["state"] == TaskState.AWAITING_APPROVAL.value
    assert answered["spec"]["output_format"] == "csv"
    assert answered["open_question"] is None

    # The answer is a real message in the conversation, not a private side channel.
    texts = [m["text"] for m in client.get("/conversations/conv-report/messages").json()]
    assert "as a csv please" in texts

    # And no duplicate candidate was formed for a request that already had a task.
    keys = {c["candidate_key"] for c in client.get("/candidates").json()}
    assert len(keys) == len(client.get("/candidates").json())

    assert client.post(f"/tasks/{task['id']}/approve").json()["state"] == TaskState.DONE.value
```

- [ ] **Step 3: Run test to verify it fails, then passes**

Run: `cd backend && python -m pytest tests/test_end_to_end.py -v`
Expected: FAIL first (404 from `/simulate/ambiguous_report_request` if the fixture is missing), then PASS once the fixture exists. No production code should need to change — if it does, an earlier task is incomplete; fix it there, not here.

- [ ] **Step 4: Verify the offline path by hand**

```bash
cd backend && DATABASE_URL="sqlite:///./e2e-check.db" LEY_KHAA_DEBOUNCE_SECONDS=0 \
  python -c "
from ley_khaa.db import run_migrations
run_migrations()
print('migrations ok')
"
rm backend/e2e-check.db
```

Expected: `migrations ok`, no traceback, with **no `ANTHROPIC_API_KEY` set**.

- [ ] **Step 5: Commit**

```bash
git add backend/ley_khaa/fixtures backend/tests/test_end_to_end.py
git commit -m "test: end-to-end proof of the autonomy dial and clarification loop"
```

---

### Task 11: Release 0.3.0

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `CHANGELOG.md`
- Modify: `README.md`

- [ ] **Step 1: Bump the version**

In `backend/pyproject.toml`: `version = "0.3.0"`.

- [ ] **Step 2: Update the README**

Change the phase table row and the headline paragraph:

```markdown
| 2 | `v0.3.0` | Interpreter + **Autonomy engine** + human-in-the-loop | ✅ shipped |
| 3 | `v0.4.0` | Synthesis-first executor, validator, Output Bundle | 📋 planned |
```

Replace the "v0.2.0 — Intake and Task Crystallizer" paragraph with a v0.3.0 one covering: a crystallized request is interpreted into a validated spec; the autonomy engine scores confidence and risk and recommends a mode with a readable reason; the task parks for approval unless the effective mode is Auto; a human can approve, reject, re-dial, edit the spec, or answer a question, and the answer re-enters as a real message. Document the new endpoints and state plainly that **the executor is still a stub — real execution is v0.4.0**, and that the offline `HeuristicLLM` deliberately never scores high enough to earn Auto on its own.

- [ ] **Step 3: Write the CHANGELOG entry**

```markdown
## [0.3.0] — <the date you run this step, ISO format>

> **Upgrading from 0.2.0:** this release introduces Alembic. A database created by
> 0.2.0 has the tables but no `alembic_version`, so the app stamps it at the
> baseline automatically on first start and then applies the new columns. No
> manual drop is needed — and this is the last release that will ever ask.

### Added
- Interpreter (§5.5): a crystallized request becomes a validated `TaskSpec`
  (`intent · inputs · operation · output_format · recipient · urgency · missing_fields ·
  source_message_ids · certainty`), with one re-prompt on malformed output and an
  escalation to the human after that.
- Autonomy engine (§5.7): a deterministic policy over confidence (interpreter
  certainty, missing fields, how settled the conversation was) and risk
  (irreversibility, money, urgency) recommends Suggest / Co-pilot / Auto with a
  plain-English reason. No LLM call, identical online and offline.
- `TaskDriver`: one re-entrant `advance()` owning the whole automatic path, so every
  entry point into a task shares the same definition of what happens next.
- Human-in-the-loop (§5.8): approve, reject, override the mode, edit the spec inline,
  and answer a clarification — `POST /tasks/{id}/approve|reject|mode|answer` and
  `PATCH /tasks/{id}/spec`.
- Clarification answers re-enter as real messages carrying `reply_to_task_id`, routed
  straight to the task they answer. This is the same path a Slack thread reply will
  take, and it stops an answer spawning a duplicate candidate.
- Alembic migrations, with a test that fails if the models and migrations disagree.
- A second golden conversation with a deliberate gap, and end-to-end tests for both
  the dial and the clarification loop.

### Changed
- A promoted task no longer races to `done`. It is interpreted, scored, and then either
  parks at `awaiting_approval` or — when the effective mode is Auto — runs through.
- The background sweeper also re-drives stalled tasks, which is how an interpretation
  that hit a transport failure gets retried.
- `InvalidTransition` now surfaces as **409** rather than a 500; a malformed spec patch
  as **422**.
- The task state machine gained the four edges the clarification loop needs.

### Known limitations
- **Suggest and Co-pilot behave identically**: both park at the single approval gate.
  They diverge in 0.4.0, when the executor has mid-run checkpoints.
- **Execution is still a stub.** `executing → validating → done` does no real work.
- The offline `HeuristicLLM` reports a deliberately mediocre certainty, so a no-API-key
  clone never reaches Auto on its own — keyword matching must not run tasks unattended.
```

- [ ] **Step 4: Verify everything green**

```bash
cd backend && python -m pytest -q && cd ../frontend && npm test
```

Expected: PASS on both. Do not proceed on a failure or a skipped test.

- [ ] **Step 5: Commit and tag**

```bash
git add backend/pyproject.toml CHANGELOG.md README.md
git commit -m "chore: release 0.3.0 — interpreter, autonomy dial, and human-in-the-loop"
```

Tagging and the GitHub release happen on `main` after the PR merges, not on the branch.

---

## Self-Review

**Spec coverage.**

| Spec requirement | Task |
|---|---|
| §5.5 Interpreter → validated `TaskSpec` | 2 (model), 3 (engine) |
| §5.5 malformed → re-prompt, then escalate to HITL | 3, 5 |
| §5.7 confidence from certainty / missing fields / readiness | 4 |
| §5.7 risk from irreversibility / urgency / money | 4 |
| §5.7 recommend Suggest/Co-pilot/Auto with a plain-English reason | 4 |
| §5.7 human can always override | 6, 8, 9 |
| §5.8 approve | 6, 8, 9 |
| §5.8 edit the spec | 6, 8, 9 |
| §5.8 answer a clarification | 7, 8, 9 |
| §5.9 `awaiting_approval` and `needs_clarification` actually entered | 2, 5 |
| §5.9 state persisted so tasks survive pause/resume | 2 |
| §5.9 amendment detector | **deferred to Phase 4** (decision 5) |
| §5.15 dashboard surfaces the above | 9 |

**Deliberately out of scope**, each with a stated reason: the real executor (§5.10, Phase 3), the Output Bundle (§5.11, Phase 3), project routing and channel adapters (§5.1/§5.4, Phase 4), the amendment detector (§5.9, Phase 4), vision extraction (§5.2, unscheduled), and Ollama fallback (§5.13, unscheduled).

**Type consistency checks performed.**

- `TaskSpec` field names are identical in Tasks 2, 3, 4, 5, 6, 9 and in the heuristic rule.
- Modes are stored as `AutonomyMode(...).value` strings everywhere a column or JSON payload is involved, and as the enum inside `autonomy/` and `driver.py`. `TaskDriver.override` takes the enum; `set_task_mode` in the API converts at the boundary.
- `TaskRepository.claim(task_id, *, expected, target)` has one signature, used identically in `driver.py` and `orchestrator.py`.
- `recommend(spec, *, candidate_missing_fields=None)` is called with the keyword in `driver._gate` and in every test.
- `effective_mode` is a `TaskRow` property (Task 2), surfaced by `TaskOut` via `from_attributes` (Task 8), and read as `task.effective_mode` in the frontend (Task 9).
- `MessageRepository.get_many` is introduced in Task 3 and reused in Task 8's `answer_task`.

**Known ordering constraint:** Task 8's `test_answering_posts_a_real_message_and_advances_the_task` depends on the fixture created in Task 10 Step 1. Either create that fixture early during Task 8 or expect that single test to fail until Task 10; it is called out inline in both places.
