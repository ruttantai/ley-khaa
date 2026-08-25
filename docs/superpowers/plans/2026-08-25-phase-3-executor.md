# Phase 3 — Synthesis-First Executor, Sandbox, and Output Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `_execute` / `_validate` stubs in `TaskDriver` with a real executor that resolves inputs, synthesizes a Python script, runs it in a sandbox, validates the result, and persists a reproducible Output Bundle containing the deliverable and the code that produced it.

**Architecture:** A new `ley_khaa/executor/` package. `ExecutionRunner.run(row, spec)` owns the whole lane: resolve inputs → build workspace → synthesize → sandbox run → validate, with exactly one repair attempt driven by the traceback. The runner never raises for a business failure — it returns a `Verdict`, which `_execute` persists and `_validate` acts on. That split is what lets the entire phase land with **no change to the state machine**.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 typed `Mapped`, Alembic, `anthropic` SDK, Faker, openpyxl, Docker, pytest; React + TypeScript + Vite + Tailwind v4, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-25-phase-3-executor-design.md` — read it before starting. It records the four decisions this plan implements and the reasoning behind them.

## Global Constraints

- **Python** `>=3.12`; **Pydantic** `v2`; **SQLAlchemy** `2.0` typed `Mapped` style. DB access **only** through repositories.
- **Model IDs are exact strings, never date-suffixed:** `claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-5`.
- **Structured output:** always `client.messages.parse(model=…, output_format=SomePydanticModel, …)` and read `response.parsed_output`. Never hand-roll JSON parsing.
- **Thinking is model-gated.** `thinking={"type": "adaptive"}` only for `claude-opus-5` / `claude-sonnet-5`. `claude-haiku-4-5` accepts neither `thinking` nor `output_config.effort`. The router already carries this as `ModelChoice.supports_thinking`; never guess at a call site.
- **Tests never make network calls.** Every LLM-touching test injects `FakeLLM`. `AnthropicLLM` must not be constructed anywhere under `backend/tests/`.
- **Tests never depend on Docker.** `conftest.py` pins `LEY_KHAA_SANDBOX=subprocess`. Docker-specific tests carry `@pytest.mark.docker` and skip when no daemon answers.
- **The offline path must stay runnable.** `docker compose up` on a fresh clone with **no `ANTHROPIC_API_KEY`** must reach `done` with a real `.xlsx` on disk. Any new LLM stage needs a `HeuristicLLM` rule or the fresh-clone demo breaks.
- **The state machine does not change.** `backend/ley_khaa/domain/states.py` is not edited in this phase. If you think you need a new edge, re-read Task 10 — the verdict/act split already covers it.
- **Data is synthetic only.** No real employer data, credentials, or infrastructure — ever.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `test:`, `chore:`, `docs:`). Commit after every task.
- **Versioning:** SemVer; this phase releases as tag **`v0.4.0`**.
- **Package name:** backend Python package is `ley_khaa` (underscore); repo/product name is `ley-khaa`.
- **Registry is out of scope.** No workflow matching, binding, or promotion. That is Phase 4 (v0.5.0).

## File Structure

| File | Responsibility |
|---|---|
| `backend/ley_khaa/executor/__init__.py` | package marker |
| `backend/ley_khaa/executor/catalog.py` | Faker-seeded synthetic securities datasets; deterministic |
| `backend/ley_khaa/executor/resolver.py` | spec input names → `ResolvedInput`; attachment first, catalog second |
| `backend/ley_khaa/executor/workspace.py` | bundle directory layout, generator attempts, `manifest.json` |
| `backend/ley_khaa/executor/sandbox.py` | `SandboxRunner` protocol, `SubprocessSandbox`, `DockerSandbox`, selection |
| `backend/ley_khaa/executor/synthesizer.py` | `TaskSpec` + inputs → `SynthesizedScript`; synthesize and repair |
| `backend/ley_khaa/executor/validator.py` | pure `validate(...) -> Verdict` |
| `backend/ley_khaa/executor/runner.py` | `ExecutionRunner` — the lane, the repair loop, the manifest |
| `backend/ley_khaa/llm/router.py` | extend: `Stage.SYNTHESIS` → Opus, 16000 max tokens |
| `backend/ley_khaa/llm/heuristic.py` | extend: offline `SynthesizedScript` rule |
| `backend/ley_khaa/persistence/orm.py` | extend: `TaskRow.workspace_path`, `TaskRow.execution_verdict` |
| `backend/ley_khaa/persistence/repository.py` | extend: `save_execution()` |
| `backend/ley_khaa/alembic/versions/0003_executor.py` | phase-3 columns |
| `backend/ley_khaa/orchestrator/driver.py` | rewire `_execute` / `_validate` onto the runner |
| `backend/ley_khaa/api/app.py` | bundle endpoints |
| `backend/ley_khaa/api/schemas.py` | extend: `TaskOut`, `BundleOut` |
| `backend/sandbox/Dockerfile` | the `ley-khaa-sandbox` image |
| `docker-compose.yml` | docker socket, named workspace volume, sandbox build |
| `frontend/src/api.ts` | extend: `Task` fields, bundle calls |
| `frontend/src/BundlePanel.tsx` | manifest summary, generator source, downloads |
| `frontend/src/TaskDetail.tsx` | mount the bundle panel |

---

### Task 1: Synthetic dataset catalog

**Files:**
- Create: `backend/ley_khaa/executor/__init__.py`
- Create: `backend/ley_khaa/executor/catalog.py`
- Modify: `backend/pyproject.toml`
- Test: `backend/tests/test_catalog.py`

**Interfaces:**
- Consumes: nothing (first task of the phase).
- Produces: `catalog.DATASET_NAMES: tuple[str, ...]`, `catalog.build_dataset(name: str) -> str` (CSV text), `catalog.resolve_name(query: str) -> str | None`. Task 2 calls all three.

**Why ambiguity returns `None`:** the input name `"universe"` matches both `bloomberg_universe` and `factset_universe`. Guessing which one the human meant is precisely the mistake that should surface as a clarification, so ambiguous lookups resolve to nothing and Task 2 turns that into a question.

- [ ] **Step 1: Add the dependencies**

In `backend/pyproject.toml`, extend the `dependencies` list:

```toml
dependencies = [
    "anthropic>=0.70",
    "fastapi>=0.111",
    "uvicorn[standard]>=0.30",
    "sqlalchemy>=2.0",
    "psycopg[binary]>=3.1",
    "pydantic>=2.7",
    "alembic>=1.13",
    "faker>=25",
    "openpyxl>=3.1",
]
```

`faker` builds the catalog. `openpyxl` is needed **in the backend** (not only the sandbox image) so the validator can count rows in a generated spreadsheet and the reproducibility test can compare cell values. Then run `pip install -e '.[dev]'` from `backend/`.

- [ ] **Step 2: Write the failing test**

```python
# backend/tests/test_catalog.py
import csv
import io

from ley_khaa.executor import catalog


def _rows(name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(catalog.build_dataset(name))))


def test_datasets_are_deterministic():
    """The whole reproducibility claim rests on this: same seed, same bytes."""
    catalog.build_dataset.cache_clear()
    first = catalog.build_dataset("bloomberg_universe")
    catalog.build_dataset.cache_clear()
    second = catalog.build_dataset("bloomberg_universe")
    assert first == second


def test_universes_differ_in_both_directions():
    """The demo asks what is missing; a set-difference with nothing to find
    would make the headline conversation look like it worked when it didn't."""
    bloomberg = {r["ticker"] for r in _rows("bloomberg_universe")}
    factset = {r["ticker"] for r in _rows("factset_universe")}
    assert len(bloomberg - factset) == 5
    assert len(factset - bloomberg) == 3


def test_resolve_name_matches_a_spoken_input_name():
    assert catalog.resolve_name("Bloomberg universe") == "bloomberg_universe"
    assert catalog.resolve_name("factset") == "factset_universe"
    assert catalog.resolve_name("holdings") == "holdings"


