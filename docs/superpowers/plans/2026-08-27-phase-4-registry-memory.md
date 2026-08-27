# Phase 4 (v0.5.0) — Workflow Registry and Task Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the two caches that make the system stop re-deriving work it has already proven — task memory short-circuits the interpreter, the workflow registry short-circuits synthesis — so the same request asked twice is served the second time with zero LLM calls.

**Architecture:** Two new packages (`registry/`, `memory/`) that each expose a matcher with the same shape: a deterministic fingerprint first (free, works offline), one cheap Haiku call on a miss behind a confidence gate, and "no match" as an always-legal answer that costs only a fall-through to the existing path. Both caches are written to only by proven runs. Neither may raise into the driver, and neither changes the state machine.

**Tech Stack:** Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy · Alembic · pytest · React + Vite + Tailwind + vitest.

**Spec:** `docs/superpowers/specs/2026-08-27-phase-4-registry-memory-design.md` — read it before Task 1. Section 2 (Decisions) is settled and not open for re-litigation during execution.

## Global Constraints

- **Never send `thinking` or `output_config.effort` to Haiku 4.5** — it predates adaptive thinking and returns 400. Only `claude-sonnet-5` and `claude-opus-5` accept it. `ModelChoice.supports_thinking` already encodes this; do not bypass it.
- **`HeuristicLLM` must answer every new `output_format` this phase adds.** Tests run with `LEY_KHAA_LLM=heuristic` (`backend/tests/conftest.py`), and `docker compose up` must work with **no `ANTHROPIC_API_KEY`**. An unhandled format raises `NotImplementedError` and takes the whole offline path down.
- **`Settings` in `config.py` is `@dataclass(frozen=True)`** — a Phase 0 invariant. Tests must never unfreeze it; pin settings by rebinding the module-level `settings` name to a `dataclasses.replace(...)` copy.
- **Every path read out of a bundle goes through `_contained()`** (`backend/ley_khaa/api/app.py:315`). The workspace is written by untrusted generator code.
- **The manifest records what actually happened**, never what was intended or would have been chosen. On a cached run no model wrote the script, so `models.synthesis` is `null`.
- **A cache that fails must cost only the work it was trying to save.** Every matcher swallows its own exceptions at the boundary and returns "no match".
- **Confidence gate for both matchers: `>= 0.8`.** One constant per package, pinned by a test.
- **Familiarity bonus: `+0.05` per remembered run, capped at `+0.15`** — strictly less than `_MISSING_FIELD_PENALTY` (0.2).
- **Run backend tests with `TMPDIR` under `$HOME`** on this machine (Colima). `TMPDIR="$HOME/.leykhaa-tmp" python -m pytest -q` from `backend/`.
- **Conventional Commits**, one commit per task, `main` stays green.

## File Structure

**Created:**

| path | responsibility |
|---|---|
| `backend/ley_khaa/registry/__init__.py` | package marker |
| `backend/ley_khaa/registry/models.py` | `InputRole`, `RegistryDecision`, `Match` — the types the registry passes around |
| `backend/ley_khaa/registry/fingerprint.py` | operation normalization, format agreement, stage-1 candidates |
| `backend/ley_khaa/registry/binder.py` | roles → this run's files, or refusal |
| `backend/ley_khaa/registry/matcher.py` | the two-stage matcher |
| `backend/ley_khaa/registry/promote.py` | a passing bundle → a `WorkflowRow` |
| `backend/ley_khaa/registry/seeds/__init__.py` | `SEEDS` + `ensure_seed_workflows(session)` |
| `backend/ley_khaa/registry/seeds/set_difference.py` | hand-written seed workflow source |
| `backend/ley_khaa/registry/seeds/summary_stats.py` | hand-written seed workflow source |
| `backend/ley_khaa/memory/__init__.py` | package marker |
| `backend/ley_khaa/memory/models.py` | `MemoryDecision` |
| `backend/ley_khaa/memory/fingerprint.py` | `STOPWORDS`, `request_fingerprint()` |
| `backend/ley_khaa/memory/matcher.py` | the two-stage recall |
| `backend/ley_khaa/persistence/workflow_repository.py` | `WorkflowRepository` |
| `backend/ley_khaa/persistence/memory_repository.py` | `MemoryRepository` |
| `backend/ley_khaa/alembic/versions/0004_registry_memory.py` | two tables + two `tasks` columns |
| `frontend/src/Registry.tsx` | the Registry page |

**Modified:** `persistence/orm.py` (two rows, two `TaskRow` columns) · `persistence/repository.py` (`save_memory_hit`) · `executor/workspace.py` (`write_params`) · `executor/synthesizer.py` (prompt) · `executor/runner.py` (lane selection) · `llm/router.py` (two stages) · `llm/heuristic.py` (params preamble, two null decisions) · `orchestrator/orchestrator.py` + `orchestrator/driver.py` (thread the repos, recall, record) · `autonomy/engine.py` (familiarity) · `api/app.py` + `api/schemas.py` (promote + registry routes) · `frontend/src/api.ts`, `TaskDetail.tsx`, `App.tsx` · `README.md`, `CHANGELOG.md`.

## Departures from the spec — read these first

The spec was written before this scan of the code. Four things it says are adjusted here, deliberately:

1. **ORM rows live in `persistence/orm.py`, repositories in `persistence/`.** The spec sketched `registry/models.py` and `memory/models.py` holding the rows. Every existing row in this codebase lives in `orm.py` and every repository in `persistence/`, and the migration drift guard walks `Base.metadata`. Following the house pattern; the packages' `models.py` hold the pydantic types instead.
2. **Seed workflows are installed at startup, not by the migration.** The spec §3.7 says the migration seeds rows. Migrations that import application code rot when the code moves. `api/app.py:92` already seeds the demo conversation at startup, so `ensure_seed_workflows(session)` follows that precedent and is idempotent.
3. **`.xlsx` is not byte-reproducible, so the phase's headline test uses CSV.** `runner.py` already states this in the manifest: *"cell values for .xlsx; bytes for csv, json and text"* — an `.xlsx` is a zip embedding timestamps. The spec's DoD says "byte-identical output". The end-to-end test in Task 16 therefore asks for a **csv** deliverable, where byte-identity is real. Testing it on `.xlsx` would have failed for a reason that has nothing to do with caching.
4. **`workflows=` and `memories=` are optional constructor arguments** on `Orchestrator`, `TaskDriver`, and `ExecutionRunner`, defaulting to `None` (feature off). Dozens of existing tests construct these directly; optional keeps them green, and it makes "the cache is not available" a first-class state rather than a crash.

---

### Task 1: The `params.json` input-binding contract

Every synthesized script stops hardcoding filenames. This must land first — promotion, binding, and the whole registry lane depend on it.

**Files:**
- Modify: `backend/ley_khaa/executor/workspace.py` (add `write_params`)
- Modify: `backend/ley_khaa/executor/synthesizer.py` (`SYSTEM`, `_task_block`)
- Modify: `backend/ley_khaa/llm/heuristic.py` (`_PREAMBLE`, `_synthesize`)
- Modify: `backend/ley_khaa/executor/runner.py` (call `write_params`)
- Test: `backend/tests/test_workspace.py`, `backend/tests/test_synthesizer.py`