def test_resolve_name_refuses_to_guess_when_ambiguous():
    assert catalog.resolve_name("universe") is None


def test_resolve_name_returns_none_for_unknown_input():
    assert catalog.resolve_name("trades") is None
    assert catalog.resolve_name("") is None
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `cd backend && python -m pytest tests/test_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.executor'`

- [ ] **Step 4: Create the package marker**

```python
# backend/ley_khaa/executor/__init__.py
```

(Empty file.)

- [ ] **Step 5: Write the implementation**

```python
# backend/ley_khaa/executor/catalog.py
"""Faker-seeded synthetic securities datasets (spec §5.10, decision 2).

The catalog is the fallback when a spec input name matches no attachment. It is
deterministic on purpose: the same seed produces the same rows on every machine
and every run, which is what lets the reproducibility test make a claim about
the bundle rather than about the weather.

All data here is synthetic. Nothing in this module touches a real vendor feed.
"""
from __future__ import annotations

import csv
import io
import re
from functools import lru_cache

from faker import Faker

CATALOG_SEED = 20260825

DATASET_NAMES = ("bloomberg_universe", "factset_universe", "holdings", "portfolio")

_SECTORS = ("Financials", "Technology", "Energy", "Healthcare", "Industrials")
_CURRENCIES = ("USD", "EUR", "GBP", "JPY")

_UNIVERSE_SIZE = 200
# factset drops the first few rows of the shared base and gains a tail of its
# own, so the demo's set-difference finds something in BOTH directions.
_FACTSET_DROPPED = 5
_FACTSET_EXTRA = 3

_UNIVERSE_FIELDS = ["ticker", "isin", "name", "sector", "currency"]
_POSITION_FIELDS = ["ticker", "isin", "quantity", "weight"]

_TOKEN = re.compile(r"[a-z0-9]+")


def _base_rows() -> list[dict[str, str]]:
    fake = Faker()
    Faker.seed(CATALOG_SEED)
    rows: list[dict[str, str]] = []
    for i in range(_UNIVERSE_SIZE + _FACTSET_EXTRA):
        rows.append(
            {
                "ticker": f"SYN{i:04d}",
                "isin": f"XS{i:010d}",
                "name": fake.company(),
                "sector": _SECTORS[i % len(_SECTORS)],
                "currency": _CURRENCIES[i % len(_CURRENCIES)],
                "quantity": str((i * 37) % 5000 + 100),
                "weight": f"{(i % 97 + 1) / 1000:.4f}",
            }
        )
    return rows


def _to_csv(rows: list[dict[str, str]], fields: list[str]) -> str:
    buf = io.StringIO()
    # lineterminator is pinned so the bytes do not differ between platforms.
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row[field] for field in fields})
    return buf.getvalue()


@lru_cache(maxsize=None)
def build_dataset(name: str) -> str:
    """Return the dataset as CSV text. Raises KeyError for an unknown name."""
    if name not in DATASET_NAMES:
        raise KeyError(name)
    rows = _base_rows()
    if name == "bloomberg_universe":
        return _to_csv(rows[:_UNIVERSE_SIZE], _UNIVERSE_FIELDS)
    if name == "factset_universe":
        return _to_csv(rows[_FACTSET_DROPPED:], _UNIVERSE_FIELDS)
    if name == "holdings":
        return _to_csv(rows[:60], _POSITION_FIELDS)
    return _to_csv(rows[3:63], _POSITION_FIELDS)


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(value.lower()))


def resolve_name(query: str) -> str | None:
    """Map a spec input name onto a dataset, or None if unknown or ambiguous.

    Ambiguity resolves to None deliberately: "universe" matches two datasets,
    and guessing which one the human meant is exactly the mistake that should
    become a clarification instead of a silently wrong answer.
    """
    wanted = _tokens(query)
    if not wanted:
        return None
    matches = [
        name for name in DATASET_NAMES
        if wanted <= _tokens(name) or _tokens(name) <= wanted
    ]
    return matches[0] if len(matches) == 1 else None
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd backend && python -m pytest tests/test_catalog.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Commit**

```bash
git add backend/pyproject.toml backend/ley_khaa/executor/ backend/tests/test_catalog.py
git commit -m "feat(executor): deterministic synthetic dataset catalog"
```

---

### Task 2: Input resolver

**Files:**
- Create: `backend/ley_khaa/executor/resolver.py`
- Test: `backend/tests/test_input_resolver.py`

**Interfaces:**
- Consumes: `catalog.resolve_name`, `catalog.build_dataset` (Task 1); `MessageRepository.get_many(ids) -> list[MessageRow]` and `TaskRow.source_message_ids` (both already exist).
- Produces: `ResolvedInput(name, filename, content, source)` with a `.sha256` property; `UnresolvedInputs(Exception)` carrying `.names: list[str]`; `resolve_inputs(spec, task, messages) -> list[ResolvedInput]`. Tasks 3, 8, and 9 all consume these.

**Note on the spec sketch:** §3.2 of the design sketches `ResolvedInput` with a `path`. The path is assigned by the workspace in Task 3, not by the resolver, so `ResolvedInput` carries `content` + `filename` and the final path is always `inputs/<filename>`. This keeps the resolver pure and testable without touching a filesystem.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_input_resolver.py
import pytest

from ley_khaa.domain.models import Attachment, AttachmentKind, Message
from ley_khaa.executor.resolver import UnresolvedInputs, resolve_inputs
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _spec(inputs: list[str]) -> TaskSpec:
    return TaskSpec(
        intent="compare the two lists",
        inputs=inputs,
        operation="set_difference",
        output_format="xlsx",
        certainty=0.9,
    )


def _task_with(session, attachments: list[Attachment]):
    messages = MessageRepository(session)
    row = messages.add(
        Message(
            source="slack",
            client="demo",
            conversation_id="conv-1",
            author="boss",
            text="compare these",
            attachments=attachments,
        )
    )
    task = TaskRepository(session).create(
        project="demo", title="compare", source_message_ids=[row.id]
    )
    return task, messages


def test_an_attachment_satisfies_a_spec_input(session):
    task, messages = _task_with(
        session,
        [Attachment(kind=AttachmentKind.TABLE, name="holdings.csv", content="ticker\nAAA\n")],
    )
    resolved = resolve_inputs(_spec(["holdings"]), task, messages)
    assert [r.source for r in resolved] == ["attachment"]
    assert resolved[0].content == "ticker\nAAA\n"


def test_attachments_win_over_the_catalog(session):
    """A human who pasted data meant that data, not our synthetic stand-in."""
    task, messages = _task_with(
        session,
        [Attachment(kind=AttachmentKind.TABLE, name="holdings.csv", content="ticker\nAAA\n")],
    )
    resolved = resolve_inputs(_spec(["holdings"]), task, messages)
    assert resolved[0].source == "attachment"
    assert "SYN" not in resolved[0].content


def test_the_catalog_covers_a_name_no_attachment_provides(session):
    task, messages = _task_with(session, [])
    resolved = resolve_inputs(_spec(["Bloomberg universe", "FactSet"]), task, messages)
    assert [r.source for r in resolved] == ["catalog", "catalog"]
    assert [r.filename for r in resolved] == ["bloomberg_universe.csv", "factset_universe.csv"]


def test_an_unresolvable_input_raises_with_every_missing_name(session):
    task, messages = _task_with(session, [])
    with pytest.raises(UnresolvedInputs) as excinfo:
        resolve_inputs(_spec(["Bloomberg universe", "trade blotter", "universe"]), task, messages)
    # All of them, not just the first: asking the human one question about one
    # gap, then another question about the next, is the ping-pong the
    # clarification cap exists to prevent.
    assert excinfo.value.names == ["trade blotter", "universe"]


def test_image_attachments_are_not_treated_as_data(session):
    """Vision extraction is not built in this phase; an image is not a table."""
    task, messages = _task_with(
        session,
        [Attachment(kind=AttachmentKind.IMAGE, name="holdings.png", content="base64...")],
    )
    with pytest.raises(UnresolvedInputs):
        resolve_inputs(_spec(["holdings screenshot"]), task, messages)


def test_colliding_filenames_stay_distinct(session):
    task, messages = _task_with(
        session,
        [
            Attachment(kind=AttachmentKind.TABLE, name="data.csv", content="a\n1\n"),
            Attachment(kind=AttachmentKind.TABLE, name="data.csv", content="b\n2\n"),
        ],
    )
    resolved = resolve_inputs(_spec(["data", "data"]), task, messages)
    assert len({r.filename for r in resolved}) == 2


def test_sha256_is_content_addressed(session):
    task, messages = _task_with(session, [])
    resolved = resolve_inputs(_spec(["holdings"]), task, messages)
    assert len(resolved[0].sha256) == 64
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && python -m pytest tests/test_input_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.executor.resolver'`

- [ ] **Step 3: Write the implementation**

```python
# backend/ley_khaa/executor/resolver.py
"""Turn `TaskSpec.inputs` names into actual bytes (spec §5.10, decision 2).

Attachments win over the catalog: a human who pasted data meant that data. A
name that matches neither is NOT guessed at — it is raised, and the caller
turns it into a clarification before a single token is spent on synthesis.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..domain.models import AttachmentKind
from ..interpreter.spec import TaskSpec
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import TaskRow
from . import catalog

_TOKEN = re.compile(r"[a-z0-9]+")

# Only these carry literal content the executor can compute on. An IMAGE
# attachment needs vision extraction, which is not built in this phase.
_TEXTUAL = {AttachmentKind.TABLE.value, AttachmentKind.TEXT.value}


@dataclass(frozen=True)
class ResolvedInput:
    name: str       # the spec input name this satisfies
    filename: str   # what it is called inside inputs/
    content: str
    source: str     # "attachment" | "catalog"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class UnresolvedInputs(Exception):
    """Raised with EVERY unresolved name, so the human is asked once."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(", ".join(names))
        self.names = names


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_TOKEN.findall(value.lower()))


def _attachments_for(task: TaskRow, messages: MessageRepository) -> list[dict]:
    rows = messages.get_many(list(task.source_message_ids or []))
    found: list[dict] = []
    for row in rows:
        for attachment in row.attachments or []:
            if attachment.get("kind") in _TEXTUAL:
                found.append(attachment)
    return found


def _from_attachments(name: str, attachments: list[dict], used: set[int]) -> dict | None:
    wanted = _tokens(name)
    if not wanted:
        return None
    for index, attachment in enumerate(attachments):
        if index in used:
            continue
        # Strip the extension before matching: "holdings.csv" should answer to
        # the spoken input name "holdings".
        stem = _tokens(attachment.get("name", "").rsplit(".", 1)[0])
        if wanted <= stem or stem <= wanted:
            used.add(index)
            return attachment
    return None


def _unique(filename: str, taken: set[str]) -> str:
    if filename not in taken:
        taken.add(filename)
        return filename
    stem, _, suffix = filename.rpartition(".")
    counter = 2
    while True:
        candidate = f"{stem}_{counter}.{suffix}" if stem else f"{filename}_{counter}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        counter += 1


def resolve_inputs(
    spec: TaskSpec, task: TaskRow, messages: MessageRepository
) -> list[ResolvedInput]:
    attachments = _attachments_for(task, messages)
    used_attachments: set[int] = set()
    taken_filenames: set[str] = set()
    resolved: list[ResolvedInput] = []
    missing: list[str] = []

    for name in spec.inputs:
        hit = _from_attachments(name, attachments, used_attachments)
        if hit is not None:
            resolved.append(
                ResolvedInput(
                    name=name,
                    filename=_unique(hit.get("name") or f"{name}.csv", taken_filenames),
                    content=hit.get("content", ""),
                    source="attachment",
                )
            )
            continue

        dataset = catalog.resolve_name(name)
        if dataset is not None:
            resolved.append(
                ResolvedInput(
                    name=name,
                    filename=_unique(f"{dataset}.csv", taken_filenames),
                    content=catalog.build_dataset(dataset),
                    source="catalog",
                )
            )
            continue

        missing.append(name)

    if missing:
        raise UnresolvedInputs(missing)
    return resolved
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && python -m pytest tests/test_input_resolver.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full suite for regressions**

Run: `cd backend && python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/executor/resolver.py backend/tests/test_input_resolver.py
git commit -m "feat(executor): resolve spec inputs from attachments then the catalog"
```

---

### Task 3: Workspace and manifest persistence

**Files:**
- Create: `backend/ley_khaa/executor/workspace.py`
- Modify: `backend/ley_khaa/config.py`
- Modify: `.gitignore`
- Test: `backend/tests/test_workspace.py`

**Interfaces:**
- Consumes: `ResolvedInput` (Task 2).
- Produces: `Workspace` with `.root`, `.inputs_dir`, `.generator_dir`, `.deliverable_dir`, `Workspace.create(root, task_id)`, `.write_inputs(list[ResolvedInput])`, `.write_generator(attempt, source) -> Path`, `.deliverables() -> list[Path]`, `.input_hashes() -> dict[str, str]`, `.write_manifest(dict) -> Path`, `.read_manifest() -> dict`; and `sha256_file(path) -> str`. Tasks 8, 9, and 11 consume these.

**Why `input_hashes()` exists:** the Docker sandbox mounts the workspace read-write (see Task 5 for why a read-only input mount is not available to us), so input immutability is enforced by *checking* rather than by a mount flag. The validator re-hashes inputs after the run; a script that rewrote its own inputs has broken reproducibility and fails.

- [ ] **Step 1: Add the workspace settings**

In `backend/ley_khaa/config.py`, add to `Settings`:

```python
    # Where Output Bundles live (spec §5.11). Under compose this is a named
    # volume mounted at the SAME path in the backend and the sandbox container,
    # so a path is valid on both sides — see docker-compose.yml.
    workspace_root: str = os.getenv("LEY_KHAA_WORKSPACE_ROOT", "./task-workspaces")
```

- [ ] **Step 2: Ignore generated bundles**

Append to `.gitignore`:

```
task-workspaces/
```

- [ ] **Step 3: Write the failing test**

```python
# backend/tests/test_workspace.py
import json

from ley_khaa.executor.resolver import ResolvedInput
from ley_khaa.executor.workspace import Workspace, sha256_file


def _input(name="holdings", content="ticker\nAAA\n") -> ResolvedInput:
    return ResolvedInput(name=name, filename=f"{name}.csv", content=content, source="catalog")