**Interfaces:**
- Consumes: `Workspace` (`executor/workspace.py`), `ResolvedInput` (`.name`, `.filename`, `.content`, `.source`, `.sha256`), `catalog.CATALOG_SEED`.
- Produces: `Workspace.write_params(*, inputs: dict[str, str], output: str, seed: int) -> Path` — writes `inputs/params.json`. Every later task binds through this.

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_workspace.py`:

```python
def test_params_json_lands_in_inputs_so_the_tamper_check_covers_it(tmp_path):
    """params.json is an input like any other.

    It sits in inputs/, so input_hashes() covers it and a script that rewrites
    its own binding mid-run is caught by the existing check rather than being a
    new hole this contract opens.
    """
    workspace = Workspace.create(tmp_path, "t1")
    path = workspace.write_params(
        inputs={"bloomberg": "bloomberg_universe.csv"},
        output="deliverable/output.csv",
        seed=20260825,
    )

    assert path == workspace.inputs_dir / "params.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "inputs": {"bloomberg": "bloomberg_universe.csv"},
        "output": "deliverable/output.csv",
        "seed": 20260825,
    }
    assert "params.json" in workspace.input_hashes()
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_workspace.py::test_params_json_lands_in_inputs_so_the_tamper_check_covers_it -v
```

Expected: `AttributeError: 'Workspace' object has no attribute 'write_params'`.

- [ ] **Step 3: Implement `write_params`**

In `backend/ley_khaa/executor/workspace.py`, after `write_inputs`:

```python
    def write_params(self, *, inputs: dict[str, str], output: str, seed: int) -> Path:
        """The binding a generator reads instead of hardcoding filenames.

        Keys are roles: on a synthesized run they are the spec's own input
        names; on a cached run they are the promoted workflow's role names bound
        to THIS run's files. That is what lets a frozen script run unchanged
        against different data — the script reads the name it was born with.

        Written into inputs/ deliberately: it travels with the bundle, so
        re-running generator/run.sh reproduces the binding too, and the existing
        input_hashes() tamper check covers it for free.
        """
        path = self.inputs_dir / "params.json"
        path.write_text(
            json.dumps({"inputs": inputs, "output": output, "seed": seed}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path
```

- [ ] **Step 4: Run it and watch it pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_workspace.py -v
```

Expected: PASS.

- [ ] **Step 5: Write the failing prompt test**

In `backend/tests/test_synthesizer.py`:

```python
def test_the_prompt_forbids_hardcoded_filenames(fake_llm):
    """A script that hardcodes a filename cannot be promoted.

    Promotion is a pure copy of proven source, so the contract that makes a
    script reusable has to be in the prompt that writes it, not bolted on
    afterwards.
    """
    assert "params.json" in SYSTEM
    assert "hardcode" in SYSTEM.lower()

    Synthesizer(fake_llm).synthesize(_spec(), [_resolved("bloomberg", "b.csv")])
    user = fake_llm.calls[0].user

    assert "inputs/params.json" in user
    assert '"bloomberg"' in user
```

Use the module's existing `_spec()` / `_resolved()` helpers and `fake_llm` fixture; if `test_synthesizer.py` has no such helpers, build a `TaskSpec(intent="i", inputs=["bloomberg"], operation="set_difference", output_format="csv", certainty=0.9)` and a `ResolvedInput(name="bloomberg", filename="b.csv", content="ticker\nAAA\n", source="catalog")` inline.

- [ ] **Step 6: Run it and watch it fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_synthesizer.py -v
```

Expected: FAIL — `params.json` is in neither `SYSTEM` nor the rendered prompt.

- [ ] **Step 7: Change the synthesis contract**

In `backend/ley_khaa/executor/synthesizer.py`, add these two bullets to `SYSTEM` immediately after the "Working directory contains inputs/" line:

```
- Read every input path and the output path from inputs/params.json. It looks like
  {"inputs": {"<role>": "<filename in inputs/>"}, "output": "deliverable/<name>", "seed": <int>}.
  Open inputs/ + the filename you find there. NEVER hardcode a filename or an output path:
  a script that hardcodes them cannot be re-run against next week's data.
- Use params["seed"] for anything that would otherwise be random.
```

and extend `_task_block` so the model sees the exact binding it will get:

```python
def _task_block(spec: TaskSpec, resolved: list[ResolvedInput]) -> str:
    target = f"deliverable/{deliverable_filename(spec.output_format)}"
    previews = "\n\n".join(_preview(item) for item in resolved)
    params = json.dumps(
        {
            "inputs": {item.name: item.filename for item in resolved},
            "output": target,
            "seed": catalog.CATALOG_SEED,
        },
        indent=2,
        sort_keys=True,
    )
    return (
        f"## Task\n"
        f"intent: {spec.intent}\n"
        f"operation: {spec.operation}\n"
        f"output_format: {spec.output_format}\n"
        f"write the result to: {target}\n"
        f"\n## inputs/params.json (read your paths from this file)\n{params}\n"
        f"\n## Inputs (first {_PREVIEW_LINES} lines of each)\n{previews}\n"
    )
```

Add `import json` and `from . import catalog` at the top of the module.

- [ ] **Step 8: Make the offline stand-in honour the same contract**

In `backend/ley_khaa/llm/heuristic.py`, change `_PREAMBLE`'s header so the canned scripts read the binding too. Replace `import csv` with:

```python
import csv
import json

_params = json.load(open("inputs/params.json", encoding="utf-8"))
# Ordered: params.json is written in spec-input order, and dicts preserve it.
INPUTS = list(_params["inputs"].values())
TARGET = _params["output"]
```

Then in `_synthesize`, delete the line that injects the constants:

```python
        source = _PREAMBLE + f"INPUTS = {inputs!r}\nTARGET = {target!r}\n" + body + substitution
```

becomes:

```python
        # INPUTS and TARGET now come from inputs/params.json, set in _PREAMBLE.
        # The substitution below still overrides TARGET when the requested
        # format is one the offline stand-in cannot honestly write.
        source = _PREAMBLE + substitution_target + body + substitution
```

where `substitution_target` is `""` normally and `f"TARGET = {target!r}\n"` when the format substitution kicked in (the existing `if not target.endswith((".xlsx", ".csv"))` branch). Keep the existing `inputs` parsing: it is still what decides between `_BODIES` and `_INVENTORY` when there are no inputs.

- [ ] **Step 9: Write the binding in the runner**

In `backend/ley_khaa/executor/runner.py`, in `run()`, immediately after `workspace.write_inputs(resolved)`:

```python
        workspace.write_params(
            inputs={item.name: item.filename for item in resolved},
            output=f"deliverable/{deliverable_filename(spec.output_format)}",
            seed=catalog.CATALOG_SEED,
        )
        input_hashes = workspace.input_hashes()
```

(`input_hashes` must be computed **after** `write_params`, or the tamper check will see params.json appear mid-run and report the inputs as modified.) Add `from .formats import deliverable_filename` to the imports.

- [ ] **Step 10: Run the whole backend suite**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest -q
```

Expected: all pass. The golden end-to-end test exercises the offline canned script through a real sandbox — if it fails with `FileNotFoundError: inputs/params.json`, the runner is writing params after computing hashes, or not at all.

- [ ] **Step 11: Commit**

```bash
git add backend/ley_khaa/executor backend/ley_khaa/llm/heuristic.py backend/tests
git commit -m "feat(executor): read input and output paths from inputs/params.json

A generator that hardcodes a filename cannot be re-run against next week's
data, which makes it unpromotable. Every synthesized script now reads its
binding from inputs/params.json, so promoting a proven script is a pure copy
of the source that was proven rather than a rewrite of it."
```

---

### Task 2: Persistence — workflows, task memory, and two task columns

**Files:**
- Modify: `backend/ley_khaa/persistence/orm.py`
- Create: `backend/ley_khaa/alembic/versions/0004_registry_memory.py`
- Test: `backend/tests/test_migrations.py` (existing drift guard covers the new models), `backend/tests/test_orm_registry_memory.py`

**Interfaces:**
- Produces: `WorkflowRow`, `MemoryRow` (importable from `ley_khaa.persistence.orm`), and `TaskRow.remembered_from_task_id: str | None`, `TaskRow.familiarity: int`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_orm_registry_memory.py`:

```python
from ley_khaa.persistence.orm import MemoryRow, TaskRow, WorkflowRow


def test_a_workflow_row_carries_its_provenance_and_its_hash(session):
    """A promoted capability has to be traceable back to the run that proved it.

    Without promoted_from_task_id there is no way to answer "where did this
    code come from?", which is the question the whole bundle design exists to
    answer.
    """
    row = WorkflowRow(
        id="w1",
        name="set_difference",
        description="rows in A missing from B",
        operation_aliases=["set_difference"],
        output_format="csv",
        inputs=[{"role": "left", "suffixes": [".csv"]}],
        source="print('hi')",
        source_sha256="abc",
        origin="promoted",
        promoted_from_task_id="t1",
    )
    session.add(row)
    session.commit()

    stored = session.get(WorkflowRow, "w1")
    assert stored.origin == "promoted"
    assert stored.promoted_from_task_id == "t1"
    assert stored.runs_ok == 0 and stored.runs_failed == 0
    assert stored.quarantined is False


def test_a_memory_row_is_scoped_to_a_project(session):
    """Memory must never leak a spec from one project into another."""
    session.add(
        MemoryRow(
            id="m1",
            project="acme",
            fingerprint="deadbeef",
            intent="compare the universes",
            spec={"operation": "set_difference"},
            source_task_id="t1",
        )
    )
    session.commit()

    stored = session.get(MemoryRow, "m1")
    assert stored.project == "acme"
    assert stored.times_seen == 1


def test_a_task_remembers_where_its_spec_came_from(session):
    """familiarity feeds the dial; remembered_from_task_id feeds the dashboard."""
    session.add(TaskRow(id="t2", state="received"))
    session.commit()

    stored = session.get(TaskRow, "t2")
    assert stored.familiarity == 0
    assert stored.remembered_from_task_id is None
```

- [ ] **Step 2: Run it and watch it fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_orm_registry_memory.py -v
```

Expected: `ImportError: cannot import name 'WorkflowRow'`.

- [ ] **Step 3: Add the rows**

In `backend/ley_khaa/persistence/orm.py`, add to `TaskRow` (beside the Phase 3 columns):

```python
    # Set when a spec came from memory rather than the interpreter. familiarity
    # is the remembered times_seen and feeds the autonomy dial; the task id is
    # what the dashboard links back to.
    remembered_from_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    familiarity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
```

and append the two new models:

```python
class WorkflowRow(Base):
    """A promoted, proven workflow — the registry's learned cache (spec §5.6).

    `source` is frozen: it is byte-for-byte the script that passed validation in
    the bundle named by promoted_from_task_id. Nothing rewrites it, which is why
    source_sha256 is meaningful.
    """

    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, index=True)
    description: Mapped[str] = mapped_column(String, default="")
    # Normalized operation strings that match this workflow. Grows by one every
    # time the model matcher finds a phrasing that then passes validation.
    operation_aliases: Mapped[list] = mapped_column(JSON, default=list)
    output_format: Mapped[str] = mapped_column(String)
    # [{"role": "left", "suffixes": [".csv"]}], in the order the script expects.
    inputs: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String)
    source_sha256: Mapped[str] = mapped_column(String)
    origin: Mapped[str] = mapped_column(String, default="promoted")  # seed | promoted
    promoted_from_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    runs_ok: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    runs_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set by a failed cached run. Blocks matching until a human clears it: a
    # workflow that just produced a wrong answer must not be handed the next
    # matching request as though nothing happened.
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class MemoryRow(Base):
    """A remembered request → the spec that satisfied it (spec §5.14).

    Written only for tasks that reached DONE with a passing verdict: the same
    "proven before it is cached" rule promotion follows.
    """

    __tablename__ = "task_memory"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    project: Mapped[str] = mapped_column(String, index=True, default="default")
    fingerprint: Mapped[str] = mapped_column(String, index=True)
    intent: Mapped[str] = mapped_column(String, default="")
    spec: Mapped[dict] = mapped_column(JSON)
    source_task_id: Mapped[str] = mapped_column(String)
    times_seen: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
```

Ensure `Boolean` and `Integer` are in the `sqlalchemy` import at the top of the file.

- [ ] **Step 4: Run it and watch it pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_orm_registry_memory.py -v
```

Expected: PASS.

- [ ] **Step 5: Watch the drift guard fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_migrations.py -v
```

Expected: FAIL — the models now describe tables no migration creates. This is the guard doing its job.

- [ ] **Step 6: Write the migration**

Create `backend/ley_khaa/alembic/versions/0004_registry_memory.py`:

```python
"""phase 4: workflow registry and task memory

Revision ID: 0004_registry_memory
Revises: 0003_executor
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_registry_memory"
down_revision = "0003_executor"
branch_labels = None
depends_on = None

_TASK_COLUMNS = [
    sa.Column("remembered_from_task_id", sa.String(), nullable=True),
    sa.Column("familiarity", sa.Integer(), nullable=False, server_default="0"),
]


def upgrade() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.String(), nullable=False, server_default=""),
        sa.Column("operation_aliases", sa.JSON(), nullable=False),
        sa.Column("output_format", sa.String(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_sha256", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False, server_default="promoted"),
        sa.Column("promoted_from_task_id", sa.String(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("runs_ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("runs_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_index("ix_workflows_name", "workflows", ["name"], unique=True)

    op.create_table(
        "task_memory",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project", sa.String(), nullable=False, server_default="default"),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("intent", sa.String(), nullable=False, server_default=""),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("source_task_id", sa.String(), nullable=False),
        sa.Column("times_seen", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_memory_project", "task_memory", ["project"])
    op.create_index("ix_task_memory_fingerprint", "task_memory", ["fingerprint"])

    for column in _TASK_COLUMNS:
        op.add_column("tasks", column)


def downgrade() -> None:
    for column in reversed(_TASK_COLUMNS):
        op.drop_column("tasks", column.name)
    op.drop_index("ix_task_memory_fingerprint", table_name="task_memory")
    op.drop_index("ix_task_memory_project", table_name="task_memory")
    op.drop_table("task_memory")
    op.drop_index("ix_workflows_name", table_name="workflows")
    op.drop_table("workflows")
```

- [ ] **Step 7: Run the migration tests**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_migrations.py -v
```

Expected: PASS. If the guard still reports drift, the index names or a `server_default` differ between the model and the migration — match them exactly.

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa/persistence/orm.py backend/ley_khaa/alembic backend/tests/test_orm_registry_memory.py
git commit -m "feat(persistence): add workflow registry and task memory tables

Two rows and two task columns. A workflow keeps its frozen source, that
source's hash, and a pointer back to the bundle that proved it, so a promoted
capability can always be traced to the run it came from."
```

---

### Task 3: Registry fingerprint — stage-1 matching

**Files:**
- Create: `backend/ley_khaa/registry/__init__.py`, `backend/ley_khaa/registry/models.py`, `backend/ley_khaa/registry/fingerprint.py`
- Test: `backend/tests/test_registry_fingerprint.py`

**Interfaces:**
- Consumes: `TaskSpec`, `WorkflowRow`, `formats.expected_suffixes`.
- Produces:
  - `normalize_operation(operation: str) -> str`
  - `formats_agree(a: str, b: str) -> bool`
  - `fingerprint_candidates(spec: TaskSpec, workflows: list[WorkflowRow]) -> list[WorkflowRow]`
  - `registry/models.py`: `InputRole(BaseModel)` with `role: str`, `suffixes: list[str]`; `RegistryDecision(BaseModel)` with `workflow: str | None`, `confidence: float`, `reason: str`; `Match` frozen dataclass with `workflow: WorkflowRow`, `binding: dict[str, str]`, `matched_by: str`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_registry_fingerprint.py`:

```python
import pytest

from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.persistence.orm import WorkflowRow
from ley_khaa.registry.fingerprint import (
    formats_agree,
    fingerprint_candidates,
    normalize_operation,
)


def _spec(operation="set_difference", output_format="csv", inputs=("a", "b")):
    return TaskSpec(
        intent="compare the two universes",
        inputs=list(inputs),
        operation=operation,
        output_format=output_format,
        certainty=0.9,
    )


def _workflow(name="set_difference", aliases=("set_difference",), output_format="csv", roles=2):
    return WorkflowRow(
        id=name,
        name=name,
        description="",
        operation_aliases=list(aliases),
        output_format=output_format,
        inputs=[{"role": f"r{i}", "suffixes": [".csv"]} for i in range(roles)],
        source="",
        source_sha256="",
        origin="seed",
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Set Difference", "set_difference"),
        ("  set-difference  ", "set_difference"),
        ("set__difference", "set_difference"),
        ("SET DIFFERENCE!!", "set_difference"),
        ("", ""),
    ],
)
def test_operations_normalize_to_one_shape(raw, expected):
    assert normalize_operation(raw) == expected


def test_excel_and_xlsx_are_the_same_format():
    """formats.py already knows this. Comparing raw strings would forget it and
    re-synthesize a workflow we already have, purely over a synonym."""
    assert formats_agree("excel", "xlsx") is True
    assert formats_agree("spreadsheet", "xlsx") is True
    assert formats_agree("csv", "xlsx") is False


def test_an_unknown_format_never_agrees_with_anything():
    """expected_suffixes() returns () for a format it does not recognise. Two
    unknown formats matching each other would let any unrecognised word match
    any other, which is worse than a cache miss."""
    assert formats_agree("interpretive dance", "interpretive dance") is False
    assert formats_agree("interpretive dance", "csv") is False


def test_a_candidate_needs_the_operation_the_format_and_the_arity():
    workflows = [
        _workflow(name="right"),
        _workflow(name="wrong_operation", aliases=("summary_stats",)),
        _workflow(name="wrong_format", output_format="docx"),
        _workflow(name="wrong_arity", roles=1),
    ]
    assert [w.name for w in fingerprint_candidates(_spec(), workflows)] == ["right"]


def test_a_paraphrased_operation_is_a_miss_not_a_guess():
    """Stage 2 exists for this. Stage 1 guessing here is how a request gets run
    by code that was proven for a different job."""
    assert fingerprint_candidates(_spec(operation="compare_lists"), [_workflow()]) == []


def test_a_quarantined_workflow_is_never_a_candidate():
    workflow = _workflow()
    workflow.quarantined = True
    assert fingerprint_candidates(_spec(), [workflow]) == []
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_fingerprint.py -v
```

Expected: `ModuleNotFoundError: No module named 'ley_khaa.registry'`.

- [ ] **Step 3: Create the package types**

Create `backend/ley_khaa/registry/__init__.py` (empty) and `backend/ley_khaa/registry/models.py`:

```python
"""Types the registry passes around (spec §5.6).

The ORM rows live in persistence/orm.py with every other row; these are the
pydantic and dataclass types that never touch the database.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from ..persistence.orm import WorkflowRow


class InputRole(BaseModel):
    """One declared input of a workflow. `role` is the key the frozen script
    reads out of params.json, so it is fixed at promotion and never renamed."""

    role: str
    suffixes: list[str]


class RegistryDecision(BaseModel):
    """What the stage-2 model call returns. `workflow` is a name or null —
    null is a first-class answer, not a failure."""

    workflow: str | None
    confidence: float
    reason: str


@dataclass(frozen=True)
class Match:
    workflow: WorkflowRow
    # role -> the filename in inputs/ that this run bound to it.
    binding: dict[str, str]
    # "fingerprint" or "model". Recorded in the manifest, and the difference
    # decides whether an alias is learned on success.
    matched_by: str
```

- [ ] **Step 4: Implement the fingerprint**

Create `backend/ley_khaa/registry/fingerprint.py`:

```python
"""Stage 1: free, offline, deterministic matching (spec §3.3).

Everything here is a pure function over a spec and some rows. That matters more
than it looks: this is the half of the matcher that still works with no
ANTHROPIC_API_KEY, so the fast path never depends on a model being reachable.
"""
from __future__ import annotations

import re

from ..executor.formats import expected_suffixes
from ..interpreter.spec import TaskSpec
from ..persistence.orm import WorkflowRow

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_operation(operation: str) -> str:
    """Lowercase, non-alphanumerics to _, collapsed, stripped.

    "Set Difference", "set-difference" and "set__difference" are the same
    operation. The interpreter invents these strings freely (its prompt says so),
    so matching raw text would miss the cache on capitalization alone.
    """
    return _NON_ALNUM.sub("_", (operation or "").lower()).strip("_")


def formats_agree(a: str, b: str) -> bool:
    """True when both formats mean the same file suffix.

    Delegates to formats.py, which already knows excel == xlsx == spreadsheet.
    An unrecognised format yields (), and () never agrees with anything —
    otherwise every unknown word would match every other unknown word.
    """
    left, right = expected_suffixes(a), expected_suffixes(b)
    return bool(left) and left == right


def fingerprint_candidates(spec: TaskSpec, workflows: list[WorkflowRow]) -> list[WorkflowRow]:
    """Workflows this spec could be served by, on deterministic evidence alone.

    Conservative on purpose: a paraphrased operation is a miss here and stage 2's
    problem. Guessing at this layer is how a request ends up run by code that was
    proven for a different job — the one failure mode worse than a cache miss.
    """
    operation = normalize_operation(spec.operation)
    if not operation:
        return []
    return [
        workflow
        for workflow in workflows
        if not workflow.quarantined
        and operation in {normalize_operation(a) for a in workflow.operation_aliases or []}
        and formats_agree(spec.output_format, workflow.output_format)
        and len(workflow.inputs or []) == len(spec.inputs or [])
    ]
```

- [ ] **Step 5: Run them and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_fingerprint.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/registry backend/tests/test_registry_fingerprint.py
git commit -m "feat(registry): add deterministic stage-1 workflow matching

Operation normalization plus format agreement via the existing formats.py, so
'excel' and 'xlsx' do not count as different formats and re-synthesize a
workflow that already exists. A paraphrase is deliberately a miss here."
```

---

### Task 4: The binder — roles to this run's files, or a refusal

**Files:**
- Create: `backend/ley_khaa/registry/binder.py`
- Test: `backend/tests/test_registry_binder.py`

**Interfaces:**
- Consumes: `WorkflowRow.inputs` (`[{"role": str, "suffixes": [str]}]`), `ResolvedInput` (`.name`, `.filename`).
- Produces: `bind(workflow: WorkflowRow, resolved: list[ResolvedInput]) -> dict[str, str] | None` — role → filename, or `None` meaning "not a match". Never raises, never guesses.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_registry_binder.py`:

```python
from ley_khaa.executor.resolver import ResolvedInput
from ley_khaa.persistence.orm import WorkflowRow
from ley_khaa.registry.binder import bind


def _workflow(roles):
    return WorkflowRow(
        id="w", name="w", description="", operation_aliases=["w"], output_format="csv",
        inputs=roles, source="", source_sha256="", origin="seed",
    )


def _resolved(name, filename):
    return ResolvedInput(name=name, filename=filename, content="ticker\nAAA\n", source="catalog")


def test_roles_bind_positionally_to_this_run_s_files():
    """The frozen script reads params["inputs"]["left"]. Binding is what puts
    THIS run's filename behind that name."""
    workflow = _workflow([
        {"role": "left", "suffixes": [".csv"]},
        {"role": "right", "suffixes": [".csv"]},
    ])
    resolved = [_resolved("bloomberg universe", "b.csv"), _resolved("factset universe", "f.csv")]

    assert bind(workflow, resolved) == {"left": "b.csv", "right": "f.csv"}


def test_a_suffix_mismatch_is_a_refusal_not_a_coercion():
    """A workflow that parses CSV handed an .xlsx will not fail cleanly — it
    will produce garbage that the validator may well accept."""
    workflow = _workflow([{"role": "left", "suffixes": [".csv"]}])
    assert bind(workflow, [_resolved("book", "b.xlsx")]) is None


def test_a_count_mismatch_is_a_refusal():
    workflow = _workflow([{"role": "left", "suffixes": [".csv"]}])
    resolved = [_resolved("a", "a.csv"), _resolved("b", "b.csv")]

    assert bind(workflow, resolved) is None
    assert bind(workflow, []) is None


def test_a_role_with_no_declared_suffixes_accepts_anything():
    """An empty list is 'no opinion', the same convention formats.py uses."""
    workflow = _workflow([{"role": "any", "suffixes": []}])
    assert bind(workflow, [_resolved("x", "x.docx")]) == {"any": "x.docx"}


def test_duplicate_roles_are_refused():
    """Two roles with one name means one of them silently wins in params.json,
    and the frozen script reads a file it was not given."""
    workflow = _workflow([
        {"role": "left", "suffixes": [".csv"]},
        {"role": "left", "suffixes": [".csv"]},
    ])
    resolved = [_resolved("a", "a.csv"), _resolved("b", "b.csv")]

    assert bind(workflow, resolved) is None


def test_a_malformed_role_declaration_is_a_refusal_not_a_crash():
    """A row can be hand-edited in the database. The matcher must survive it."""
    workflow = _workflow([{"suffixes": [".csv"]}])
    assert bind(workflow, [_resolved("a", "a.csv")]) is None
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_binder.py -v
```

Expected: `ModuleNotFoundError: No module named 'ley_khaa.registry.binder'`.

- [ ] **Step 3: Implement `bind`**

Create `backend/ley_khaa/registry/binder.py`:

```python
"""Stage 3: bind a workflow's declared roles to this run's resolved inputs.

The rule that governs this whole module: **a bind failure is a cache miss, never
a guess.** Falling through to synthesis costs one Opus call. Binding the wrong
file to a role costs a confident, deterministic, wrong answer that the validator
may well accept — a spreadsheet full of the wrong numbers is still a spreadsheet
full of numbers.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..executor.resolver import ResolvedInput
from ..persistence.orm import WorkflowRow

logger = logging.getLogger(__name__)


def bind(workflow: WorkflowRow, resolved: list[ResolvedInput]) -> dict[str, str] | None:
    """role -> filename in inputs/, or None if this run cannot serve this workflow.

    Positional: roles are declared in the order the script expects them, and
    resolved inputs arrive in spec-input order. Anything else — a different
    count, a suffix the role does not accept, a malformed declaration — is a
    refusal.
    """
    roles = workflow.inputs or []
    if len(roles) != len(resolved) or not roles:
        return None

    binding: dict[str, str] = {}
    for declared, item in zip(roles, resolved):
        if not isinstance(declared, dict):
            return None
        role = declared.get("role")
        if not role or role in binding:
            # A duplicate role silently collapses in params.json, leaving the
            # frozen script reading a file it was never bound.
            return None
        suffixes = declared.get("suffixes") or []
        if suffixes and Path(item.filename).suffix.lower() not in {s.lower() for s in suffixes}:
            return None
        binding[role] = item.filename
    return binding
```

- [ ] **Step 4: Run them and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_binder.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ley_khaa/registry/binder.py backend/tests/test_registry_binder.py
git commit -m "feat(registry): bind workflow roles to a run's inputs, or refuse

Positional binding with a suffix check. Every failure mode returns None rather
than picking the likeliest file: a cache miss costs one model call, a wrong
bind costs a confident wrong answer the validator may accept."
```

---

### Task 5: `WorkflowRepository`

**Files:**
- Create: `backend/ley_khaa/persistence/workflow_repository.py`
- Test: `backend/tests/test_workflow_repository.py`

**Interfaces:**
- Produces `WorkflowRepository(session)` with:
  - `create(*, name, description, operation_aliases, output_format, inputs, source, origin="promoted", promoted_from_task_id=None) -> WorkflowRow` (raises `DuplicateWorkflow` on a taken name)
  - `get(name) -> WorkflowRow | None` · `list() -> list[WorkflowRow]` · `active() -> list[WorkflowRow]`
  - `record_success(name, *, learned_alias: str | None = None) -> WorkflowRow`
  - `record_failure(name) -> WorkflowRow` (increments and quarantines)
  - `unquarantine(name) -> WorkflowRow` · `delete(name) -> None`
- Also exports `DuplicateWorkflow(Exception)`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_workflow_repository.py`:

```python
import pytest

from ley_khaa.persistence.workflow_repository import DuplicateWorkflow, WorkflowRepository


def _create(repo, name="set_difference", aliases=("set_difference",)):
    return repo.create(
        name=name,
        description="rows in A missing from B",
        operation_aliases=list(aliases),
        output_format="csv",
        inputs=[{"role": "left", "suffixes": [".csv"]}],
        source="print('hi')",
        origin="seed",
    )


def test_creating_a_workflow_hashes_its_source(session):
    """source_sha256 is what lets a manifest prove which code ran."""
    import hashlib

    repo = WorkflowRepository(session)
    row = _create(repo)

    assert row.source_sha256 == hashlib.sha256(b"print('hi')").hexdigest()


def test_a_taken_name_is_refused(session):
    repo = WorkflowRepository(session)
    _create(repo)
    with pytest.raises(DuplicateWorkflow):
        _create(repo)


def test_active_excludes_quarantined_workflows(session):
    repo = WorkflowRepository(session)
    _create(repo, name="good")
    _create(repo, name="bad")
    repo.record_failure("bad")

    assert [w.name for w in repo.active()] == ["good"]
    assert len(repo.list()) == 2


def test_a_failure_quarantines_immediately(session):
    """One wrong answer is enough. A workflow that just produced garbage must
    not be handed the next matching request as though nothing happened."""
    repo = WorkflowRepository(session)
    _create(repo)
    row = repo.record_failure("set_difference")

    assert row.quarantined is True
    assert row.runs_failed == 1


def test_success_records_use_and_learns_an_alias(session):
    """The learning loop: a phrasing the model matched, that then passed, is a
    free deterministic hit forever after."""
    repo = WorkflowRepository(session)
    _create(repo)
    row = repo.record_success("set_difference", learned_alias="compare_lists")

    assert row.runs_ok == 1
    assert row.last_used_at is not None
    assert set(row.operation_aliases) == {"set_difference", "compare_lists"}


def test_a_known_alias_is_not_added_twice(session):
    repo = WorkflowRepository(session)
    _create(repo)
    repo.record_success("set_difference", learned_alias="set_difference")

    assert repo.get("set_difference").operation_aliases == ["set_difference"]


def test_unquarantine_lets_a_workflow_match_again(session):
    repo = WorkflowRepository(session)
    _create(repo)
    repo.record_failure("set_difference")
    repo.unquarantine("set_difference")

    assert repo.get("set_difference").quarantined is False
    # The failure itself stays on the record.
    assert repo.get("set_difference").runs_failed == 1
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_workflow_repository.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement the repository**

Create `backend/ley_khaa/persistence/workflow_repository.py`:

```python
"""The registry's storage (spec §5.6).

Follows the house pattern: rows in orm.py, access here, no ORM objects
constructed anywhere else.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .orm import WorkflowRow


class DuplicateWorkflow(Exception):
    """That name is taken. Names are how a human refers to a capability, so
    silently versioning behind one would make the registry unreadable."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        name: str,
        description: str,
        operation_aliases: list[str],
        output_format: str,
        inputs: list[dict],
        source: str,
        origin: str = "promoted",
        promoted_from_task_id: str | None = None,
    ) -> WorkflowRow:
        if self.get(name) is not None:
            raise DuplicateWorkflow(name)
        row = WorkflowRow(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            operation_aliases=list(operation_aliases),
            output_format=output_format,
            inputs=list(inputs),
            source=source,
            source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            origin=origin,
            promoted_from_task_id=promoted_from_task_id,
            promoted_at=_now(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def get(self, name: str) -> WorkflowRow | None:
        return self.session.scalars(
            select(WorkflowRow).where(WorkflowRow.name == name)
        ).one_or_none()

    def list(self) -> list[WorkflowRow]:
        return list(self.session.scalars(select(WorkflowRow).order_by(WorkflowRow.name)))

    def active(self) -> list[WorkflowRow]:
        """What the matcher is allowed to consider."""
        return [row for row in self.list() if not row.quarantined]

    def _row(self, name: str) -> WorkflowRow:
        row = self.get(name)
        if row is None:
            raise KeyError(name)
        return row

    def record_success(self, name: str, *, learned_alias: str | None = None) -> WorkflowRow:
        row = self._row(name)
        row.runs_ok += 1
        row.last_used_at = _now()
        if learned_alias and learned_alias not in (row.operation_aliases or []):
            # Reassign rather than append: a JSON column mutated in place is not
            # always seen as dirty by SQLAlchemy, and the alias would be lost on
            # commit — the learning loop failing silently.
            row.operation_aliases = list(row.operation_aliases or []) + [learned_alias]
        self.session.commit()
        return row

    def record_failure(self, name: str) -> WorkflowRow:
        row = self._row(name)
        row.runs_failed += 1
        row.quarantined = True
        self.session.commit()
        return row

    def unquarantine(self, name: str) -> WorkflowRow:
        row = self._row(name)
        row.quarantined = False
        self.session.commit()
        return row

    def delete(self, name: str) -> None:
        self.session.delete(self._row(name))
        self.session.commit()
```

- [ ] **Step 4: Run them and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_workflow_repository.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ley_khaa/persistence/workflow_repository.py backend/tests/test_workflow_repository.py
git commit -m "feat(persistence): add WorkflowRepository

Create hashes the source on the way in, a failure quarantines on the first
occurrence, and a learned alias is written by reassigning the JSON column —
mutating it in place is not reliably seen as dirty, which would lose the
learning loop's only effect."
```

---

### Task 6: The two-stage registry matcher

**Files:**
- Modify: `backend/ley_khaa/llm/router.py` (two stages)
- Modify: `backend/ley_khaa/llm/heuristic.py` (answer `RegistryDecision`)
- Create: `backend/ley_khaa/registry/matcher.py`
- Test: `backend/tests/test_registry_matcher.py`, `backend/tests/test_router.py`

**Interfaces:**
- Consumes: `fingerprint_candidates`, `bind`, `WorkflowRepository`, `LLMClient`, `RegistryDecision`, `Match`.
- Produces: `RegistryMatcher(workflows: WorkflowRepository, llm: LLMClient)` with `match(spec: TaskSpec, resolved: list[ResolvedInput]) -> Match | None`, and `CONFIDENCE_FLOOR = 0.8`.
- Router gains `Stage.REGISTRY_MATCH` and `Stage.MEMORY_MATCH` (Haiku at both complexities, `max_tokens` 1024). Task 12 uses `MEMORY_MATCH`; both are added here so the router has one change, not two.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_registry_matcher.py`:

```python
import pytest

from ley_khaa.executor.resolver import ResolvedInput
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.workflow_repository import WorkflowRepository
from ley_khaa.registry.matcher import CONFIDENCE_FLOOR, RegistryMatcher
from ley_khaa.registry.models import RegistryDecision


def _spec(operation="set_difference", output_format="csv"):
    return TaskSpec(
        intent="compare the universes", inputs=["a", "b"], operation=operation,
        output_format=output_format, certainty=0.9,
    )


def _resolved():
    return [
        ResolvedInput(name="a", filename="a.csv", content="t\nAAA\n", source="catalog"),
        ResolvedInput(name="b", filename="b.csv", content="t\nBBB\n", source="catalog"),
    ]


def _seed(session, aliases=("set_difference",)):
    repo = WorkflowRepository(session)
    repo.create(
        name="set_difference", description="rows in A missing from B",
        operation_aliases=list(aliases), output_format="csv",
        inputs=[{"role": "left", "suffixes": [".csv"]}, {"role": "right", "suffixes": [".csv"]}],
        source="print('hi')", origin="seed",
    )
    return repo


def test_a_fingerprint_hit_never_calls_the_model(session):
    """The whole point. A cache that costs a model call to consult is not a
    cache — it is a slower synthesis."""
    repo = _seed(session)
    llm = FakeLLM(responses=[])

    match = RegistryMatcher(repo, llm).match(_spec(), _resolved())

    assert match is not None
    assert match.matched_by == "fingerprint"
    assert match.binding == {"left": "a.csv", "right": "b.csv"}
    assert llm.calls == []


def test_a_paraphrase_is_found_by_the_model(session):
    repo = _seed(session)
    llm = FakeLLM(responses=[
        RegistryDecision(workflow="set_difference", confidence=0.92, reason="same shape")
    ])

    match = RegistryMatcher(repo, llm).match(_spec(operation="compare_lists"), _resolved())

    assert match is not None
    assert match.matched_by == "model"
    assert len(llm.calls) == 1


def test_a_low_confidence_answer_is_not_a_match(session):
    repo = _seed(session)
    llm = FakeLLM(responses=[
        RegistryDecision(workflow="set_difference", confidence=CONFIDENCE_FLOOR - 0.01, reason="maybe")
    ])

    assert RegistryMatcher(repo, llm).match(_spec(operation="compare_lists"), _resolved()) is None


def test_a_model_naming_a_workflow_that_does_not_exist_is_not_a_match(session):
    """Model output is untrusted here exactly as it is in the crystallizer."""
    repo = _seed(session)
    llm = FakeLLM(responses=[
        RegistryDecision(workflow="does_not_exist", confidence=0.99, reason="confident nonsense")
    ])

    assert RegistryMatcher(repo, llm).match(_spec(operation="compare_lists"), _resolved()) is None


def test_a_model_match_that_cannot_bind_is_not_a_match(session):
    repo = _seed(session)
    llm = FakeLLM(responses=[
        RegistryDecision(workflow="set_difference", confidence=0.99, reason="looks right")
    ])
    wrong_shape = [ResolvedInput(name="a", filename="a.csv", content="x", source="catalog")]

    assert RegistryMatcher(repo, llm).match(_spec(operation="compare_lists"), wrong_shape) is None


def test_an_empty_registry_never_calls_the_model(session):
    """Nothing to match against, so asking would be a call that cannot succeed."""
    llm = FakeLLM(responses=[])
    assert RegistryMatcher(WorkflowRepository(session), llm).match(_spec(), _resolved()) is None
    assert llm.calls == []


def test_a_broken_model_call_is_a_miss_not_a_crash(session):
    """A cache that fails must cost only the work it was trying to save."""
    class Boom:
        name = "boom"

        def parse(self, **kwargs):
            raise RuntimeError("connection reset")

    repo = _seed(session)
    assert RegistryMatcher(repo, Boom()).match(_spec(operation="compare_lists"), _resolved()) is None


def test_the_offline_stand_in_answers_no_match(session):
    """With no API key the fast path is fingerprint-only, not broken."""
    from ley_khaa.llm.heuristic import HeuristicLLM

    repo = _seed(session)
    assert RegistryMatcher(repo, HeuristicLLM()).match(_spec(operation="compare_lists"), _resolved()) is None
```

Add to `backend/tests/test_router.py`:

```python
def test_the_cache_matchers_run_on_haiku():
    """These calls exist to AVOID an Opus call. Routing them to Opus would make
    consulting the cache cost more than the synthesis it saves."""
    from ley_khaa.llm.router import HAIKU, Stage, model_for

    for stage in (Stage.REGISTRY_MATCH, Stage.MEMORY_MATCH):
        for complexity in ("routine", "hard"):
            choice = model_for(stage, complexity)
            assert choice.model == HAIKU
            assert choice.supports_thinking is False
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_matcher.py tests/test_router.py -v
```

Expected: `ModuleNotFoundError` and `AttributeError: REGISTRY_MATCH`.

- [ ] **Step 3: Add the router stages**

In `backend/ley_khaa/llm/router.py`, add to `Stage`:

```python
    REGISTRY_MATCH = "registry_match"
    MEMORY_MATCH = "memory_match"
```

to `_POLICY`:

```python
    # Both exist to avoid an Opus call. Routing them to Opus would make
    # consulting the cache cost more than the work it saves.
    Stage.REGISTRY_MATCH: {"routine": HAIKU, "hard": HAIKU},
    Stage.MEMORY_MATCH: {"routine": HAIKU, "hard": HAIKU},
```

and to `_MAX_TOKENS`:

```python
    # A name, a float and one sentence.
    Stage.REGISTRY_MATCH: 1024,
    Stage.MEMORY_MATCH: 1024,
```

- [ ] **Step 4: Teach the offline stand-in to decline**

In `backend/ley_khaa/llm/heuristic.py`, import `RegistryDecision` from `..registry.models` and add to `parse`, before the final `raise NotImplementedError`:

```python
        if output_format is RegistryDecision:
            # Offline matching is fingerprint-only by design: a regex cannot
            # judge whether two phrasings mean the same operation, and guessing
            # here would hand a request to code proven for a different job.
            return RegistryDecision(workflow=None, confidence=0.0, reason="offline: no model match")
```

If importing `..registry.models` at module scope creates a cycle (it imports `persistence.orm`), import it inside `parse` instead and leave a one-line comment saying why.

- [ ] **Step 5: Implement the matcher**

Create `backend/ley_khaa/registry/matcher.py`:

```python
"""Does a proven workflow already do this? (spec §3.3)

Two stages and a bind. The shape is the Crystallizer's: a free deterministic
filter first, one cheap model call only when that filter says nothing. No match
is always a legal answer — it costs a fall-through to synthesis, which is the
path that worked before this module existed.
"""
from __future__ import annotations

import logging

from ..executor.resolver import ResolvedInput
from ..interpreter.spec import TaskSpec
from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.workflow_repository import WorkflowRepository
from .binder import bind
from .fingerprint import fingerprint_candidates, normalize_operation
from .models import Match, RegistryDecision

logger = logging.getLogger(__name__)

# Below this, the model's answer is not evidence. Pinned by a test: loosening it
# silently is how a cache starts serving confident wrong answers.
CONFIDENCE_FLOOR = 0.8

SYSTEM = """You decide whether a data request can be served by an existing, proven workflow.

You are given one request and a list of workflows, each with a name, a description, and the
inputs and output format it expects. Answer with the name of the workflow that does EXACTLY
this job, or null.

Say null unless you are confident. A wrong match runs code that was proven for a different
job and produces a plausible, wrong answer. A null costs only that the script is written
fresh, which is the normal path. When in doubt, null."""


class RegistryMatcher:
    def __init__(self, workflows: WorkflowRepository, llm: LLMClient) -> None:
        self.workflows = workflows
        self.llm = llm

    def match(self, spec: TaskSpec, resolved: list[ResolvedInput]) -> Match | None:
        try:
            return self._match(spec, resolved)
        except Exception:
            # A cache that fails must cost only the work it was trying to save.
            logger.exception("registry matching failed; falling through to synthesis")
            return None

    def _match(self, spec: TaskSpec, resolved: list[ResolvedInput]) -> Match | None:
        active = self.workflows.active()
        if not active:
            return None

        for workflow in fingerprint_candidates(spec, active):
            binding = bind(workflow, resolved)
            if binding is not None:
                return Match(workflow=workflow, binding=binding, matched_by="fingerprint")

        decision = self.llm.parse(
            choice=model_for(Stage.REGISTRY_MATCH),
            system=SYSTEM,
            user=_prompt(spec, active),
            output_format=RegistryDecision,
        )
        if not decision.workflow or decision.confidence < CONFIDENCE_FLOOR:
            return None

        # The model names a workflow; it does not choose one. A hallucinated
        # name is the same untrusted-output problem the crystallizer already
        # learned, and active() is what keeps a quarantined row unreachable.
        chosen = next((w for w in active if w.name == decision.workflow), None)
        if chosen is None:
            logger.info("registry matcher named an unknown workflow %r", decision.workflow)
            return None

        binding = bind(chosen, resolved)
        if binding is None:
            return None
        return Match(workflow=chosen, binding=binding, matched_by="model")


def _prompt(spec: TaskSpec, workflows: list) -> str:
    lines = [
        "## Request",
        f"intent: {spec.intent}",
        f"operation: {normalize_operation(spec.operation)}",
        f"inputs: {', '.join(spec.inputs)}",
        f"output_format: {spec.output_format}",
        "",
        "## Available workflows",
    ]
    for workflow in workflows:
        roles = ", ".join(str(role.get("role")) for role in workflow.inputs or [])
        lines.append(
            f"- {workflow.name}: {workflow.description} "
            f"(inputs: {roles}; output: {workflow.output_format})"
        )
    return "\n".join(lines)
```

- [ ] **Step 6: Run them and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_matcher.py tests/test_router.py -v
```

Expected: all PASS.

- [ ] **Step 7: Run the whole suite**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest -q
```

Expected: all pass. A `NotImplementedError` from `HeuristicLLM` here means Step 4 was skipped.

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa/registry backend/ley_khaa/llm backend/tests
git commit -m "feat(registry): add the two-stage workflow matcher

Fingerprint first and free; one Haiku call only on a miss, gated at 0.8, whose
answer is treated as untrusted — a named workflow must exist, be active, and
bind before it is a match. Every failure path returns no-match."
```

---

### Task 7: Seed workflows

**Files:**
- Create: `backend/ley_khaa/registry/seeds/__init__.py`, `seeds/set_difference.py`, `seeds/summary_stats.py`
- Modify: `backend/ley_khaa/api/app.py` (install seeds at startup)
- Modify: `backend/tests/conftest.py` (a `seeded_registry` fixture)
- Test: `backend/tests/test_registry_seeds.py`

**Interfaces:**
- Produces: `SEEDS: list[dict]` and `ensure_seed_workflows(session) -> int` (returns how many were installed; idempotent), plus a `seeded_registry` pytest fixture.

**Why startup and not the migration:** see Departure 2. **Why tests must opt in:** `conftest.py` sets `LEY_KHAA_DISABLE_STARTUP=1`, so the lifespan seeding never runs under test. Existing tests therefore keep the synthesis lane unchanged, and a test that wants the fast path asks for it explicitly — which also keeps every test's lane obvious from its own body.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_registry_seeds.py`:

```python
import json
import subprocess
import sys

from ley_khaa.persistence.workflow_repository import WorkflowRepository
from ley_khaa.registry.seeds import ensure_seed_workflows


def test_seeding_is_idempotent(session):
    """Startup runs on every boot; a second boot must not duplicate or raise."""
    assert ensure_seed_workflows(session) == 2
    assert ensure_seed_workflows(session) == 0
    assert len(WorkflowRepository(session).list()) == 2


def test_seeds_are_marked_as_seeds_not_promotions(session):
    ensure_seed_workflows(session)
    for row in WorkflowRepository(session).list():
        assert row.origin == "seed"
        assert row.promoted_from_task_id is None


def test_the_set_difference_seed_reads_its_binding_and_writes_the_deliverable(tmp_path):
    """The seed is a real program, run the way the sandbox runs it: cwd is the
    bundle root, paths come from inputs/params.json."""
    (tmp_path / "inputs").mkdir()
    (tmp_path / "deliverable").mkdir()
    (tmp_path / "inputs" / "left.csv").write_text("ticker,name\nAAA,Alpha\nBBB,Beta\n")
    (tmp_path / "inputs" / "right.csv").write_text("ticker,name\nBBB,Beta\n")
    (tmp_path / "inputs" / "params.json").write_text(
        json.dumps({
            "inputs": {"left": "left.csv", "right": "right.csv"},
            "output": "deliverable/output.csv",
            "seed": 1,
        })
    )
    script = tmp_path / "run.py"
    script.write_text(_source("set_difference"))

    done = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    assert (tmp_path / "deliverable" / "output.csv").read_text() == "ticker,name\nAAA,Alpha\n"


def test_the_summary_stats_seed_summarises_numeric_columns(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "deliverable").mkdir()
    (tmp_path / "inputs" / "data.csv").write_text("name,weight\nAAA,1\nBBB,3\n")
    (tmp_path / "inputs" / "params.json").write_text(
        json.dumps({
            "inputs": {"dataset": "data.csv"},
            "output": "deliverable/output.csv",
            "seed": 1,
        })
    )
    script = tmp_path / "run.py"
    script.write_text(_source("summary_stats"))

    done = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True)

    assert done.returncode == 0, done.stderr
    rows = (tmp_path / "deliverable" / "output.csv").read_text().splitlines()
    assert rows[0] == "column,count,min,max,mean"
    assert rows[1] == "weight,2,1.0000,3.0000,2.0000"


def _source(name: str) -> str:
    from ley_khaa.registry.seeds import SEEDS

    return next(seed["source"] for seed in SEEDS if seed["name"] == name)
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_seeds.py -v
```

Expected: `ModuleNotFoundError: No module named 'ley_khaa.registry.seeds'`.

- [ ] **Step 3: Write the two seed scripts**

Create `backend/ley_khaa/registry/seeds/set_difference.py`. This file holds the source as a string, because what is stored in the row is source text, and keeping it here means it is lintable and diffable rather than a blob in a migration:

```python
"""The seed set_difference workflow (spec §5.6).

Hand-written, not model-written: this is what a hardened, promoted capability is
supposed to look like, and it is the thing a promoted script is measured against.
Reads its binding from inputs/params.json like every other generator.
"""

SOURCE = '''"""Rows in the left input whose key is absent from the right input.

A seed workflow of ley-khaa's registry: proven code, run without a model.
"""
import csv
import json

with open("inputs/params.json", encoding="utf-8") as handle:
    params = json.load(handle)

TARGET = params["output"]


def read_rows(name):
    with open("inputs/" + name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


left = read_rows(params["inputs"]["left"])
right = read_rows(params["inputs"]["right"])

fields = list(left[0].keys()) if left else ["ticker"]
key = fields[0]
seen = {row.get(key) for row in right}
missing = [row for row in left if row.get(key) not in seen]

if TARGET.endswith(".xlsx"):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "result"
    sheet.append(fields)
    for row in missing:
        sheet.append([row.get(field, "") for field in fields])
    book.save(TARGET)
else:
    with open(TARGET, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\\n")
        writer.writeheader()
        for row in missing:
            writer.writerow({field: row.get(field, "") for field in fields})

print("%d of %d rows keyed on %s are missing from the second input"
      % (len(missing), len(left), key))
'''

WORKFLOW = {
    "name": "set_difference",
    "description": "rows in the first input whose key column is absent from the second",
    "operation_aliases": ["set_difference"],
    # The golden universe check asks for Excel, and a workflow declares exactly
    # one output format — a CSV request is a different capability, not this one.
    "output_format": "xlsx",
    "inputs": [
        {"role": "left", "suffixes": [".csv"]},
        {"role": "right", "suffixes": [".csv"]},
    ],
    "source": SOURCE,
}
```

Create `backend/ley_khaa/registry/seeds/summary_stats.py` the same way:

```python
"""The seed summary_stats workflow (spec §5.6). Hand-written, not model-written."""

SOURCE = '''"""Count, min, max and mean of every numeric column in one dataset.

A seed workflow of ley-khaa's registry: proven code, run without a model.
"""
import csv
import json

with open("inputs/params.json", encoding="utf-8") as handle:
    params = json.load(handle)

TARGET = params["output"]

with open("inputs/" + params["inputs"]["dataset"], newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))

summary = []
for field in (list(rows[0].keys()) if rows else []):
    values = []
    for row in rows:
        try:
            values.append(float(row.get(field, "")))
        except (TypeError, ValueError):
            continue
    if not values:
        continue
    summary.append({
        "column": field,
        "count": str(len(values)),
        "min": "%.4f" % min(values),
        "max": "%.4f" % max(values),
        "mean": "%.4f" % (sum(values) / len(values)),
    })

fields = ["column", "count", "min", "max", "mean"]
if TARGET.endswith(".xlsx"):
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "result"
    sheet.append(fields)
    for row in summary:
        sheet.append([row[field] for field in fields])
    book.save(TARGET)
else:
    with open(TARGET, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\\n")
        writer.writeheader()
        for row in summary:
            writer.writerow(row)

print("summarised %d numeric column(s) over %d row(s)" % (len(summary), len(rows)))
'''

WORKFLOW = {
    "name": "summary_stats",
    "description": "count, min, max and mean of every numeric column of one dataset",
    "output_format": "csv",
    "operation_aliases": ["summary_stats"],
    "inputs": [{"role": "dataset", "suffixes": [".csv"]}],
    "source": SOURCE,
}
```

- [ ] **Step 4: Implement the installer**

Create `backend/ley_khaa/registry/seeds/__init__.py`:

```python
"""The registry ships near-empty but not empty (spec §5.6).

Two proven workflows, so a fresh clone can demonstrate the fast path before any
human has promoted anything. Installed at startup rather than by a migration:
migrations that import application code rot when the code moves, and app.py
already seeds the demo conversation this same way.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..fingerprint import normalize_operation
from ...persistence.workflow_repository import DuplicateWorkflow, WorkflowRepository
from . import set_difference, summary_stats

SEEDS: list[dict] = [set_difference.WORKFLOW, summary_stats.WORKFLOW]


def ensure_seed_workflows(session: Session) -> int:
    """Install any seed the registry does not already have. Returns how many.

    Idempotent: startup runs on every boot, and a human may have deleted a seed
    on purpose — so this fills gaps rather than resetting the registry. It never
    overwrites an existing row, because that row may be one a human edited.
    """
    repo = WorkflowRepository(session)
    installed = 0
    for seed in SEEDS:
        if repo.get(seed["name"]) is not None:
            continue
        try:
            repo.create(
                name=seed["name"],
                description=seed["description"],
                operation_aliases=[normalize_operation(a) for a in seed["operation_aliases"]],
                output_format=seed["output_format"],
                inputs=seed["inputs"],
                source=seed["source"],
                origin="seed",
            )
        except DuplicateWorkflow:
            # Another process seeded it between the check and the insert.
            continue
        installed += 1
    return installed
```

- [ ] **Step 5: Install at startup and expose a test fixture**

In `backend/ley_khaa/api/app.py`, inside `lifespan`, in the existing `try:` block after `repo = TaskRepository(session)`:

```python
        # The registry ships with two proven workflows so a fresh clone can show
        # the fast path before anyone has promoted anything.
        ensure_seed_workflows(session)
```

with `from ..registry.seeds import ensure_seed_workflows` at the top.

In `backend/tests/conftest.py`, add:

```python
@pytest.fixture
def seeded_registry(session):
    """Install the seed workflows for tests that want the registry fast path.

    Not automatic: conftest sets LEY_KHAA_DISABLE_STARTUP=1, so the lifespan
    seeding never runs under test. Opting in keeps every existing test on the
    synthesis lane it was written for, and makes a test's lane obvious from its
    own body rather than from a global.
    """
    from ley_khaa.registry.seeds import ensure_seed_workflows

    ensure_seed_workflows(session)
    return session
```

- [ ] **Step 6: Run them and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_seeds.py -v
```

Expected: all PASS. A failing `set_difference` assertion on `\r\n` line endings means `lineterminator="\n"` was dropped from the `DictWriter`.

- [ ] **Step 7: Commit**

```bash
git add backend/ley_khaa/registry/seeds backend/ley_khaa/api/app.py backend/tests
git commit -m "feat(registry): ship two hand-written seed workflows

set_difference and summary_stats, installed idempotently at startup rather than
by a migration. Both read their binding from params.json, so they are exactly
the shape a promoted script has — they are the reference, not a special case."
```

---

### Task 8: The registry fast path in `ExecutionRunner`

The lane selection itself. This is the task with the most ways to go quietly wrong, so read the whole task before starting.

**Files:**
- Modify: `backend/ley_khaa/executor/runner.py`
- Modify: `backend/ley_khaa/orchestrator/driver.py`, `backend/ley_khaa/orchestrator/orchestrator.py`, `backend/ley_khaa/api/app.py` (thread `workflows=` through)
- Test: `backend/tests/test_runner_registry.py`

**Interfaces:**
- Consumes: `RegistryMatcher`, `Match`, `WorkflowRepository`, `Workspace.write_params`.
- Produces: `ExecutionRunner(..., workflows: WorkflowRepository | None = None)`; manifest keys `lane` and `workflow`.

**Two traps this task must avoid:**

1. **`input_hashes` must be computed after the final write of `params.json`.** A cached run rewrites `params.json` with the workflow's role names, and a fallback to synthesis rewrites it again with the spec's input names. Hashing before the last write makes the validator report that the script tampered with its inputs — a false failure whose message points nowhere near the cause.
2. **A failed cached run must not consume a synthesis attempt.** `next_attempt_number()` keeps numbering across rounds; the cached attempt takes one number, and synthesis starts at the next.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_runner_registry.py`. Reuse the helpers from `tests/test_runner.py` (`_runner`, `_spec`, `_writes_csv`, `_script`) by importing them if they are module-level, or copy the two you need — do not invent a different fixture shape:

```python
import json

from ley_khaa.executor.runner import ExecutionRunner
from ley_khaa.persistence.workflow_repository import WorkflowRepository
from ley_khaa.registry.seeds import ensure_seed_workflows


def test_a_matching_request_runs_the_proven_code_and_calls_no_model(tmp_path, task, session):
    """The claim the registry exists to make."""
    ensure_seed_workflows(session)
    llm = FakeLLM(responses=[])   # any synthesis call would IndexError
    row, runner = _registry_runner(tmp_path, task, session, llm)

    outcome = runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["data"]))

    assert outcome.verdict.ok, outcome.verdict.reason
    assert llm.calls == []
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["lane"] == "registry"
    assert manifest["workflow"]["name"] == "summary_stats"
    assert manifest["workflow"]["matched_by"] == "fingerprint"