def test_create_lays_out_the_bundle_directories(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    assert ws.root == tmp_path / "task-task-1"
    assert ws.inputs_dir.is_dir()
    assert ws.generator_dir.is_dir()
    assert ws.deliverable_dir.is_dir()


def test_inputs_are_frozen_into_the_bundle(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    ws.write_inputs([_input()])
    assert (ws.inputs_dir / "holdings.csv").read_text() == "ticker\nAAA\n"


def test_each_attempt_is_kept(tmp_path):
    """A bundle that hides its first failure is not an audit trail."""
    ws = Workspace.create(tmp_path, "task-1")
    first = ws.write_generator(1, "print('one')")
    second = ws.write_generator(2, "print('two')")
    assert first.name == "attempt_1.py"
    assert second.name == "attempt_2.py"
    assert first.read_text() == "print('one')"
    assert sorted(p.name for p in ws.generator_dir.iterdir()) == ["attempt_1.py", "attempt_2.py"]


def test_deliverables_lists_only_produced_files(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    assert ws.deliverables() == []
    (ws.deliverable_dir / "out.xlsx").write_text("x")
    assert [p.name for p in ws.deliverables()] == ["out.xlsx"]


def test_input_hashes_detect_a_script_that_rewrote_its_inputs(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    ws.write_inputs([_input()])
    before = ws.input_hashes()
    (ws.inputs_dir / "holdings.csv").write_text("tampered\n")
    assert ws.input_hashes() != before


def test_manifest_round_trips(tmp_path):
    ws = Workspace.create(tmp_path, "task-1")
    path = ws.write_manifest({"task_id": "task-1", "lane": "synthesis"})
    assert json.loads(path.read_text())["lane"] == "synthesis"
    assert ws.read_manifest()["task_id"] == "task-1"


def test_read_manifest_is_empty_before_one_is_written(tmp_path):
    assert Workspace.create(tmp_path, "task-1").read_manifest() == {}


def test_create_is_idempotent(tmp_path):
    """The sweeper can re-enter a task; re-creating must not wipe evidence."""
    ws = Workspace.create(tmp_path, "task-1")
    ws.write_generator(1, "print('one')")
    again = Workspace.create(tmp_path, "task-1")
    assert (again.generator_dir / "attempt_1.py").exists()


def test_sha256_file_is_content_addressed(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("hello")
    assert sha256_file(target) == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `cd backend && python -m pytest tests/test_workspace.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.executor.workspace'`

- [ ] **Step 5: Write the implementation**

```python
# backend/ley_khaa/executor/workspace.py
"""The reproducible Output Bundle on disk (spec §5.11).

    task-<id>/
    ├── deliverable/   what the human asked for
    ├── generator/     the ACTUAL code that produced it, every attempt
    ├── inputs/        the exact bytes it ran against
    └── manifest.json  provenance

Every failed attempt stays in generator/. A bundle that hides its first failure
is not an audit trail.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .resolver import ResolvedInput

MANIFEST_NAME = "manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Workspace:
    root: Path

    @classmethod
    def create(cls, root: Path | str, task_id: str) -> Workspace:
        """Lay out (or re-open) the bundle for a task.

        Idempotent: the sweeper can re-enter a task, and re-creating the
        workspace must never wipe evidence from an earlier attempt.
        """
        workspace = cls(Path(root) / f"task-{task_id}")
        for directory in (workspace.inputs_dir, workspace.generator_dir, workspace.deliverable_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return workspace

    @property
    def inputs_dir(self) -> Path:
        return self.root / "inputs"

    @property
    def generator_dir(self) -> Path:
        return self.root / "generator"

    @property
    def deliverable_dir(self) -> Path:
        return self.root / "deliverable"

    def write_inputs(self, resolved: list[ResolvedInput]) -> None:
        for item in resolved:
            (self.inputs_dir / item.filename).write_text(item.content, encoding="utf-8")

    def write_generator(self, attempt: int, source: str) -> Path:
        path = self.generator_dir / f"attempt_{attempt}.py"
        path.write_text(source, encoding="utf-8")
        return path

    def deliverables(self) -> list[Path]:
        return sorted(p for p in self.deliverable_dir.iterdir() if p.is_file())

    def input_hashes(self) -> dict[str, str]:
        """filename -> sha256, so a script that rewrote its inputs is caught."""
        return {p.name: sha256_file(p) for p in sorted(self.inputs_dir.iterdir()) if p.is_file()}

    def write_manifest(self, manifest: dict) -> Path:
        path = self.root / MANIFEST_NAME
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def read_manifest(self) -> dict:
        path = self.root / MANIFEST_NAME
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd backend && python -m pytest tests/test_workspace.py -v`
Expected: PASS (9 tests)

- [ ] **Step 7: Commit**

```bash
git add backend/ley_khaa/executor/workspace.py backend/ley_khaa/config.py backend/tests/test_workspace.py .gitignore
git commit -m "feat(executor): output bundle workspace and manifest persistence"
```

---

### Task 4: Sandbox seam and the subprocess fallback

**Files:**
- Create: `backend/ley_khaa/executor/sandbox.py`
- Modify: `backend/ley_khaa/config.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_sandbox_contract.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `SandboxResult(exit_code, stdout, stderr, duration_ms, timed_out)` with an `.ok` property; `SandboxUnavailable(Exception)`; the `SandboxRunner` protocol (`.name`, `.run(script=, workspace=, timeout_s=)`); `SubprocessSandbox`. Task 5 adds `DockerSandbox` and `pick_sandbox()`; Task 9 consumes the protocol.

**Why a contract test:** the fallback exists so the SQLite dev loop keeps working. The moment it can behave differently from `DockerSandbox`, it eventually will, and the difference shows up as a bundle that ran somewhere other than where its manifest claims. One parametrized suite both must pass keeps them honest — Task 5 adds Docker to the same suite rather than writing its own.

- [ ] **Step 1: Add the sandbox settings**

In `backend/ley_khaa/config.py`, add to `Settings`:

```python
    # "auto" picks Docker when a daemon answers and falls back otherwise.
    # "docker" / "subprocess" pin one explicitly. Tests pin subprocess.
    sandbox_backend: str = os.getenv("LEY_KHAA_SANDBOX", "auto")
    sandbox_image: str = os.getenv("LEY_KHAA_SANDBOX_IMAGE", "ley-khaa-sandbox")
    sandbox_timeout_seconds: int = int(os.getenv("LEY_KHAA_SANDBOX_TIMEOUT", "60"))
    sandbox_memory_mb: int = int(os.getenv("LEY_KHAA_SANDBOX_MEMORY_MB", "512"))
    # Set under compose so DockerSandbox mounts the named volume by name; a
    # sibling container cannot bind-mount the backend's own container paths.
    workspace_volume: str | None = os.getenv("LEY_KHAA_WORKSPACE_VOLUME") or None
```

- [ ] **Step 2: Pin the sandbox in tests**

In `backend/tests/conftest.py`, add alongside the other env pins at the top of the file (before any `ley_khaa` import):

```python
os.environ["LEY_KHAA_SANDBOX"] = "subprocess"
```

- [ ] **Step 3: Write the failing contract test**

```python
# backend/tests/test_sandbox_contract.py
"""One suite every SandboxRunner must pass.

Task 5 adds DockerSandbox to RUNNERS rather than writing a second suite: the
fallback earning a pass the real sandbox would fail is exactly the drift this
file exists to prevent.
"""
import pytest

from ley_khaa.executor.sandbox import SubprocessSandbox

RUNNERS = [pytest.param(SubprocessSandbox(), id="subprocess")]


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "deliverable").mkdir()
    (tmp_path / "generator").mkdir()
    (tmp_path / "inputs" / "data.csv").write_text("ticker\nAAA\n")
    return tmp_path


def _script(workspace, source: str):
    path = workspace / "generator" / "attempt_1.py"
    path.write_text(source)
    return path


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_clean_run_reports_success_and_captures_stdout(runner, workspace):
    script = _script(workspace, "print('hello from the sandbox')")
    result = runner.run(script=script, workspace=workspace, timeout_s=30)
    assert result.exit_code == 0
    assert result.ok
    assert "hello from the sandbox" in result.stdout
    assert result.duration_ms >= 0


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_crash_reports_the_traceback(runner, workspace):
    """The traceback is what the repair attempt is prompted with."""
    script = _script(workspace, "raise ValueError('boom')")
    result = runner.run(script=script, workspace=workspace, timeout_s=30)
    assert result.exit_code != 0
    assert not result.ok
    assert "ValueError" in result.stderr
    assert "boom" in result.stderr


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_runaway_script_is_killed(runner, workspace):
    script = _script(workspace, "while True:\n    pass\n")
    result = runner.run(script=script, workspace=workspace, timeout_s=2)
    assert result.timed_out
    assert not result.ok


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_script_can_read_inputs_and_write_a_deliverable(runner, workspace):
    script = _script(
        workspace,
        "rows = open('inputs/data.csv').read()\n"
        "open('deliverable/out.csv', 'w').write(rows)\n",
    )
    result = runner.run(script=script, workspace=workspace, timeout_s=30)
    assert result.ok, result.stderr
    assert (workspace / "deliverable" / "out.csv").read_text() == "ticker\nAAA\n"


@pytest.mark.parametrize("runner", RUNNERS)
def test_secrets_are_not_visible_to_the_script(runner, workspace, monkeypatch):
    """Synthesized code must never be able to read our API key out of the env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    script = _script(
        workspace,
        "import os\nprint('KEY=' + repr(os.environ.get('ANTHROPIC_API_KEY')))\n",
    )
    result = runner.run(script=script, workspace=workspace, timeout_s=30)
    assert result.ok, result.stderr
    assert "KEY=None" in result.stdout
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `cd backend && python -m pytest tests/test_sandbox_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.executor.sandbox'`

- [ ] **Step 5: Write the implementation**

```python
# backend/ley_khaa/executor/sandbox.py
"""Where synthesized code actually runs (spec §5.10, decision 3).

Two implementations behind one protocol. DockerSandbox is the real thing and
the default. SubprocessSandbox keeps the Docker-free dev loop working and is
weaker in a way it is loud about: it cannot take the network away. The manifest
records which one ran, so a bundle never overstates its own isolation.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# Everything else is stripped. A synthesized script has no business reading our
# API key, and an allowlist means a credential added later is excluded by
# default rather than leaking until someone notices.
_ALLOWED_ENV = ("PATH", "LANG", "LC_ALL", "TZ")


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxUnavailable(Exception):
    """The sandbox itself could not run.

    Infrastructure, not the script — the caller must treat this as a failure of
    ley-khaa, never as a question to put to a human.
    """


class SandboxRunner(Protocol):
    name: str

    def run(self, *, script: Path, workspace: Path, timeout_s: int) -> SandboxResult:
        ...


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _clean_env(workspace: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ALLOWED_ENV if key in os.environ}
    env["HOME"] = str(workspace)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class SubprocessSandbox:
    """Fallback for when no Docker daemon answers.

    Weaker than DockerSandbox: it limits CPU and memory and scrubs the
    environment, but it CANNOT remove network access. Callers announce that.
    """

    name = "subprocess"

    def __init__(self, *, memory_mb: int = 512) -> None:
        self.memory_mb = memory_mb

    def _limits(self, timeout_s: int):
        if os.name != "posix":
            return None
        memory_mb = self.memory_mb

        def apply() -> None:
            import resource

            resource.setrlimit(resource.RLIMIT_CPU, (timeout_s, timeout_s))
            # RLIMIT_AS is deliberately skipped on macOS: numpy and pandas
            # reserve large VIRTUAL address ranges at import time, so an
            # address-space cap spuriously MemoryErrors there while saying
            # nothing about real memory use. The wall-clock timeout and
            # RLIMIT_CPU still apply.
            if sys.platform != "darwin":
                limit = memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

        return apply

    def run(self, *, script: Path, workspace: Path, timeout_s: int) -> SandboxResult:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(workspace),
                env=_clean_env(workspace),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                preexec_fn=self._limits(timeout_s),
            )
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(
                exit_code=-1,
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr) + f"\nkilled after {timeout_s}s",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )
        except OSError as exc:  # no interpreter, no permission: our problem
            raise SandboxUnavailable(f"could not start the sandbox: {exc}") from exc

        return SandboxResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
        )
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `cd backend && python -m pytest tests/test_sandbox_contract.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Run the full suite for regressions**

Run: `cd backend && python -m pytest -q`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add backend/ley_khaa/executor/sandbox.py backend/ley_khaa/config.py backend/tests/conftest.py backend/tests/test_sandbox_contract.py
git commit -m "feat(executor): sandbox seam with the subprocess fallback"
```

---

### Task 5: Docker sandbox, its image, and compose wiring

**Files:**
- Modify: `backend/ley_khaa/executor/sandbox.py`
- Modify: `backend/pyproject.toml`
- Create: `backend/sandbox/Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `backend/tests/test_sandbox_contract.py`
- Test: `backend/tests/test_sandbox_selection.py`

**Interfaces:**
- Consumes: `SandboxResult`, `SandboxUnavailable`, `SandboxRunner`, `SubprocessSandbox` (Task 4).
- Produces: `DockerSandbox(image=, memory_mb=, volume=, volume_target=)` with a `.available()` method, and `pick_sandbox() -> SandboxRunner`. Task 9 calls `pick_sandbox()`.

**DEVIATION FROM THE SPEC — read this before writing the Dockerfile.** §4.1 of the design lists `pandas` and `numpy` in the sandbox image. Do **not** install them. The subprocess fallback runs the *backend's* interpreter, so any library the image has and the backend lacks makes the two sandboxes behave differently — which is the exact drift the contract test exists to prevent, and it would mean a script that works under `docker compose up` crashes on the SQLite dev loop. The allowed set is therefore **stdlib + `openpyxl` + `python-docx`**, installed in both places. `set_difference` and `summary_stats` need nothing more than `csv` and `openpyxl`. Adding pandas is a fine future change *provided* it is added to the backend at the same time.

- [ ] **Step 1: Match the library sets**

In `backend/pyproject.toml`, add `python-docx` so the fallback can run the same scripts the image can (`openpyxl` was added in Task 1):

```toml
    "faker>=25",
    "openpyxl>=3.1",
    "python-docx>=1.1",
]
```

Run `pip install -e '.[dev]'` from `backend/`.

- [ ] **Step 2: Write the sandbox image**

```dockerfile
# backend/sandbox/Dockerfile
# The only place synthesized code is allowed to run.
#
# Versions are pinned, not floated: a bundle claims that generator/ re-runs to
# the same deliverable, and a library that silently changes behaviour under it
# turns that claim into a lie.
#
# Keep this library set identical to the backend's own (backend/pyproject.toml).
# The subprocess fallback runs the backend interpreter, so anything installed
# here and missing there makes the two sandboxes disagree.
FROM python:3.12-slim
RUN pip install --no-cache-dir openpyxl==3.1.5 python-docx==1.1.2
WORKDIR /work
```

- [ ] **Step 3: Write the failing selection test**

```python
# backend/tests/test_sandbox_selection.py
from ley_khaa.executor.sandbox import DockerSandbox, SubprocessSandbox, pick_sandbox


def test_env_can_pin_the_subprocess_fallback(monkeypatch):
    monkeypatch.setattr("ley_khaa.executor.sandbox.settings.sandbox_backend", "subprocess")
    assert pick_sandbox().name == "subprocess"


def test_env_can_pin_docker_even_when_unavailable(monkeypatch):
    """An explicit pin must not be silently downgraded — that is how a reader
    ends up believing a bundle was isolated when it wasn't."""
    monkeypatch.setattr("ley_khaa.executor.sandbox.settings.sandbox_backend", "docker")
    monkeypatch.setattr(DockerSandbox, "available", lambda self: False)
    assert pick_sandbox().name == "docker"


def test_auto_prefers_docker_when_it_is_available(monkeypatch):
    monkeypatch.setattr("ley_khaa.executor.sandbox.settings.sandbox_backend", "auto")
    monkeypatch.setattr(DockerSandbox, "available", lambda self: True)
    assert pick_sandbox().name == "docker"


def test_auto_falls_back_when_docker_is_not_available(monkeypatch):
    monkeypatch.setattr("ley_khaa.executor.sandbox.settings.sandbox_backend", "auto")
    monkeypatch.setattr(DockerSandbox, "available", lambda self: False)
    assert pick_sandbox().name == "subprocess"


def test_the_named_volume_is_mounted_by_name(monkeypatch):
    """Under compose the backend is itself a container, so a bind mount of its
    own path would point at a host path that does not exist."""
    sandbox = DockerSandbox(image="img", volume="ley-khaa-task-workspaces", volume_target="/work/task-workspaces")
    args = sandbox._mount_args("/work/task-workspaces/task-1")
    assert args == [
        "--mount",
        "type=volume,source=ley-khaa-task-workspaces,target=/work/task-workspaces",
    ]


def test_a_plain_directory_is_bind_mounted_at_the_same_path(tmp_path):
    sandbox = DockerSandbox(image="img")
    args = sandbox._mount_args(str(tmp_path))
    assert args == ["-v", f"{tmp_path}:{tmp_path}"]
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `cd backend && python -m pytest tests/test_sandbox_selection.py -v`
Expected: FAIL — `ImportError: cannot import name 'DockerSandbox'`