def test_the_manifest_credits_no_model_on_a_cached_run(tmp_path, task, session):
    """No model wrote that script. The manifest may not imply one did — the same
    rule the sandbox field follows."""
    ensure_seed_workflows(session)
    row, runner = _registry_runner(tmp_path, task, session, FakeLLM(responses=[]))
    runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["data"]))

    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["models"]["synthesis"] is None


def test_the_binding_is_recorded_and_the_frozen_source_is_what_ran(tmp_path, task, session):
    ensure_seed_workflows(session)
    row, runner = _registry_runner(tmp_path, task, session, FakeLLM(responses=[]))
    runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["data"]))

    root = tmp_path / f"task-{row.id}"
    manifest = json.loads((root / "manifest.json").read_text())
    workflow = WorkflowRepository(session).get("summary_stats")

    assert manifest["workflow"]["sha256"] == workflow.source_sha256
    assert (root / "generator" / "attempt_1.py").read_text() == workflow.source
    # params.json carries the WORKFLOW's role name, not the spec's input name.
    params = json.loads((root / "inputs" / "params.json").read_text())
    assert list(params["inputs"]) == ["dataset"]
    assert manifest["workflow"]["binding"] == params["inputs"]


def test_a_failing_cached_workflow_quarantines_and_the_run_still_succeeds(tmp_path, task, session):
    """A cache that fails costs only the work it was trying to save."""
    repo = WorkflowRepository(session)
    repo.create(
        name="poisoned", description="always crashes",
        operation_aliases=["summary_stats"], output_format="csv",
        inputs=[{"role": "dataset", "suffixes": [".csv"]}],
        source="raise SystemExit(1)", origin="seed",
    )
    llm = FakeLLM(responses=[_script()])   # exactly one synthesis rescue
    row, runner = _registry_runner(tmp_path, task, session, llm, steps=[_writes_csv])

    outcome = runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["data"]))

    assert outcome.verdict.ok, outcome.verdict.reason
    assert repo.get("poisoned").quarantined is True
    assert repo.get("poisoned").runs_failed == 1

    root = tmp_path / f"task-{row.id}"
    manifest = json.loads((root / "manifest.json").read_text())
    # The lane that produced the deliverable, plus a record that the cache was tried.
    assert manifest["lane"] == "synthesis"
    assert manifest["workflow"]["name"] == "poisoned"
    assert manifest["workflow"]["quarantined"] is True
    # Both attempts are in the bundle. The cached one did not eat a synthesis attempt.
    assert (root / "generator" / "attempt_1.py").is_file()
    assert (root / "generator" / "attempt_2.py").is_file()
    assert len(manifest["attempts"]) == 2