- [ ] **Step 5: Add DockerSandbox and the selector**

Append to `backend/ley_khaa/executor/sandbox.py` (and add `import uuid` plus `from ..config import settings` to the imports at the top):

```python
class DockerSandbox:
    """The real thing (spec §5.10): no network, read-only rootfs, capped."""

    name = "docker"

    def __init__(
        self,
        *,
        image: str,
        memory_mb: int = 512,
        volume: str | None = None,
        volume_target: str | None = None,
    ) -> None:
        self.image = image
        self.memory_mb = memory_mb
        self.volume = volume
        self.volume_target = volume_target

    def available(self) -> bool:
        """True only if a daemon answers AND our image exists.

        Checking the image too is what lets "auto" fall back cleanly on a
        machine that has Docker but has never built the sandbox, instead of
        failing every task with an obscure `docker run` error.
        """
        for command in (["docker", "info"], ["docker", "image", "inspect", self.image]):
            try:
                completed = subprocess.run(command, capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if completed.returncode != 0:
                return False
        return True

    def _mount_args(self, workspace: str) -> list[str]:
        if self.volume and self.volume_target:
            # Under compose the backend is itself a container and spawns SIBLING
            # containers on the host daemon, so a bind mount of the backend's own
            # container path would resolve to a host path that does not exist.
            # The workspace therefore lives on a named volume mounted at the SAME
            # path on both sides, and paths line up without translation.
            return [
                "--mount",
                f"type=volume,source={self.volume},target={self.volume_target}",
            ]
        return ["-v", f"{workspace}:{workspace}"]

    def run(self, *, script: Path, workspace: Path, timeout_s: int) -> SandboxResult:
        workspace = workspace.resolve()
        container = f"ley-khaa-{uuid.uuid4().hex[:12]}"
        command = [
            "docker", "run", "--rm",
            "--name", container,
            # The §5.10 guarantee: synthesized code reaches nothing.
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:size=64m,exec",
            "--memory", f"{self.memory_mb}m",
            "--cpus", "1",
            "--pids-limit", "64",
            # Run as the caller so the deliverable is owned by us and readable
            # back out of a bind-mounted workspace.
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            *self._mount_args(str(workspace)),
            "-w", str(workspace),
            self.image,
            "python", str(script.resolve()),
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            # docker run --rm leaves the container going after we stop waiting.
            subprocess.run(["docker", "kill", container], capture_output=True)
            return SandboxResult(
                exit_code=-1,
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr) + f"\nkilled after {timeout_s}s",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"could not invoke docker: {exc}") from exc

        # 125 is docker's own "could not start the container" code. That is our
        # infrastructure failing, not the script failing, and the two must not be
        # confused: one is a bug report, the other is a question for a human.
        if completed.returncode == 125:
            raise SandboxUnavailable(f"docker could not start the sandbox: {completed.stderr}")

        return SandboxResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
        )


_warned_about_fallback = False


def pick_sandbox() -> SandboxRunner:
    """Docker unless we cannot, subprocess when we must, never a silent swap."""
    subprocess_sandbox = SubprocessSandbox(memory_mb=settings.sandbox_memory_mb)
    if settings.sandbox_backend == "subprocess":
        return subprocess_sandbox

    docker = DockerSandbox(
        image=settings.sandbox_image,
        memory_mb=settings.sandbox_memory_mb,
        volume=settings.workspace_volume,
        volume_target=settings.workspace_root if settings.workspace_volume else None,
    )
    if settings.sandbox_backend == "docker":
        # An explicit pin is never downgraded: quietly running somewhere weaker
        # than the operator asked for is how a reader ends up trusting a bundle
        # that was never isolated.
        return docker
    if docker.available():
        return docker

    global _warned_about_fallback
    if not _warned_about_fallback:
        _warned_about_fallback = True
        logger.warning(
            "No usable Docker sandbox (daemon or image %s missing) — falling back to "
            "SubprocessSandbox. Synthesized scripts will run on this machine's "
            "interpreter with CPU and memory caps and a scrubbed environment, but "
            "WITHOUT network isolation. The manifest records this.",
            settings.sandbox_image,
        )
    return subprocess_sandbox
```

- [ ] **Step 6: Add Docker to the contract suite**

In `backend/tests/test_sandbox_contract.py`, replace the `RUNNERS` definition:

```python
import shutil

import pytest

from ley_khaa.executor.sandbox import DockerSandbox, SubprocessSandbox

_docker = DockerSandbox(image="ley-khaa-sandbox")
# Skipped unless a daemon answers AND the image is built. CI builds it, so the
# real sandbox is genuinely exercised there rather than only in theory.
_no_docker = not _docker.available()

RUNNERS = [
    pytest.param(SubprocessSandbox(), id="subprocess"),
    pytest.param(
        _docker,
        id="docker",
        marks=[
            pytest.mark.docker,
            pytest.mark.skipif(_no_docker, reason="no docker daemon or sandbox image"),
        ],
    ),
]
```

Register the marker in `backend/pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
markers = [
    "docker: requires a Docker daemon and the ley-khaa-sandbox image",
]
```

- [ ] **Step 7: Run the tests**

Run: `cd backend && python -m pytest tests/test_sandbox_selection.py tests/test_sandbox_contract.py -v`
Expected: PASS — selection tests pass; docker contract params SKIP unless the image is built.

Optional local check of the real sandbox:

```bash
docker build -t ley-khaa-sandbox backend/sandbox
cd backend && python -m pytest tests/test_sandbox_contract.py -v
```
Expected: the `docker` params now run and pass.

- [ ] **Step 8: Wire compose**

Replace the `backend` service in `docker-compose.yml` and add the volume and image-builder service:

```yaml
  backend:
    build: ./backend
    environment:
      DATABASE_URL: postgresql+psycopg://ley:ley@db:5432/leykhaa
      # Without these the backend silently runs the offline HeuristicLLM stand-in
      # even for a user who has exported a key. Empty means "no key": the factory
      # falls back and says so in the log.
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
      LEY_KHAA_LLM: ${LEY_KHAA_LLM:-anthropic}
      # Bundles live on a named volume mounted at this same path in the sandbox
      # container, so a path written here is valid there too.
      LEY_KHAA_WORKSPACE_ROOT: /work/task-workspaces
      LEY_KHAA_WORKSPACE_VOLUME: ley-khaa-task-workspaces
    volumes:
      # The backend spawns sibling sandbox containers on the host daemon.
      - /var/run/docker.sock:/var/run/docker.sock
      - task-workspaces:/work/task-workspaces
    ports:
      - "8000:8000"
    depends_on:
      db:
        condition: service_healthy
      sandbox-image:
        condition: service_completed_successfully

  sandbox-image:
    build: ./backend/sandbox
    image: ley-khaa-sandbox
    # Exists only so `docker compose up` builds the sandbox image on the host
    # daemon before the backend needs it. It runs once and exits.
    command: ["python", "-c", "print('sandbox image ready')"]
    restart: "no"
```

And at the bottom of the file:

```yaml
volumes:
  task-workspaces:
    # Named explicitly: DockerSandbox mounts it by name, and the compose default
    # (<project>_task-workspaces) would change if the directory were renamed.
    name: ley-khaa-task-workspaces
```

- [ ] **Step 9: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: all pass

- [ ] **Step 10: Commit**

```bash
git add backend/ley_khaa/executor/sandbox.py backend/sandbox/Dockerfile backend/pyproject.toml \
        backend/tests/test_sandbox_contract.py backend/tests/test_sandbox_selection.py docker-compose.yml
git commit -m "feat(executor): docker sandbox, its image, and compose wiring"
```

---

### Task 6: Synthesizer and the SYNTHESIS routing stage

**Files:**
- Create: `backend/ley_khaa/executor/formats.py`
- Create: `backend/ley_khaa/executor/synthesizer.py`
- Modify: `backend/ley_khaa/llm/router.py`
- Test: `backend/tests/test_formats.py`
- Test: `backend/tests/test_synthesizer.py`