def test_the_inputs_are_not_reported_as_tampered_after_a_fallback(tmp_path, task, session):
    """params.json is rewritten between the lanes. Hashing before the last write
    makes the validator accuse the script of rewriting its own inputs."""
    repo = WorkflowRepository(session)
    repo.create(
        name="poisoned", description="always crashes",
        operation_aliases=["summary_stats"], output_format="csv",
        inputs=[{"role": "dataset", "suffixes": [".csv"]}],
        source="raise SystemExit(1)", origin="seed",
    )
    row, runner = _registry_runner(
        tmp_path, task, session, FakeLLM(responses=[_script()]), steps=[_writes_csv]
    )

    outcome = runner.run(row, _spec(operation="summary_stats", output_format="csv", inputs=["data"]))

    assert outcome.verdict.ok
    assert outcome.verdict.checks.get("inputs_unmodified") is not False


def test_with_no_registry_the_runner_behaves_exactly_as_before(tmp_path, task):
    """workflows=None is a supported configuration, not a broken one."""
    row, runner = _runner(tmp_path, task, responses=[_script()], steps=[_writes_csv])
    outcome = runner.run(row, _spec())

    assert outcome.verdict.ok
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["lane"] == "synthesis"
    assert "workflow" not in manifest or manifest["workflow"] is None