**Interfaces:**
- Consumes: `ResolvedInput` (Task 2); `LLMClient`, `FakeLLM`, `ModelChoice`, `model_for` (all existing).
- Produces: `formats.expected_suffixes(output_format) -> tuple[str, ...]`, `formats.deliverable_filename(output_format) -> str`; `SynthesizedScript(reasoning: str, source: str)`; `Synthesizer(llm)` with `.synthesize(spec, resolved)` and `.repair(spec, resolved, previous=, result=, verdict=)`. Tasks 7, 8, and 9 consume these.

- [ ] **Step 1: Write the failing formats test**

```python
# backend/tests/test_formats.py
from ley_khaa.executor.formats import deliverable_filename, expected_suffixes


def test_spoken_format_names_map_to_suffixes():
    assert expected_suffixes("xlsx") == (".xlsx",)
    assert expected_suffixes("Excel") == (".xlsx",)
    assert expected_suffixes("csv") == (".csv",)
    assert expected_suffixes("word") == (".docx",)


def test_an_unrecognised_format_has_no_opinion():
    """Rejecting a good deliverable because the request described it in words we
    did not anticipate would be worse than not checking at all."""
    assert expected_suffixes("a nicely formatted table") == ()
    assert expected_suffixes("") == ()


def test_deliverable_filename_follows_the_format():
    assert deliverable_filename("xlsx") == "output.xlsx"
    assert deliverable_filename("csv") == "output.csv"
    assert deliverable_filename("something odd") == "output.txt"
```

- [ ] **Step 2: Write formats.py**

```python
# backend/ley_khaa/executor/formats.py
"""Mapping between the words a request uses for an output and a file suffix.

Shared by the synthesizer (which tells the script what to write) and the
validator (which checks what it wrote), so the two can never disagree about
what "Excel" means.
"""
from __future__ import annotations

_SUFFIXES: dict[str, tuple[str, ...]] = {
    "xlsx": (".xlsx",),
    "excel": (".xlsx",),
    "spreadsheet": (".xlsx",),
    "csv": (".csv",),
    "docx": (".docx",),
    "word": (".docx",),
    "markdown": (".md",),
    "md": (".md",),
    "json": (".json",),
    "text": (".txt",),
}


def expected_suffixes(output_format: str) -> tuple[str, ...]:
    """Suffixes that satisfy this format. Empty means "no opinion".

    An unrecognised format must NOT fail validation: rejecting a perfectly good
    deliverable because the request described it in words we did not anticipate
    is worse than not checking.
    """
    return _SUFFIXES.get((output_format or "").strip().lower(), ())


def deliverable_filename(output_format: str) -> str:
    suffixes = expected_suffixes(output_format)
    return f"output{suffixes[0]}" if suffixes else "output.txt"
```

- [ ] **Step 3: Add the SYNTHESIS stage to the router**

In `backend/ley_khaa/llm/router.py`, add the enum member, policy row, and token budget:

```python
class Stage(str, Enum):
    RELEVANCE_FILTER = "relevance_filter"
    CRYSTALLIZER = "crystallizer"
    INTERPRETER = "interpreter"
    VISION_EXTRACTION = "vision_extraction"
    SYNTHESIS = "synthesis"
```

```python
    Stage.VISION_EXTRACTION: {"routine": OPUS, "hard": OPUS},
    # Writing correct code from an under-specified request is the hardest thing
    # the system does, and a wrong script costs a sandbox round trip plus a
    # repair. Opus at both complexities.
    Stage.SYNTHESIS: {"routine": OPUS, "hard": OPUS},
}
```

```python
    Stage.VISION_EXTRACTION: 8000,
    # Emits a whole program, not a small structured object.
    Stage.SYNTHESIS: 16000,
}
```

- [ ] **Step 4: Write the failing synthesizer test**

```python
# backend/tests/test_synthesizer.py
from ley_khaa.executor.resolver import ResolvedInput
from ley_khaa.executor.sandbox import SandboxResult
from ley_khaa.executor.synthesizer import SynthesizedScript, Synthesizer
from ley_khaa.executor.validator import Verdict
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.router import OPUS


def _spec() -> TaskSpec:
    return TaskSpec(
        intent="find securities in Bloomberg that are missing from FactSet",
        inputs=["Bloomberg universe", "FactSet"],
        operation="set_difference",
        output_format="xlsx",
        certainty=0.9,
    )


def _resolved() -> list[ResolvedInput]:
    return [
        ResolvedInput("Bloomberg universe", "bloomberg_universe.csv", "ticker\nAAA\nBBB\n", "catalog"),
        ResolvedInput("FactSet", "factset_universe.csv", "ticker\nAAA\n", "catalog"),
    ]


def _script(source="print('ok')") -> SynthesizedScript:
    return SynthesizedScript(reasoning="because", source=source)


def test_synthesis_routes_to_opus_with_room_to_write_a_program():
    llm = FakeLLM(responses=[_script()])
    Synthesizer(llm).synthesize(_spec(), _resolved())
    choice = llm.calls[0].choice
    assert choice.model == OPUS
    assert choice.supports_thinking is True
    assert choice.max_tokens == 16000


def test_the_prompt_names_the_files_the_script_will_actually_find():
    llm = FakeLLM(responses=[_script()])
    Synthesizer(llm).synthesize(_spec(), _resolved())
    prompt = llm.calls[0].user
    assert "inputs/bloomberg_universe.csv" in prompt
    assert "inputs/factset_universe.csv" in prompt
    assert "deliverable/output.xlsx" in prompt


def test_the_prompt_shows_a_preview_of_each_input():
    """The model needs the real column names, or it invents plausible ones."""
    llm = FakeLLM(responses=[_script()])
    Synthesizer(llm).synthesize(_spec(), _resolved())
    assert "ticker" in llm.calls[0].user


def test_the_system_prompt_states_the_sandbox_rules():
    llm = FakeLLM(responses=[_script()])
    Synthesizer(llm).synthesize(_spec(), _resolved())
    system = llm.calls[0].system
    assert "no network" in system.lower()
    assert "openpyxl" in system


def test_repair_is_given_the_previous_source_and_the_failure():
    llm = FakeLLM(responses=[_script("fixed")])
    result = SandboxResult(
        exit_code=1, stdout="", stderr="KeyError: 'ticker_id'", duration_ms=12, timed_out=False
    )
    verdict = Verdict(ok=False, reason="The generated script failed while running.", checks={})
    out = Synthesizer(llm).repair(
        _spec(), _resolved(), previous="broken source", result=result, verdict=verdict
    )
    prompt = llm.calls[0].user
    assert "broken source" in prompt
    assert "KeyError: 'ticker_id'" in prompt
    assert out.source == "fixed"


def test_repair_truncates_a_giant_traceback():
    """A runaway script can emit megabytes of stderr; that must not become the
    prompt."""
    llm = FakeLLM(responses=[_script()])
    result = SandboxResult(
        exit_code=1, stdout="", stderr="x" * 50_000, duration_ms=1, timed_out=False
    )
    verdict = Verdict(ok=False, reason="failed", checks={})
    Synthesizer(llm).repair(
        _spec(), _resolved(), previous="src", result=result, verdict=verdict
    )
    assert len(llm.calls[0].user) < 20_000
```

- [ ] **Step 5: Run the test to verify it fails**

Run: `cd backend && python -m pytest tests/test_synthesizer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.executor.synthesizer'`

- [ ] **Step 6: Write the synthesizer**