```

Write `_registry_runner` as a local helper mirroring `test_runner.py`'s `_runner`, passing `workflows=WorkflowRepository(session)` into `ExecutionRunner` and a `FakeSandbox` whose `steps` default to actually executing the script (the seed workflows must really run, so use `SubprocessSandbox()` for the tests that exercise a seed, and the fake for the poisoned-workflow tests). Check the exact name and shape of `checks["inputs_unmodified"]` in `backend/ley_khaa/executor/validator.py` before asserting on it, and use whatever key that module actually sets.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_runner_registry.py -v
```

Expected: `TypeError: __init__() got an unexpected keyword argument 'workflows'`.

- [ ] **Step 3: Accept the registry in the runner**

In `backend/ley_khaa/executor/runner.py`, extend `__init__`:

```python
    def __init__(
        self,
        *,
        llm: LLMClient,
        messages: MessageRepository,
        sandbox: SandboxRunner | None = None,
        workspace_root: Path | str | None = None,
        workflows: WorkflowRepository | None = None,
    ) -> None:
        self.synthesizer = Synthesizer(llm)
        self.messages = messages
        self._sandbox = sandbox
        self.workspace_root = Path(workspace_root or settings.workspace_root)
        # None is a supported configuration: without a registry the runner is
        # exactly the Phase 3 runner, which is what every existing test builds.
        self.workflows = workflows
        self.matcher = RegistryMatcher(workflows, llm) if workflows is not None else None
```

- [ ] **Step 4: Add the lane**

Still in `run()`, replace the block from `workspace.write_inputs(resolved)` through the start of the attempt loop:

```python
        workspace.write_inputs(resolved)
        target = f"deliverable/{deliverable_filename(spec.output_format)}"

        match = self.matcher.match(spec, resolved) if self.matcher is not None else None
        # Bind under the workflow's role names when one matched, and under the
        # spec's own input names otherwise. input_hashes is computed AFTER this,
        # and again after any rewrite below: hashing a params.json that is about
        # to change makes the validator report the script tampered with its
        # inputs, which is both false and misleading.
        input_hashes = self._bind(workspace, spec, resolved, match, target)

        attempts: list[dict] = []
        lane = "synthesis"
        workflow_record: dict | None = None
        verdict = Verdict(ok=False, reason=_SYNTHESIS_FAILED, checks={})

        if match is not None:
            number = first_attempt
            verdict, result = self._run_workflow(workspace, match, number, spec, input_hashes)
            attempts.append(
                {
                    "attempt": number,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "timed_out": result.timed_out,
                    "ok": verdict.ok,
                    "reason": verdict.reason,
                    "checks": verdict.checks,
                    "reasoning": f"cached workflow {match.workflow.name}, no model call",
                    "stderr_tail": result.stderr[-_STDERR_IN_MANIFEST:],
                }
            )
            workflow_record = {
                "name": match.workflow.name,
                "sha256": match.workflow.source_sha256,
                "matched_by": match.matched_by,
                "binding": dict(match.binding),
                "quarantined": not verdict.ok,
            }
            if verdict.ok:
                self.workflows.record_success(
                    match.workflow.name,
                    # Only a phrasing the model found is worth learning; a
                    # fingerprint hit already knew this operation.
                    learned_alias=(
                        normalize_operation(spec.operation)
                        if match.matched_by == "model"
                        else None
                    ),
                )
                workspace.write_run_script(number)
                self._write_manifest(
                    workspace, row, spec, resolved=resolved, attempts=attempts,
                    verdict=verdict, earlier_attempts=first_attempt - 1,
                    lane="registry", workflow=workflow_record,
                )
                return ExecutionOutcome(verdict, str(workspace.root), len(attempts))

            # Proven code that just produced a wrong answer is not proven any
            # more. Quarantine it, re-bind under the spec's names, and let
            # synthesis rescue this run with its own full attempt budget.
            self.workflows.record_failure(match.workflow.name)
            first_attempt = number + 1
            input_hashes = self._bind(workspace, spec, resolved, None, target)
            verdict = Verdict(ok=False, reason=_SYNTHESIS_FAILED, checks={})

        previous: SynthesizedScript | None = None
        last: SandboxResult | None = None
```

Then leave the existing synthesis loop untouched, and pass the new fields at the bottom:

```python
        self._write_manifest(
            workspace, row, spec, resolved=resolved, attempts=attempts, verdict=verdict,
            earlier_attempts=first_attempt - 1 - len(attempts) + len([a for a in attempts if a]),
            lane=lane, workflow=workflow_record,
        )
```

Keep `earlier_attempts` as it already is (`first_attempt - 1` captured **before** the cached attempt shifted it) — hold that original value in a local at the top of `run()` and use it in both manifest writes, rather than recomputing it. Name it `earlier = first_attempt - 1` right after `first_attempt` is assigned.

- [ ] **Step 5: Add the two helpers**

```python
    def _bind(
        self,
        workspace: Workspace,
        spec: TaskSpec,
        resolved: list[ResolvedInput],
        match: Match | None,
        target: str,
    ) -> dict[str, str]:
        """Write params.json for whichever lane is about to run, then hash.

        Returns the input hashes, so callers cannot forget to recompute them
        after a rewrite — the failure mode is a false "the script modified its
        inputs" verdict.
        """
        binding = (
            dict(match.binding)
            if match is not None
            else {item.name: item.filename for item in resolved}
        )
        workspace.write_params(inputs=binding, output=target, seed=catalog.CATALOG_SEED)
        return workspace.input_hashes()

    def _run_workflow(
        self,
        workspace: Workspace,
        match: Match,
        number: int,
        spec: TaskSpec,
        input_hashes: dict[str, str],
    ) -> tuple[Verdict, SandboxResult]:
        """Run frozen, proven source. No model, no repair.

        There is deliberately no repair loop here: a cached script is proven
        code, so a failure means it is wrong for THIS request, and re-running it
        unchanged would fail identically. Repairing it would also mean the
        registry's source no longer matches what ran.
        """
        path = workspace.write_generator(number, match.workflow.source)
        result = self.sandbox.run(
            script=path, workspace=workspace.root, timeout_s=settings.sandbox_timeout_seconds
        )
        return validate(spec, workspace, result, input_hashes), result
```

`SandboxUnavailable` is deliberately **not** caught here: it propagates to the same handler the synthesis lane already has, which writes the manifest before re-raising.

- [ ] **Step 6: Widen `_write_manifest`**

Add `lane: str = "synthesis"` and `workflow: dict | None = None` keyword parameters, replace the hardcoded `"lane": "synthesis"` with `"lane": lane`, add `"workflow": workflow`, and make the models line honest:

```python
                # On the cached lane no model wrote this script, and the
                # manifest may not imply one did.
                "models": {
                    Stage.SYNTHESIS.value: (
                        None if lane == "registry" else _synthesis_author(self.synthesizer.llm)
                    )
                },
```

Note the `test_a_failing_cached_workflow...` test expects `lane == "synthesis"` with a `workflow` record present, so a run rescued by synthesis credits the model that actually wrote the winning script. That is the rule working, not an exception to it.

- [ ] **Step 7: Thread the repository through**

- `orchestrator/driver.py`: `TaskDriver.__init__` gains `workflows: WorkflowRepository | None = None` and passes it into `ExecutionRunner(...)`.
- `orchestrator/orchestrator.py`: `Orchestrator.__init__` gains `workflows: WorkflowRepository | None = None` and passes it into `TaskDriver(...)`.
- `api/app.py`: `build_orchestrator` passes `workflows=WorkflowRepository(session)`.

- [ ] **Step 8: Run the whole suite**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest -q
```

Expected: all pass, including the Phase 3 golden end-to-end tests. Those run through `client`, which disables startup seeding, so they stay on the synthesis lane and their `manifest["lane"] == "synthesis"` assertion still holds. **If a golden test now reports `lane == "registry"`, seeding is running under test** — check Step 5 of Task 7.

- [ ] **Step 9: Commit**

```bash
git add backend/ley_khaa backend/tests
git commit -m "feat(executor): run proven workflows on the registry fast path

A matched request runs frozen source in the same sandbox and is judged by the
same validator; the manifest records the lane, the workflow hash, and the
binding, and credits no model because none wrote the script. A failure
quarantines the workflow and hands the run to synthesis with a full attempt
budget."
```

---

### Task 9: Promotion — a proven bundle becomes a workflow

**Files:**
- Create: `backend/ley_khaa/registry/promote.py`
- Modify: `backend/ley_khaa/api/app.py`, `backend/ley_khaa/api/schemas.py`
- Test: `backend/tests/test_promote.py`

**Interfaces:**
- Consumes: `_contained()` and `_bundle_root()` from `api/app.py`, `Workspace.read_manifest`, `WorkflowRepository`.
- Produces: `promote(session, *, task_id, name, description, root: Path) -> WorkflowRow`, `NotPromotable(Exception)`, and `POST /tasks/{task_id}/promote`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_promote.py`:

```python
def test_a_passing_bundle_becomes_a_workflow(client, session, ...):
    """Promotion freezes the exact source that passed, with its roles taken from
    the params.json binding that run used."""
```

Write these cases in full, driving through the API with `client`:

1. **`test_promoting_a_passing_task_freezes_the_winning_source`** — run a task to `done` (reuse the golden conversation helper from `tests/test_executor_end_to_end.py`), `POST /tasks/{id}/promote` with `{"name": "universe_check", "description": "..."}`, then assert the created row's `source` equals `generator/attempt_1.py`'s text, `origin == "promoted"`, `promoted_from_task_id == task_id`, and `inputs` roles equal the keys of that bundle's `inputs/params.json`.
2. **`test_the_roles_come_from_the_binding_that_actually_ran`** — assert `[r["role"] for r in row.inputs] == list(params["inputs"])` and that each role's `suffixes` is the suffix of the file it was bound to.
3. **`test_a_failed_task_cannot_be_promoted`** — a task whose `execution_verdict["ok"]` is false returns **409** and creates no row.
4. **`test_a_task_with_no_bundle_cannot_be_promoted`** — 404.
5. **`test_a_duplicate_name_is_refused`** — second promote with the same name returns **409**.
6. **`test_promotion_never_reads_outside_the_bundle`** — plant `os.symlink("/etc/hosts", root/"generator"/"attempt_9.py")`, point the manifest's winning attempt at it, and assert the promote returns an error and creates no row. This is the Task 11 (Phase 3) ruling applied to a new reader of bundle contents.
7. **`test_the_promoted_workflow_serves_the_next_matching_request`** — promote, then run a second task with the same operation/format/arity and assert its manifest has `lane == "registry"` and `workflow.name` equal to the promoted name.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_promote.py -v
```

Expected: 404 from an unregistered route.

- [ ] **Step 3: Implement promotion**

Create `backend/ley_khaa/registry/promote.py`:

```python
"""A proven bundle becomes a permanent capability (spec §5.6).

Promotion is a pure copy. Nothing here rewrites, reformats or re-synthesizes the
source: the code that becomes a workflow is byte-for-byte the code that passed
validation, which is the only reason source_sha256 and the bundle's audit trail
mean anything.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..executor.workspace import Workspace
from ..persistence.orm import WorkflowRow
from ..persistence.workflow_repository import WorkflowRepository
from .fingerprint import normalize_operation

NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


class NotPromotable(Exception):
    """This bundle cannot become a workflow, and why."""


def promote(
    session: Session,
    *,
    task_id: str,
    name: str,
    description: str,
    root: Path,
    contained,
) -> WorkflowRow:
    """Freeze the winning attempt of `root` as a workflow named `name`.

    `contained` is api.app._contained, passed in rather than imported so this
    module does not depend on the API layer. Every path read below goes through
    it: the workspace is written by untrusted generator code, and a symlink
    planted in generator/ would otherwise be promoted into the registry and run
    on every future match.
    """
    if not NAME.match(name or ""):
        raise NotPromotable(
            "a workflow name must be lowercase letters, digits and underscores, 3-64 characters"
        )

    manifest = Workspace(root).read_manifest()
    if not (manifest.get("verdict") or {}).get("ok"):
        raise NotPromotable("only a run that passed validation can be promoted")

    winning = [a for a in manifest.get("attempts") or [] if a.get("ok")]
    if not winning:
        raise NotPromotable("this bundle has no passing attempt to promote")
    attempt_path = contained(root, root / "generator" / f"attempt_{winning[-1]['attempt']}.py")
    if attempt_path is None or not attempt_path.is_file():
        raise NotPromotable("the winning attempt is not a readable file inside this bundle")

    params_path = contained(root, root / "inputs" / "params.json")
    if params_path is None or not params_path.is_file():
        raise NotPromotable("this bundle has no params.json, so its roles are unknown")
    binding = json.loads(params_path.read_text(encoding="utf-8")).get("inputs") or {}
    if not binding:
        raise NotPromotable("this bundle bound no inputs")

    spec = manifest.get("spec") or {}
    return WorkflowRepository(session).create(
        name=name,
        description=description,
        operation_aliases=[normalize_operation(spec.get("operation", ""))],
        output_format=spec.get("output_format", ""),
        # Roles are the binding this run actually used, in its order — the names
        # the frozen script reads out of params.json.
        inputs=[
            {"role": role, "suffixes": [Path(filename).suffix.lower()]}
            for role, filename in binding.items()
        ],
        source=attempt_path.read_text(encoding="utf-8"),
        origin="promoted",
        promoted_from_task_id=task_id,
    )
```

- [ ] **Step 4: Add the route**

In `backend/ley_khaa/api/schemas.py`:

```python
class PromoteIn(BaseModel):
    name: str
    description: str = ""


class WorkflowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    operation_aliases: list[str]
    output_format: str
    inputs: list[dict[str, Any]]
    origin: str
    promoted_from_task_id: str | None = None
    runs_ok: int
    runs_failed: int
    quarantined: bool
    source_sha256: str
```

In `backend/ley_khaa/api/app.py`:

```python
@app.post("/tasks/{task_id}/promote", response_model=WorkflowOut)
def promote_task_workflow(
    task_id: str, body: PromoteIn, session: Session = Depends(get_session)
) -> WorkflowOut:
    root = _bundle_root(session, task_id)
    try:
        row = promote(
            session,
            task_id=task_id,
            name=body.name,
            description=body.description,
            root=root,
            contained=_contained,
        )
    except DuplicateWorkflow:
        raise HTTPException(status_code=409, detail=f"a workflow named {body.name!r} already exists")
    except NotPromotable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return WorkflowOut.model_validate(row)
```

- [ ] **Step 5: Run them and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_promote.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/registry/promote.py backend/ley_khaa/api backend/tests/test_promote.py
git commit -m "feat(registry): promote a proven bundle into a workflow

A pure copy of the winning attempt, with roles taken from the params.json
binding that run actually used. Every path is read through _contained(), so a
symlink planted by generator code cannot be promoted into the registry and then
run on every future match."
```

---

### Task 10: The registry API

**Files:**
- Modify: `backend/ley_khaa/api/app.py`
- Test: `backend/tests/test_registry_api.py`

**Interfaces:**
- Produces: `GET /registry` → `list[WorkflowOut]`; `POST /registry/{name}/unquarantine` → `WorkflowOut`; `DELETE /registry/{name}` → 204.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_registry_api.py` covering:

1. **`test_the_registry_lists_seeds_and_promotions_with_their_usage`** — after `ensure_seed_workflows`, `GET /registry` returns both, with `origin`, `runs_ok`, `runs_failed`, `quarantined` and `source_sha256` present.
2. **`test_the_listing_never_leaks_the_source`** — assert `"source"` is not a key of any returned object. The source is LLM-written code and the listing is a browsable page; `source_sha256` identifies it without shipping it.
3. **`test_a_quarantined_workflow_can_be_cleared_by_a_human`** — quarantine via `WorkflowRepository.record_failure`, `POST /registry/{name}/unquarantine`, assert `quarantined is False` and that `runs_failed` is unchanged.
4. **`test_unquarantining_an_unknown_workflow_is_404`**.
5. **`test_deleting_a_workflow_removes_it_from_matching`** — `DELETE /registry/{name}` returns 204, and a later matching request takes the synthesis lane.
6. **`test_deleting_an_unknown_workflow_is_404`**.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_api.py -v
```

Expected: 404s from unregistered routes.

- [ ] **Step 3: Add the routes**

```python
@app.get("/registry", response_model=list[WorkflowOut])
def list_workflows(session: Session = Depends(get_session)) -> list[WorkflowOut]:
    # WorkflowOut deliberately omits `source`: this is a browsable listing, the
    # source is model-written code, and source_sha256 identifies it exactly
    # without putting a program in a page that renders it.
    return [WorkflowOut.model_validate(row) for row in WorkflowRepository(session).list()]


@app.post("/registry/{name}/unquarantine", response_model=WorkflowOut)
def unquarantine_workflow(name: str, session: Session = Depends(get_session)) -> WorkflowOut:
    return WorkflowOut.model_validate(WorkflowRepository(session).unquarantine(name))


@app.delete("/registry/{name}", status_code=204)
def delete_workflow(name: str, session: Session = Depends(get_session)) -> None:
    WorkflowRepository(session).delete(name)
```

The repository raises `KeyError` for an unknown name, and `app.py` already has a `KeyError` handler that returns 404 — no extra handling needed. Verify that handler is registered before relying on it.

- [ ] **Step 4: Run them and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_registry_api.py -v
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ley_khaa/api/app.py backend/tests/test_registry_api.py
git commit -m "feat(api): list, unquarantine and delete registry workflows

The listing omits source by design: it identifies each workflow by hash rather
than shipping model-written code into a page that renders it."
```

---

### Task 11: Memory fingerprint and repository

**Files:**
- Create: `backend/ley_khaa/memory/__init__.py`, `backend/ley_khaa/memory/fingerprint.py`, `backend/ley_khaa/persistence/memory_repository.py`
- Test: `backend/tests/test_memory_fingerprint.py`, `backend/tests/test_memory_repository.py`

**Interfaces:**
- Produces:
  - `STOPWORDS: frozenset[str]`, `request_fingerprint(texts: list[str]) -> str`
  - `MemoryRepository(session)` with `record(*, project, fingerprint, intent, spec: TaskSpec, task_id) -> MemoryRow` (upsert: touches an existing row instead of inserting a duplicate), `by_fingerprint(project, fingerprint) -> MemoryRow | None`, `for_project(project) -> list[MemoryRow]`, `get(memory_id) -> MemoryRow | None`.

- [ ] **Step 1: Write the failing fingerprint tests**

Create `backend/tests/test_memory_fingerprint.py`:

```python
from ley_khaa.memory.fingerprint import STOPWORDS, request_fingerprint


def test_the_same_request_fingerprints_the_same():
    assert request_fingerprint(["Compare the Bloomberg universe against FactSet"]) == \
        request_fingerprint(["Compare the Bloomberg universe against FactSet"])


def test_politeness_and_word_order_do_not_change_the_fingerprint():
    """A repeat request is rarely typed identically. Courtesy words and order
    are exactly the noise a fingerprint has to see through."""
    a = request_fingerprint(["Hi! Can you compare the Bloomberg universe against FactSet, please?"])
    b = request_fingerprint(["compare FactSet against the bloomberg universe. Thanks!"])
    assert a == b


def test_a_different_request_fingerprints_differently():
    a = request_fingerprint(["compare the bloomberg universe against factset"])
    b = request_fingerprint(["summarise the holdings by sector"])
    assert a != b


def test_bare_numbers_are_dropped_so_a_date_does_not_split_a_repeat():
    """"the usual universe check" arriving on the 3rd and the 10th is one
    remembered request, not two."""
    a = request_fingerprint(["run the universe check for 2026-08-03"])
    b = request_fingerprint(["run the universe check for 2026-08-10"])
    assert a == b


def test_an_empty_request_has_no_fingerprint():
    """Empty must never collide with empty and match every other blank task."""
    assert request_fingerprint([]) == ""
    assert request_fingerprint(["", "   "]) == ""


def test_the_stopword_list_is_pinned():
    """Pinned deliberately: quietly adding a word re-fingerprints every stored
    memory, and every past request silently stops matching."""
    assert "please" in STOPWORDS and "the" in STOPWORDS
    assert "universe" not in STOPWORDS
    assert len(STOPWORDS) == 42
```

Set the final assertion to the real length once the list is written, and keep it.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_memory_fingerprint.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement the fingerprint**

Create `backend/ley_khaa/memory/__init__.py` (empty) and `backend/ley_khaa/memory/fingerprint.py`:

```python
"""Have I been asked this before? — the deterministic half (spec §3.5).