```python
# backend/ley_khaa/executor/synthesizer.py
"""Turn a TaskSpec plus real inputs into one Python script (spec §5.10).

This is the default lane. The registry fast path is Phase 4; until then every
task that gets this far is solved by a program written for it.
"""
from __future__ import annotations

from pydantic import BaseModel

from ..interpreter.spec import TaskSpec
from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from .formats import deliverable_filename
from .resolver import ResolvedInput
from .sandbox import SandboxResult
from .validator import Verdict

# How much of each input the model sees. Enough to learn the real column names
# without pasting a 200-row universe into the prompt.
_PREVIEW_LINES = 8
# A crashing script can emit megabytes of stderr. The tail is the part that
# carries the exception.
_MAX_STDERR = 4000


class SynthesizedScript(BaseModel):
    """What the model returns. `reasoning` is kept in the manifest so a reader
    can see why the script is shaped the way it is."""

    reasoning: str
    source: str


SYSTEM = """You write a single, self-contained Python script that solves one data task.

The script runs in a locked-down sandbox:
- Working directory contains inputs/ (read these) and deliverable/ (write here).
- Available libraries: the Python 3.12 standard library, openpyxl, and python-docx.
  pandas and numpy are NOT installed. Use the csv module.
- There is no network. Do not import requests, urllib, or anything that dials out.
- Do not read or write anything outside deliverable/. Never modify inputs/.
- Be deterministic: no randomness, no timestamps in the output, no reliance on
  dict ordering that the input does not guarantee. The same inputs must produce
  the same bytes every run — the whole bundle is audited on that.
- Print one short human-readable summary line at the end.
- Write exactly one deliverable file, at the path given in the task.

Return the complete script in `source` and one or two sentences in `reasoning`
about the approach. No markdown fences, no commentary outside those fields."""


def _preview(item: ResolvedInput) -> str:
    lines = item.content.splitlines()[:_PREVIEW_LINES]
    body = "\n".join(lines)
    return f"### inputs/{item.filename}  (spec input: {item.name}, source: {item.source})\n{body}"


def _task_block(spec: TaskSpec, resolved: list[ResolvedInput]) -> str:
    target = f"deliverable/{deliverable_filename(spec.output_format)}"
    previews = "\n\n".join(_preview(item) for item in resolved)
    return (
        f"## Task\n"
        f"intent: {spec.intent}\n"
        f"operation: {spec.operation}\n"
        f"output_format: {spec.output_format}\n"
        f"write the result to: {target}\n"
        f"\n## Inputs (first {_PREVIEW_LINES} lines of each)\n{previews}\n"
    )


class Synthesizer:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def _parse(self, user: str) -> SynthesizedScript:
        return self.llm.parse(
            choice=model_for(Stage.SYNTHESIS),
            system=SYSTEM,
            user=user,
            output_format=SynthesizedScript,
        )

    def synthesize(self, spec: TaskSpec, resolved: list[ResolvedInput]) -> SynthesizedScript:
        return self._parse(_task_block(spec, resolved))

    def repair(
        self,
        spec: TaskSpec,
        resolved: list[ResolvedInput],
        *,
        previous: str,
        result: SandboxResult,
        verdict: Verdict,
    ) -> SynthesizedScript:
        """One more go, given what went wrong.

        The traceback is information the model can act on and a human cannot,
        which is why this happens before anyone is asked a question.
        """
        stderr = result.stderr[-_MAX_STDERR:]
        user = (
            f"{_task_block(spec, resolved)}\n"
            f"## Your previous attempt failed\n"
            f"verdict: {verdict.reason}\n"
            f"exit code: {result.exit_code}"
            f"{' (killed on timeout)' if result.timed_out else ''}\n"
            f"\n### previous source\n{previous}\n"
            f"\n### stderr (last {_MAX_STDERR} chars)\n{stderr}\n"
            f"\nFix the cause and return the complete corrected script."
        )
        return self._parse(user)
```

- [ ] **Step 7: Run the tests**

Run: `cd backend && python -m pytest tests/test_formats.py tests/test_synthesizer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.executor.validator'`. `Verdict` lands in Task 8; that import is the forward reference. Create a minimal placeholder now so this task is testable on its own, and Task 8 fills it in:

```python
# backend/ley_khaa/executor/validator.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    checks: dict[str, bool]
```

- [ ] **Step 8: Re-run the tests**

Run: `cd backend && python -m pytest tests/test_formats.py tests/test_synthesizer.py -v`
Expected: PASS (9 tests)

- [ ] **Step 9: Commit**

```bash
git add backend/ley_khaa/executor/formats.py backend/ley_khaa/executor/synthesizer.py \
        backend/ley_khaa/executor/validator.py backend/ley_khaa/llm/router.py \
        backend/tests/test_formats.py backend/tests/test_synthesizer.py
git commit -m "feat(executor): synthesizer and the SYNTHESIS routing stage"
```

---

## ⚠️ PLAN INCOMPLETE — resume here

Tasks 1–6 above are complete and ready to execute. Tasks 7–13 are **outlined only**
and must be written out in full (TDD steps with real test code and real
implementation code) before this plan is executed. Do not start executing at
Task 7 against these summaries.

- **Task 7 — Offline synthesis rule.** Extend `HeuristicLLM.parse` with a
  `SynthesizedScript` branch returning genuine, runnable stdlib+openpyxl scripts
  for `set_difference` and `summary_stats`, keyed off `spec.operation` parsed out
  of the prompt. This is what makes the no-API-key fresh clone emit a real
  `.xlsx`. State in the README that offline synthesis is canned, not generated.
- **Task 8 — Validator.** Replace the Task 6 placeholder with the real
  `validate(spec, workspace, result, input_hashes) -> Verdict`. Checks, in the
  order their failure is reported: `script_ran`, `within_time_limit`,
  `deliverable_exists`, `deliverable_not_empty`, `format_matches` (via
  `formats.expected_suffixes`, skipped when empty), `inputs_unmodified` (re-hash
  against `Workspace.input_hashes()`), `has_rows` (stdlib `csv` for `.csv`,
  `openpyxl` for `.xlsx`, skipped otherwise). `reason` is plain English.
- **Task 9 — ExecutionRunner.** `run(row, spec) -> ExecutionOutcome(verdict,
  workspace_path, attempts)`. Resolve → workspace → synthesize → run → validate,
  with `_MAX_ATTEMPTS = 2` (one repair). Catches `UnresolvedInputs` and returns a
  failing `Verdict` rather than raising — see Task 10 for why. Lets
  `SandboxUnavailable` propagate. Writes `manifest.json`. Needs a `FakeSandbox`
  test double to keep the suite fast.
- **Task 10 — Schema and driver wiring.** Alembic `0003_executor` +
  `TaskRow.workspace_path` / `execution_verdict` + `TaskRepository.save_execution`.
  Rewire `_execute` (run, persist verdict, → `VALIDATING`; `SandboxUnavailable` →
  `record_failure` + `FAILED`) and `_validate` (read verdict; ok → `DONE`, else
  `set_open_question(verdict.reason)` → `NEEDS_CLARIFICATION`).
  **This is why unresolved inputs return a Verdict instead of raising:**
  `EXECUTING → NEEDS_CLARIFICATION` is not a legal edge, but
  `VALIDATING → NEEDS_CLARIFICATION` already is, so routing the failure through
  the verdict keeps `domain/states.py` untouched exactly as decision 6 requires.
- **Task 11 — Bundle API.** `GET /tasks/{id}/bundle`, `.../bundle/file?path=`
  (path-traversal guard: `resolve()` then `is_relative_to` the workspace root,
  else 400), `.../bundle/download` (zip via `io.BytesIO` + `StreamingResponse`).
  Extend `TaskOut` with `workspace_path` and `execution_verdict`.
- **Task 12 — Dashboard.** `frontend/src/BundlePanel.tsx` (manifest summary,
  generator source, download links) + `api.ts` types and calls + mount in
  `TaskDetail.tsx`. Vitest with mocked fetch.
- **Task 13 — End-to-end, reproducibility, and release.** Offline E2E: golden
  conversation → `done` with a real `.xlsx`. Reproducibility test: re-run
  `generator/` from `inputs/` and compare — **parsed cell values for `.xlsx`
  (it is a zip and embeds timestamps, so byte-hashing is flaky), raw bytes for
  CSV/JSON**. README + CHANGELOG, version bump, tag `v0.4.0`.