A token SET, not a sequence: a repeat request is rarely typed identically, and
word order carries no meaning here. Paraphrase beyond this is stage 2's job.
"""
from __future__ import annotations

import hashlib
import re

_TOKEN = re.compile(r"[a-z0-9]+")

# Pinned by a test. Adding a word re-fingerprints every stored memory, so every
# request remembered before the change silently stops matching — a cache that
# forgets everything without saying so.
STOPWORDS = frozenset(
    """
    a an the this that these those and or but if then so as of to in on for with
    at by from is are was were be been being do does did can could would should
    will please thanks thank hi hello hey we i you it me my our your
    """.split()
)


def request_fingerprint(texts: list[str]) -> str:
    """The significant tokens of a request, sorted and hashed.

    Bare numbers are dropped so that a date does not split "the usual universe
    check" into a new request every time it runs.
    """
    tokens = {
        token
        for text in texts
        for token in _TOKEN.findall((text or "").lower())
        if token not in STOPWORDS and not token.isdigit()
    }
    if not tokens:
        # Never let two blank requests fingerprint together — an empty hash
        # would match every other empty one and hand them each other's spec.
        return ""
    return hashlib.sha256(" ".join(sorted(tokens)).encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run them, fix the pinned length, watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_memory_fingerprint.py -v
```

- [ ] **Step 5: Write the failing repository tests**

Create `backend/tests/test_memory_repository.py`:

```python
def test_recording_the_same_request_twice_increments_rather_than_duplicates(session):
    """times_seen is what the dial reads. A duplicate row would keep every
    repeat at 1 and the dial would never learn anything."""
    repo = MemoryRepository(session)
    first = repo.record(project="default", fingerprint="abc", intent="i", spec=_spec(), task_id="t1")
    second = repo.record(project="default", fingerprint="abc", intent="i", spec=_spec(), task_id="t2")

    assert first.id == second.id
    assert second.times_seen == 2
    # The first task keeps the credit: it is the run that proved the spec.
    assert second.source_task_id == "t1"
    assert len(repo.for_project("default")) == 1


def test_memory_is_scoped_to_a_project(session):
    repo = MemoryRepository(session)
    repo.record(project="acme", fingerprint="abc", intent="i", spec=_spec(), task_id="t1")

    assert repo.by_fingerprint("acme", "abc") is not None
    assert repo.by_fingerprint("globex", "abc") is None
    assert repo.for_project("globex") == []


def test_the_spec_round_trips(session):
    repo = MemoryRepository(session)
    row = repo.record(project="default", fingerprint="abc", intent="i", spec=_spec(), task_id="t1")

    assert TaskSpec.model_validate(row.spec).operation == _spec().operation
```

- [ ] **Step 6: Implement the repository**

Create `backend/ley_khaa/persistence/memory_repository.py`:

```python
"""Task memory storage (spec §5.14)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..interpreter.spec import TaskSpec
from .orm import MemoryRow


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self, *, project: str, fingerprint: str, intent: str, spec: TaskSpec, task_id: str
    ) -> MemoryRow:
        """Remember a proven spec, or note that we have seen this again.

        An upsert, not an insert: times_seen is what the autonomy dial reads,
        and a second row for the same request would keep every repeat at 1 —
        familiarity would never accumulate and the feature would do nothing
        while appearing to work.
        """
        existing = self.by_fingerprint(project, fingerprint)
        if existing is not None:
            existing.times_seen += 1
            existing.last_seen_at = datetime.now(timezone.utc)
            # source_task_id is NOT updated: it points at the run that first
            # proved this spec, which is what the dashboard links to.
            self.session.commit()
            return existing

        row = MemoryRow(
            id=str(uuid.uuid4()),
            project=project,
            fingerprint=fingerprint,
            intent=intent,
            spec=spec.model_dump(mode="json"),
            source_task_id=task_id,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def by_fingerprint(self, project: str, fingerprint: str) -> MemoryRow | None:
        if not fingerprint:
            return None
        return self.session.scalars(
            select(MemoryRow).where(
                MemoryRow.project == project, MemoryRow.fingerprint == fingerprint
            )
        ).one_or_none()

    def for_project(self, project: str) -> list[MemoryRow]:
        return list(
            self.session.scalars(
                select(MemoryRow)
                .where(MemoryRow.project == project)
                .order_by(MemoryRow.last_seen_at.desc())
            )
        )

    def get(self, memory_id: str) -> MemoryRow | None:
        return self.session.get(MemoryRow, memory_id)
```

- [ ] **Step 7: Run both files and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_memory_fingerprint.py tests/test_memory_repository.py -v
```

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa/memory backend/ley_khaa/persistence/memory_repository.py backend/tests
git commit -m "feat(memory): add the request fingerprint and its repository

A pinned stopword list and a sorted token set, so politeness and word order do
not split a repeat, and a date in the text does not make 'the usual universe
check' a new request every week. Recording upserts: a duplicate row would keep
times_seen at 1 and the dial would never see familiarity accumulate."
```

---

### Task 12: Recall — memory short-circuits the interpreter

**Files:**
- Create: `backend/ley_khaa/memory/models.py`, `backend/ley_khaa/memory/matcher.py`
- Modify: `backend/ley_khaa/llm/heuristic.py`, `backend/ley_khaa/persistence/repository.py`, `backend/ley_khaa/orchestrator/driver.py`, `backend/ley_khaa/orchestrator/orchestrator.py`, `backend/ley_khaa/api/app.py`
- Test: `backend/tests/test_memory_matcher.py`, `backend/tests/test_driver_memory.py`

**Interfaces:**
- Produces:
  - `MemoryDecision(BaseModel)`: `memory_id: str | None`, `confidence: float`, `reason: str`
  - `MemoryMatcher(memories: MemoryRepository, llm: LLMClient)` with `recall(project: str, texts: list[str]) -> MemoryRow | None` and `CONFIDENCE_FLOOR = 0.8`
  - `TaskRepository.save_memory_hit(task_id, *, source_task_id: str, familiarity: int) -> TaskRow`
  - `TaskDriver(..., memories: MemoryRepository | None = None)`; `Orchestrator(..., memories=...)`

- [ ] **Step 1: Write the failing matcher tests**

Create `backend/tests/test_memory_matcher.py`, mirroring `test_registry_matcher.py` case for case:

1. **`test_a_fingerprint_hit_never_calls_the_model`** — record a memory, recall the same text, assert the row comes back and `llm.calls == []`.
2. **`test_a_paraphrase_is_found_by_the_model`** — `MemoryDecision(memory_id=<id>, confidence=0.9, ...)` returns that row.
3. **`test_a_low_confidence_answer_is_not_a_match`** — `CONFIDENCE_FLOOR - 0.01` returns `None`.
4. **`test_a_model_naming_an_unknown_memory_is_not_a_match`**.
5. **`test_a_memory_from_another_project_is_never_recalled`** — record under `acme`, recall under `globex`, assert `None` **and** that the model was never asked (nothing in scope to match).
6. **`test_a_broken_model_call_is_a_miss_not_a_crash`**.
7. **`test_the_offline_stand_in_answers_no_match`** — `HeuristicLLM` returns `None`.
8. **`test_an_empty_request_never_recalls`** — empty fingerprint must not match a stored empty one.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_memory_matcher.py -v
```

- [ ] **Step 3: Implement the matcher**

Create `backend/ley_khaa/memory/models.py`:

```python
from pydantic import BaseModel


class MemoryDecision(BaseModel):
    """Stage 2's answer. `memory_id` is an id from the list the model was shown,
    or null — null is a first-class answer."""

    memory_id: str | None
    confidence: float
    reason: str
```

Create `backend/ley_khaa/memory/matcher.py`:

```python
"""Have I been asked this before? (spec §3.5)

Same two stages and same contract as the registry matcher: deterministic first,
one cheap call on a miss, no match always legal. A miss costs one interpreter
call — the path that worked before this module existed.
"""
from __future__ import annotations

import logging

from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.memory_repository import MemoryRepository
from ..persistence.orm import MemoryRow
from .fingerprint import request_fingerprint
from .models import MemoryDecision

logger = logging.getLogger(__name__)

CONFIDENCE_FLOOR = 0.8

SYSTEM = """You decide whether a new request is a repeat of one already handled.

You are given the new request and a list of past requests with their ids. Answer with the id
of the past request that is THE SAME standing request — same work, same shape, differing only
in wording or in which day it is being run for. Otherwise answer null.

Say null unless you are confident. A wrong match reuses another request's specification and
the work is done to the wrong shape. A null costs only that the request is read fresh, which
is the normal path."""


class MemoryMatcher:
    def __init__(self, memories: MemoryRepository, llm: LLMClient) -> None:
        self.memories = memories
        self.llm = llm

    def recall(self, project: str, texts: list[str]) -> MemoryRow | None:
        try:
            return self._recall(project, texts)
        except Exception:
            logger.exception("memory recall failed; interpreting from scratch")
            return None

    def _recall(self, project: str, texts: list[str]) -> MemoryRow | None:
        fingerprint = request_fingerprint(texts)
        if not fingerprint:
            return None

        exact = self.memories.by_fingerprint(project, fingerprint)
        if exact is not None:
            return exact

        # Scoped to the project, always. A spec remembered for one client must
        # never be reachable from another's conversation.
        known = self.memories.for_project(project)
        if not known:
            return None

        decision = self.llm.parse(
            choice=model_for(Stage.MEMORY_MATCH),
            system=SYSTEM,
            user=_prompt(texts, known),
            output_format=MemoryDecision,
        )
        if not decision.memory_id or decision.confidence < CONFIDENCE_FLOOR:
            return None
        # Untrusted output: the id must be one we actually showed it.
        return next((row for row in known if row.id == decision.memory_id), None)


def _prompt(texts: list[str], known: list[MemoryRow]) -> str:
    lines = ["## New request", *texts, "", "## Past requests"]
    lines.extend(f"- [{row.id}] {row.intent} (seen {row.times_seen}x)" for row in known)
    return "\n".join(lines)
```

In `backend/ley_khaa/llm/heuristic.py`, add the offline answer beside the `RegistryDecision` one:

```python
        if output_format is MemoryDecision:
            # Fingerprint-only offline, for the same reason as RegistryDecision.
            return MemoryDecision(memory_id=None, confidence=0.0, reason="offline: no model match")
```

- [ ] **Step 4: Run them and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_memory_matcher.py -v
```

- [ ] **Step 5: Write the failing driver tests**

Create `backend/tests/test_driver_memory.py`:

```python
def test_a_remembered_request_skips_the_interpreter(session, ...):
    """The claim memory exists to make: the second identical request costs no
    interpreter call."""
```

Cover, driving through `TaskDriver.advance` with a `FakeLLM` that would raise or exhaust if the interpreter ran:

1. **`test_a_remembered_request_skips_the_interpreter`** — a recorded memory whose fingerprint matches; assert the task reaches `interpreted` (or beyond) with the remembered `operation`, and that no interpreter call was made.
2. **`test_the_remembered_spec_points_at_this_task_s_messages`** — assert `spec["source_message_ids"]` equals the CURRENT task's message ids, not the remembered task's. A remembered id would make the bundle cite messages from another conversation.
3. **`test_inputs_are_re_resolved_not_remembered`** — assert the remembered spec's `inputs` are input NAMES and that the executed bundle's `manifest["inputs"]` files come from this task's own attachments/catalog.
4. **`test_a_memory_hit_is_recorded_on_the_task`** — `remembered_from_task_id` and `familiarity` are set.
5. **`test_a_task_is_remembered_only_when_it_passes`** — a task that reaches `done` with `ok` is recorded; one that ends in `needs_clarification` or `failed` is not.
6. **`test_a_second_identical_request_increments_familiarity`** — run the same request twice; the second's `familiarity` is 1 and the memory row's `times_seen` is 2.
7. **`test_with_no_memory_repository_the_driver_behaves_exactly_as_before`**.

- [ ] **Step 6: Wire recall into the driver**

In `backend/ley_khaa/persistence/repository.py`:

```python
    def save_memory_hit(self, task_id: str, *, source_task_id: str, familiarity: int) -> TaskRow:
        """Record that this spec came from memory rather than the interpreter.

        familiarity feeds the autonomy dial; source_task_id is what the
        dashboard links back to so a human can see what is being reused.
        """
        row = self._row(task_id)
        row.remembered_from_task_id = source_task_id
        row.familiarity = familiarity
        self.session.commit()
        return row
```

In `backend/ley_khaa/orchestrator/driver.py`, extend `__init__` with `memories: MemoryRepository | None = None`, build `self.memory = MemoryMatcher(memories, llm) if memories is not None else None`, keep `self.memories = memories`, and restructure `_interpret`:

```python
    def _interpret(self, row: TaskRow) -> bool:
        remembered = self._recall(row)
        if remembered is not None:
            # Only the shape is reused. source_message_ids is re-pointed at THIS
            # task's messages, and `inputs` are names that the resolver resolves
            # against this task at execution time — last week's spec must not be
            # able to quietly reuse last week's file.
            spec = TaskSpec.model_validate(remembered.spec).model_copy(
                update={"source_message_ids": list(row.source_message_ids or [])}
            )
            self.repo.save_memory_hit(
                row.id,
                source_task_id=remembered.source_task_id,
                familiarity=remembered.times_seen,
            )
            return self._after_spec(row, spec)

        try:
            spec = self.interpreter.interpret(row)
        except MalformedSpec:
            ...unchanged...
        except Exception:
            ...unchanged...

        return self._after_spec(row, spec)

    def _recall(self, row: TaskRow):
        if self.memory is None:
            return None
        rows = self.messages.get_many(list(row.source_message_ids or []))
        return self.memory.recall(row.project, [r.text for r in rows])

    def _after_spec(self, row: TaskRow, spec: TaskSpec) -> bool:
        """Everything that happens once a spec exists, however it was obtained.

        Extracted so the remembered path and the interpreted path cannot drift:
        a remembered spec with missing_fields must still stop and ask.
        """
        self.repo.save_spec(row.id, spec)
        if spec.missing_fields and (row.clarification_rounds or 0) < _MAX_CLARIFICATION_ROUNDS:
            self.repo.set_open_question(row.id, _question_for(spec.missing_fields))
            return self.repo.claim(
                row.id, expected=TaskState.CLASSIFIED, target=TaskState.NEEDS_CLARIFICATION
            )
        self.repo.set_open_question(row.id, None)
        return self.repo.claim(
            row.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED
        )
```

`TaskDriver` needs `self.messages = messages` if it does not already keep it — check before adding.

Then record on a passing finish, in `_validate`:

```python
        if verdict.get("ok"):
            self.repo.set_open_question(row.id, None)
            claimed = self.repo.claim(
                row.id, expected=TaskState.VALIDATING, target=TaskState.DONE
            )
            if claimed:
                # Only a proven run is remembered — the same rule promotion
                # follows. Recording before the claim would remember a spec for
                # a task another caller had already moved.
                self._remember(row)
            return claimed
```

```python
    def _remember(self, row: TaskRow) -> None:
        if self.memories is None or not row.spec:
            return
        try:
            rows = self.messages.get_many(list(row.source_message_ids or []))
            texts = [r.text for r in rows]
            fingerprint = request_fingerprint(texts)
            if not fingerprint:
                return
            spec = TaskSpec.model_validate(row.spec)
            self.memories.record(
                project=row.project,
                fingerprint=fingerprint,
                intent=spec.intent,
                spec=spec,
                task_id=row.id,
            )
        except Exception:
            # Remembering is a bonus, never a reason for a finished task to fail.
            logger.exception("could not remember task %s", row.id)
```

Thread `memories=` through `Orchestrator.__init__` into `TaskDriver`, and pass `memories=MemoryRepository(session)` in `build_orchestrator`.

- [ ] **Step 7: Run the whole suite**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa backend/tests
git commit -m "feat(memory): recall a remembered spec instead of re-interpreting

A recognised repeat skips the interpreter entirely. Only the shape is reused:
source_message_ids are re-pointed at this task's messages and inputs stay names
the resolver re-resolves against this task, so last week's spec cannot quietly
reuse last week's file. Recording happens only on a proven, claimed DONE."
```

---

### Task 13: Familiarity feeds the dial

**Files:**
- Modify: `backend/ley_khaa/autonomy/engine.py`, `backend/ley_khaa/orchestrator/driver.py`
- Test: `backend/tests/test_autonomy_engine.py`

**Interfaces:**
- Produces: `recommend(spec, *, candidate_missing_fields=None, familiarity: int = 0)`; constants `_FAMILIARITY_BONUS = 0.05`, `_MAX_FAMILIARITY_BONUS = 0.15`.

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_autonomy_engine.py`:

```python
def test_familiarity_raises_confidence_and_says_so():
    spec = _spec(certainty=0.75)
    fresh = recommend(spec)
    seen = recommend(spec, familiarity=2)

    assert seen.confidence == round(fresh.confidence + 0.10, 4)
    assert "done this 2 times before" in seen.reason


def test_the_familiarity_bonus_is_capped():
    """A request repeated a hundred times is not more certain than one repeated
    three times — repetition is weak evidence and must stay weak."""
    spec = _spec(certainty=0.5)
    assert recommend(spec, familiarity=3).confidence == recommend(spec, familiarity=99).confidence
    assert recommend(spec, familiarity=99).confidence == round(recommend(spec).confidence + 0.15, 4)


def test_familiarity_cannot_carry_an_incomplete_spec_to_auto():
    """The whole bonus is smaller than one missing field's penalty. Repetition
    must never be able to push a spec with known gaps into acting alone."""
    spec = _spec(certainty=1.0, missing_fields=["output_format"])
    assert recommend(spec, familiarity=99).mode is not AutonomyMode.AUTO


def test_zero_familiarity_changes_nothing():
    spec = _spec(certainty=0.9)
    assert recommend(spec, familiarity=0).reason == recommend(spec).reason


def test_the_familiarity_numbers_are_pinned():
    """These two numbers are the policy, like the four already pinned here."""
    from ley_khaa.autonomy.engine import _FAMILIARITY_BONUS, _MAX_FAMILIARITY_BONUS

    assert (_FAMILIARITY_BONUS, _MAX_FAMILIARITY_BONUS) == (0.05, 0.15)
    assert _MAX_FAMILIARITY_BONUS < _MISSING_FIELD_PENALTY
```

Match the existing `_spec()` helper's signature in that file rather than inventing one.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_autonomy_engine.py -v
```

Expected: `TypeError: recommend() got an unexpected keyword argument 'familiarity'`.

- [ ] **Step 3: Implement it**

In `backend/ley_khaa/autonomy/engine.py`, beside the existing penalty constants:

```python
# --- confidence bonuses ---------------------------------------------------
# Repetition is weak evidence, and it is capped so it stays weak: the whole
# bonus is smaller than one missing field's penalty, so a request that has been
# run twenty times still cannot act alone while it has a known gap.
_FAMILIARITY_BONUS = 0.05
_MAX_FAMILIARITY_BONUS = 0.15
```

Change the signature and thread it through:

```python
def recommend(
    spec: TaskSpec,
    *,
    candidate_missing_fields: list[str] | None = None,
    familiarity: int = 0,
) -> Recommendation:
    confidence, confidence_clauses = _confidence(
        spec, candidate_missing_fields or [], familiarity
    )
```

and in `_confidence`, after the existing penalties:

```python
def _confidence(
    spec: TaskSpec, candidate_missing: list[str], familiarity: int = 0
) -> tuple[float, list[str]]:
    ...
    if familiarity > 0:
        score += min(_MAX_FAMILIARITY_BONUS, _FAMILIARITY_BONUS * familiarity)
        clauses.append(f"I've done this {familiarity} times before")
    return _clamp(score), clauses
```

In `driver.py`'s `_gate`, pass it:

```python
        recommendation = recommend(
            spec,
            candidate_missing_fields=list(candidate.missing_fields or []) if candidate else [],
            familiarity=row.familiarity or 0,
        )
```

- [ ] **Step 4: Run them and watch them pass**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_autonomy_engine.py -q
```

- [ ] **Step 5: Commit**

```bash
git add backend/ley_khaa/autonomy backend/ley_khaa/orchestrator/driver.py backend/tests/test_autonomy_engine.py
git commit -m "feat(autonomy): let familiarity raise confidence, within a cap

+0.05 per remembered run, capped at +0.15 — deliberately smaller than one
missing field's penalty, so repetition can help a good spec over the line but
can never push a spec with known gaps into acting alone."
```

---

### Task 14: Dashboard — promote a bundle, and show a remembered spec

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/BundlePanel.tsx`, `frontend/src/TaskDetail.tsx`
- Test: `frontend/src/BundlePanel.test.tsx`, `frontend/src/TaskDetail.test.tsx`

**Interfaces:**
- Consumes: `POST /tasks/{id}/promote`, `Task.remembered_from_task_id`, `Task.familiarity`.
- Produces: `promoteTask(id, name, description) -> Promise<Workflow>`, `type Workflow`, and the two `Task` fields in `api.ts`.

There is no router in this app — `App.tsx` renders sections directly. Follow that: no routing library, no navigation abstraction.

- [ ] **Step 1: Write the failing tests**

In `frontend/src/BundlePanel.test.tsx`:

```tsx
it("offers promotion only for a bundle that passed", async () => {
  // A failed run has nothing worth freezing; offering the button would invite a
  // 409 the human cannot act on.
  mockBundle({ manifest: { verdict: { ok: false } } });
  render(<BundlePanel taskId="t1" />);
  expect(await screen.findByText(/generator/i)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /promote/i })).toBeNull();
});

it("promotes a passing bundle under a name the human chooses", async () => {
  mockBundle({ manifest: { verdict: { ok: true } } });
  const promote = vi.fn().mockResolvedValue({ name: "universe_check" });
  render(<BundlePanel taskId="t1" />);

  fireEvent.click(await screen.findByRole("button", { name: /promote/i }));
  fireEvent.change(screen.getByLabelText(/name/i), { target: { value: "universe_check" } });
  fireEvent.click(screen.getByRole("button", { name: /save/i }));

  await waitFor(() => expect(promote).toHaveBeenCalledWith("t1", "universe_check", ""));
});

it("shows why a promotion was refused", async () => {
  // A duplicate name is the common case, and it is fixable — the human needs to
  // read it, not watch the dialog close on nothing.
  ...assert the 409 detail is rendered...
});

it("says a cached run took the fast path and names the workflow", async () => {
  mockBundle({ manifest: { lane: "registry", workflow: { name: "set_difference", matched_by: "fingerprint" } } });
  render(<BundlePanel taskId="t1" />);
  expect(await screen.findByText(/set_difference/)).toBeTruthy();
  expect(screen.getByText(/no model/i)).toBeTruthy();
});
```

In `frontend/src/TaskDetail.test.tsx`:

```tsx
it("marks a task whose spec came from memory", () => {
  render(<TaskDetail task={task({ remembered_from_task_id: "t0", familiarity: 3 })} onChanged={() => {}} />);
  expect(screen.getByText(/remembered/i)).toBeTruthy();
  expect(screen.getByText(/3/)).toBeTruthy();
});

it("says nothing about memory for a freshly interpreted task", () => {
  render(<TaskDetail task={task({ remembered_from_task_id: null, familiarity: 0 })} onChanged={() => {}} />);
  expect(screen.queryByText(/remembered/i)).toBeNull();
});
```

Follow the mocking style already used in these two files (they mock `./api`); do not introduce a different approach.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd frontend && npm test
```

- [ ] **Step 3: Extend `api.ts`**

```ts
export type Workflow = {
  name: string;
  description: string;
  operation_aliases: string[];
  output_format: string;
  inputs: { role: string; suffixes: string[] }[];
  origin: string;
  promoted_from_task_id: string | null;
  runs_ok: number;
  runs_failed: number;
  quarantined: boolean;
  source_sha256: string;
};

export async function promoteTask(
  id: string,
  name: string,
  description: string,
): Promise<Workflow> {
  const res = await fetch(`${BASE}/tasks/${id}/promote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description }),
  });
  if (!res.ok) {
    // The 409 detail is the actionable part — a taken name, or a run that did
    // not pass. Swallowing it leaves the human with a dialog that just fails.
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `promoteTask failed: ${res.status}`);
  }
  return res.json();
}
```

and add to `type Task`:

```ts
  remembered_from_task_id: string | null;
  familiarity: number;
```

Add the same two fields to `TaskOut` in `backend/ley_khaa/api/schemas.py` — the frontend type is only true if the API actually sends them. Assert this in a backend test (`GET /tasks/{id}` includes both keys) rather than trusting it.

- [ ] **Step 4: Add the promote control and the lane line to `BundlePanel.tsx`**

Render, above the generator list:

- when `manifest.lane === "registry"`: a line naming `manifest.workflow.name`, how it matched, and that **no model wrote this script**;
- when `manifest.verdict?.ok` is true: a **Promote** button that reveals a small inline form (a `name` input, an optional `description`, a Save button), calls `promoteTask`, and renders any error text it throws.

Keep it inline and Tailwind-styled like the rest of the panel.

- [ ] **Step 5: Add the remembered badge to `TaskDetail.tsx`**

When `task.remembered_from_task_id` is set, render a small badge reading e.g. `remembered · seen 3×` beside the mode controls, with the source task id visible so a human can find what is being reused.

- [ ] **Step 6: Run the frontend checks**

```bash
cd frontend && npm test && npm run typecheck
```

Expected: all pass, no type errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src backend/ley_khaa/api/schemas.py backend/tests
git commit -m "feat(dashboard): promote a proven bundle and flag a remembered spec

Promotion is offered only for a run that passed, and a refusal renders its
reason — a taken name is fixable, but only if the human can read it."
```

---

### Task 15: The Registry page

**Files:**
- Create: `frontend/src/Registry.tsx`, `frontend/src/Registry.test.tsx`
- Modify: `frontend/src/api.ts`, `frontend/src/App.tsx`

**Interfaces:**
- Consumes: `GET /registry`, `POST /registry/{name}/unquarantine`, `DELETE /registry/{name}`.
- Produces: `fetchWorkflows()`, `unquarantineWorkflow(name)`, `deleteWorkflow(name)`, and `<Registry />` rendered by `App.tsx`.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/Registry.test.tsx` covering:

1. **`lists each workflow with its origin and usage`** — name, description, `seed`/`promoted`, and the run counts are on screen.
2. **`marks a quarantined workflow and offers to clear it`** — a quarantined row renders as quarantined and shows an enable control; clicking it calls `unquarantineWorkflow`.
3. **`does not offer to clear a healthy workflow`**.
4. **`removes a workflow and refreshes the list`** — clicking delete calls `deleteWorkflow` and re-fetches.
5. **`says so when the registry is empty`** — an empty registry renders an explanatory line, not a blank area. A fresh, unseeded install must not look broken.
6. **`surfaces a fetch failure`**.

Note for the implementer: `DELETE` is destructive and there is no undo. Use an inline two-step confirm (the button becomes "Really delete?") rather than `window.confirm` — a modal dialog blocks the browser automation this repo uses, and the project's own guidance is to avoid them.

- [ ] **Step 2: Run them and watch them fail**

```bash
cd frontend && npm test
```

- [ ] **Step 3: Extend `api.ts`**

```ts
export async function fetchWorkflows(): Promise<Workflow[]> {
  const res = await fetch(`${BASE}/registry`);
  if (!res.ok) throw new Error(`fetchWorkflows failed: ${res.status}`);
  return res.json();
}

export async function unquarantineWorkflow(name: string): Promise<Workflow> {
  const res = await fetch(`${BASE}/registry/${encodeURIComponent(name)}/unquarantine`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`unquarantineWorkflow failed: ${res.status}`);
  return res.json();
}

export async function deleteWorkflow(name: string): Promise<void> {
  const res = await fetch(`${BASE}/registry/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`deleteWorkflow failed: ${res.status}`);
}
```

- [ ] **Step 4: Build the page and mount it**

Write `Registry.tsx` in the shape of `Candidates.tsx` — a fetch in `useEffect`, a list, Tailwind classes matching the existing panels. Each row: name, description, `origin`, the declared roles and output format, `runs_ok`/`runs_failed`, a short `source_sha256` prefix, and the two controls. Mount it in `App.tsx` under a new `<h2>Registry</h2>` below Tasks, following how `Candidates` is rendered.

- [ ] **Step 5: Run the frontend checks**

```bash
cd frontend && npm test && npm run typecheck
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src
git commit -m "feat(dashboard): add the Registry page

Every cached capability, where it came from, how often it has run, and whether
it is quarantined — with an inline two-step delete rather than a modal, which
would block the browser automation this repo uses."
```

---

### Task 16: The end-to-end claim, and the docs

The phase's whole thesis, as a test. Nothing before this proves the two caches chain.

**Files:**
- Create: `backend/tests/test_caches_end_to_end.py`
- Modify: `README.md`, `CHANGELOG.md`
- Test: the suite, both frontends, the drift guard

**Interfaces:** consumes everything built above; produces nothing new.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_caches_end_to_end.py`:

```python
"""§9: the same request, asked twice, is free the second time.

Nothing is mocked except the count of model calls. Memory skips the interpreter,
the registry skips synthesis, and the two together mean the second run makes no
model call at all. If this test ever needs a mock to pass, the caches are not
actually chaining.
"""
```

Write these three tests:

1. **`test_the_second_identical_request_makes_no_model_calls`**
   - Install the seed workflows (`seeded_registry`).
   - Post a request whose spec resolves to `summary_stats` / **csv** / one input, drive it to `done`.
   - Wrap the LLM in a counting proxy (or assert on `FakeLLM.calls`) and post the **same text again** as a new conversation.
   - Assert: the second task reaches `done`; **zero** model calls were made during it; its `manifest["lane"] == "registry"`; its `remembered_from_task_id` is the first task's id.
   - **Use csv, not xlsx** — see Departure 3; an `.xlsx` is a zip embedding timestamps and is not byte-reproducible.

2. **`test_the_second_run_produces_byte_identical_output`**
   - Same setup; compare `deliverable/output.csv` bytes from both bundles with `read_bytes()`.

3. **`test_a_request_with_no_match_still_synthesizes`**
   - A request whose operation matches no seed takes `lane == "synthesis"` and still succeeds. The fast path must not have become the only path.

- [ ] **Step 2: Run it**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest tests/test_caches_end_to_end.py -v
```

Expected: PASS. **If the second run still makes a model call**, find which one before changing anything: an interpreter call means recall missed (check the fingerprint of the two message texts), and a synthesis call means the registry missed (check `operation_aliases`, `formats_agree`, and the bind).

- [ ] **Step 3: Run everything**

```bash
cd backend && TMPDIR="$HOME/.leykhaa-tmp" python -m pytest -q
cd ../frontend && npm test && npm run typecheck
```

Expected: all green, no skips, no warnings.

- [ ] **Step 4: Update the README**

Add a `### The two caches` subsection after "Where synthesized code runs", covering:

- what each cache short-circuits, and that the second identical request costs no model call;
- that matching is fingerprint-first and **fingerprint-only offline**, so both caches work with no `ANTHROPIC_API_KEY` — they just do not catch paraphrases;
- that a promoted workflow is frozen source with a hash and a link back to the bundle that proved it, promoted by a human from the dashboard;
- that a cached run is validated exactly like a synthesized one, and a failure quarantines the workflow and falls back to synthesis;
- that **the seeded demo conversation now takes the registry fast path** on a fresh `docker compose up`, because `set_difference` is a seed workflow and the demo matches it — and that anything else still synthesizes;
- that memory never remembers input FILES, only input names.

Also update the test counts in the `## Develop` block to whatever the suite now reports.

- [ ] **Step 5: Update the CHANGELOG**

Add a `## [0.5.0]` entry in the existing style: the two caches, the `params.json` contract change (call it out as the one change affecting Phase 3 behaviour), promotion, the Registry page, and the familiarity signal.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_caches_end_to_end.py README.md CHANGELOG.md
git commit -m "test: prove the two caches chain, and document them

The second identical request makes zero model calls and produces byte-identical
output. Deliberately asserted on a csv deliverable: an .xlsx is a zip embedding
timestamps, so byte-identity there would be a claim the bundle already declines
to make."
```

---

## Self-Review

Run against the spec after the plan was complete.

**Spec coverage:** §3.1 params contract → Task 1. §3.2 data model → Task 2. §3.3 matching/binding/alias learning → Tasks 3, 4, 6, 8. §3.4 fast path, quarantine, manifest → Task 8. §3.5 memory → Tasks 11, 12. §3.6 routing → Task 6. §3.7 persistence → Task 2. §4 seeds → Task 7. §5 API + dashboard → Tasks 9, 10, 14, 15. §6 error handling → distributed across Tasks 4, 6, 8, 9, 12 (every row of the spec's table has a named test). §7 testing → each task's tests, plus Task 16. §9 definition of done → Task 16 asserts the headline claim; the remaining lines are covered by Tasks 7, 8, 9, 12, 13.

**Gaps found and closed while reviewing:**
- The spec's §5 promote guard "the task must be `done`" is implemented as "the manifest's verdict passed", which is what `promote.py` can actually see from a bundle root. Task 9 case 3 tests the failed-verdict refusal, which is the same protection.
- The spec did not say what happens to `models.synthesis` when a cached run fails and synthesis rescues it. Task 8 Step 6 makes it explicit: the winning script's author is credited, which is the existing honesty rule rather than an exception to it.

**Type consistency:** `Match(workflow, binding, matched_by)` is defined in Task 3 and used in Tasks 6 and 8. `bind()` returns `dict[str, str] | None` in Task 4 and is consumed that way in Task 6. `RegistryDecision` / `MemoryDecision` field names match between the models, the matchers, and the `HeuristicLLM` answers. `WorkflowRepository.record_success(name, *, learned_alias=None)` is defined in Task 5 and called with exactly that signature in Task 8. `ensure_seed_workflows(session) -> int` is defined in Task 7 and used in Tasks 8, 10, 16. `save_memory_hit(task_id, *, source_task_id, familiarity)` is defined and called in Task 12. `recommend(..., familiarity=0)` is defined in Task 13 and called from `_gate` in the same task.

**Known risk, flagged rather than solved:** Task 8 is the largest task and the only one touching a hot path that every existing executor test runs through. If its review turns up more than one defect, stop and split it — the fast path, the quarantine fallback, and the manifest fields are three separable commits.
