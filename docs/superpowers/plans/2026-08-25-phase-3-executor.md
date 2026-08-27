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
| `backend/ley_khaa/executor/formats.py` | output-format words → file suffixes, shared by synthesizer and validator |
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

### Task 7: Offline synthesis, and the input names it has to resolve

**Files:**
- Modify: `backend/ley_khaa/llm/heuristic.py`
- Modify: `backend/tests/test_heuristic_llm.py`
- Modify: `README.md`
- Test: `backend/tests/test_heuristic_synthesis.py`

**Interfaces:**
- Consumes: `SynthesizedScript`, `Synthesizer` (Task 6); `ResolvedInput` (Task 2); `Workspace` (Task 3); `SubprocessSandbox` (Task 4); `catalog` (Task 1).
- Produces: `HeuristicLLM.parse(..., output_format=SynthesizedScript)` returning genuinely runnable source. Task 13's offline end-to-end rests entirely on this.

**Why this task also changes `_interpret`.** The golden `messy_universe_check` conversation says
"compare the Bloomberg universe against FactSet". `_SOURCE_WORDS` matches single words, so the
heuristic emits `inputs = ["bloomberg", "factset", "universe"]` — and the bare name `"universe"`
matches *both* catalog datasets, so `catalog.resolve_name` returns `None` and Task 2 raises
`UnresolvedInputs`. The golden conversation would stop at `needs_clarification` asking about a gap
that does not exist, and §9's definition of done would be unreachable. `"bloomberg universe"` is one
input, not two, so the fix is to match phrases longest-first and drop any shorter name whose words a
longer match already covered. Both fixtures then resolve: `["bloomberg universe", "factset"]` and
`["holdings", "portfolio"]`.

- [ ] **Step 1: Write the failing test for phrase-shaped input names**

Append to `backend/tests/test_heuristic_llm.py`:

```python
def test_a_multi_word_source_is_one_input_not_two():
    """"bloomberg universe" is one dataset. Emitting the bare word "universe"
    alongside it produces an input that matches two catalog datasets and is
    therefore unresolvable — a clarification about a gap that isn't real."""
    spec = _interpret(_UNIVERSE_PROMPT)
    assert spec.inputs == ["bloomberg universe", "factset"]


def test_single_word_sources_still_come_through():
    spec = _interpret(
        "## Messages\n[m1] alice: compare the holdings against the portfolio as csv"
    )
    assert spec.inputs == ["holdings", "portfolio"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python -m pytest tests/test_heuristic_llm.py -v -k source`
Expected: FAIL — `assert ['bloomberg', 'factset', 'universe'] == ['bloomberg universe', 'factset']`

- [ ] **Step 3: Match source phrases longest-first**

In `backend/ley_khaa/llm/heuristic.py`, replace the `_SOURCE_WORDS` tuple:

```python
# Longest first. A shorter name whose words a longer match already covered is
# dropped: "bloomberg universe" is one dataset, and also emitting the bare word
# "universe" yields an input that matches TWO catalog datasets, which the
# resolver correctly refuses to guess between (executor/catalog.py).
_SOURCE_PHRASES = (
    "bloomberg universe",
    "factset universe",
    "bloomberg",
    "factset",
    "holdings",
    "portfolio",
    "trades",
    "universe",
)
```

Add the extractor beside `_first_match` at the bottom of the file:

```python
def _sources(blob: str) -> list[str]:
    found: list[str] = []
    covered: set[str] = set()
    for phrase in _SOURCE_PHRASES:
        if phrase not in blob:
            continue
        words = set(phrase.split())
        if words <= covered:
            continue
        found.append(phrase)
        covered |= words
    return found
```

And in `_interpret`, replace the `inputs` line:

```python
        inputs = _sources(blob)
```

- [ ] **Step 4: Run the interpreter tests**

Run: `cd backend && python -m pytest tests/test_heuristic_llm.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing synthesis test**

```python
# backend/tests/test_heuristic_synthesis.py
"""The offline lane end of the synthesizer contract.

These tests run the canned scripts for real, in the real sandbox, against the
real catalog. The heuristic parses the prompt the Synthesizer builds, so a
change to either side that breaks the pair fails here rather than silently
turning the no-API-key demo into an empty bundle.
"""
from openpyxl import load_workbook

from ley_khaa.executor import catalog
from ley_khaa.executor.resolver import ResolvedInput
from ley_khaa.executor.sandbox import SubprocessSandbox
from ley_khaa.executor.synthesizer import Synthesizer
from ley_khaa.executor.workspace import Workspace
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.heuristic import HeuristicLLM


def _spec(operation="set_difference", output_format="xlsx") -> TaskSpec:
    return TaskSpec(
        intent="find what is missing",
        inputs=["Bloomberg universe", "FactSet"],
        operation=operation,
        output_format=output_format,
        certainty=0.55,
    )


def _universes() -> list[ResolvedInput]:
    return [
        ResolvedInput(
            name="Bloomberg universe",
            filename="bloomberg_universe.csv",
            content=catalog.build_dataset("bloomberg_universe"),
            source="catalog",
        ),
        ResolvedInput(
            name="FactSet",
            filename="factset_universe.csv",
            content=catalog.build_dataset("factset_universe"),
            source="catalog",
        ),
    ]


def _run(tmp_path, spec, resolved):
    """Synthesize offline, then actually execute the result."""
    script = Synthesizer(HeuristicLLM()).synthesize(spec, resolved)
    workspace = Workspace.create(tmp_path, "t1")
    workspace.write_inputs(resolved)
    path = workspace.write_generator(1, script.source)
    result = SubprocessSandbox().run(script=path, workspace=workspace.root, timeout_s=60)
    return workspace, result


def test_the_offline_set_difference_actually_writes_a_spreadsheet(tmp_path):
    workspace, result = _run(tmp_path, _spec(), _universes())
    assert result.ok, result.stderr
    book = load_workbook(workspace.deliverable_dir / "output.xlsx")
    # Task 1 guarantees bloomberg has exactly 5 tickers factset lacks; header + 5.
    assert book.active.max_row == 6
    assert [cell.value for cell in book.active[1]][0] == "ticker"


def test_the_offline_lane_honours_a_csv_request(tmp_path):
    workspace, result = _run(tmp_path, _spec(output_format="csv"), _universes())
    assert result.ok, result.stderr
    lines = (workspace.deliverable_dir / "output.csv").read_text().splitlines()
    assert len(lines) == 6


def test_summary_stats_describes_the_numeric_columns(tmp_path):
    resolved = [
        ResolvedInput(
            name="holdings",
            filename="holdings.csv",
            content=catalog.build_dataset("holdings"),
            source="catalog",
        )
    ]
    spec = _spec(operation="summary_stats", output_format="csv")
    workspace, result = _run(tmp_path, spec, resolved)
    assert result.ok, result.stderr
    body = (workspace.deliverable_dir / "output.csv").read_text()
    assert body.startswith("column,count,min,max,mean\n")
    assert "quantity" in body


def test_an_unrecognised_operation_still_produces_an_honest_deliverable(tmp_path):
    """The offline stand-in must never leave the fresh-clone demo with no
    bundle. A request it cannot pattern-match gets a truthful description of the
    inputs rather than an exception."""
    spec = _spec(operation="reconcile_everything", output_format="csv")
    workspace, result = _run(tmp_path, spec, _universes())
    assert result.ok, result.stderr
    body = (workspace.deliverable_dir / "output.csv").read_text()
    assert "bloomberg_universe.csv" in body


def test_the_canned_script_never_touches_its_inputs(tmp_path):
    """Reproducibility is the bundle's whole claim, and the validator enforces
    it — the offline lane must not be the thing that trips it."""
    workspace, result = _run(tmp_path, _spec(), _universes())
    assert result.ok, result.stderr
    before = {
        item.filename: item.sha256 for item in _universes()
    }
    assert workspace.input_hashes() == before


def test_the_offline_script_is_deterministic(tmp_path):
    first, _ = _run(tmp_path / "a", _spec(output_format="csv"), _universes())
    second, _ = _run(tmp_path / "b", _spec(output_format="csv"), _universes())
    assert (first.deliverable_dir / "output.csv").read_bytes() == (
        second.deliverable_dir / "output.csv"
    ).read_bytes()
```

- [ ] **Step 6: Run it to verify it fails**

Run: `cd backend && python -m pytest tests/test_heuristic_synthesis.py -v`
Expected: FAIL — `NotImplementedError: HeuristicLLM has no rule for SynthesizedScript`

- [ ] **Step 7: Add the canned scripts**

At the top of `backend/ley_khaa/llm/heuristic.py`, add the import (`executor.synthesizer` imports
`llm.client` and `llm.router`, never `llm.heuristic`, so there is no cycle):

```python
from ..executor.synthesizer import SynthesizedScript
```

Then add the script templates below `_HEURISTIC_CERTAINTY`. They are assembled by concatenation,
not by `str.format`, so the braces in the emitted Python need no escaping:

```python
# The prompt the Synthesizer builds (executor/synthesizer.py::_task_block).
_SYNTH_OPERATION = re.compile(r"^operation:\s*(?P<operation>.+)$", re.MULTILINE)
_SYNTH_TARGET = re.compile(r"^write the result to:\s*(?P<target>\S+)$", re.MULTILINE)
_SYNTH_INPUT = re.compile(r"^### inputs/(?P<filename>\S+)", re.MULTILINE)

_PREAMBLE = '''"""Offline canned generator.

Written by ley-khaa's deterministic offline stand-in, not by a model. It is a
real, runnable program: it reads the frozen inputs and writes the deliverable,
so a fresh clone with no ANTHROPIC_API_KEY still produces a genuine bundle.
"""
import csv


def read_rows(name):
    with open("inputs/" + name, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_rows(target, fields, rows):
    if target.endswith(".xlsx"):
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.title = "result"
        sheet.append(fields)
        for row in rows:
            sheet.append([row.get(field, "") for field in fields])
        book.save(target)
        return
    # Everything else is written as CSV text. The offline stand-in covers the
    # two demo shapes honestly rather than pretending to write Word.
    with open(target, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})

'''

_SET_DIFFERENCE = '''
left = read_rows(INPUTS[0])
right = read_rows(INPUTS[1]) if len(INPUTS) > 1 else []
fields = list(left[0].keys()) if left else ["ticker"]
key = fields[0]
seen = {row.get(key) for row in right}
missing = [row for row in left if row.get(key) not in seen]
write_rows(TARGET, fields, missing)
print("%d of %d rows keyed on %s are missing from the second input"
      % (len(missing), len(left), key))
'''

_SUMMARY_STATS = '''
rows = read_rows(INPUTS[0])
summary = []
for field in list(rows[0].keys()) if rows else []:
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
write_rows(TARGET, ["column", "count", "min", "max", "mean"], summary)
print("summarised %d numeric column(s) over %d row(s)" % (len(summary), len(rows)))
'''

_INVENTORY = '''
summary = []
for name in INPUTS:
    rows = read_rows(name)
    summary.append({
        "input": name,
        "rows": str(len(rows)),
        "columns": ", ".join(rows[0].keys()) if rows else "",
    })
write_rows(TARGET, ["input", "rows", "columns"], summary)
print("described %d input file(s)" % len(summary))
'''

_BODIES = {
    "set_difference": (_SET_DIFFERENCE, "rows in the first input whose key is absent from the second"),
    "summary_stats": (_SUMMARY_STATS, "count, min, max and mean of every numeric column"),
}
```

- [ ] **Step 8: Dispatch to them**

In `HeuristicLLM.parse`, add the branch (before the `raise`):

```python
        if output_format is SynthesizedScript:
            return self._synthesize(user)
```

And add the method:

```python
    def _synthesize(self, user: str) -> SynthesizedScript:
        """Canned, not generated. See the README: offline synthesis is a lookup.

        These two scripts are the honest precursor to the Phase 4 registry —
        the same idea (a proven script for a known shape), chosen by keyword
        instead of by a matcher.
        """
        operation_match = _SYNTH_OPERATION.search(user)
        target_match = _SYNTH_TARGET.search(user)
        operation = operation_match.group("operation").strip() if operation_match else ""
        target = target_match.group("target").strip() if target_match else "deliverable/output.txt"
        inputs = _SYNTH_INPUT.findall(user)

        body, approach = _BODIES.get(operation, (_INVENTORY, "a description of each input"))
        if not inputs:
            # set_difference with nothing to read would IndexError. Describing
            # an empty input set produces an empty table, which the validator
            # rejects for having no rows — a clear failure instead of a crash.
            body, approach = _INVENTORY, "a description of each input"

        source = _PREAMBLE + f"INPUTS = {inputs!r}\nTARGET = {target!r}\n" + body
        return SynthesizedScript(
            reasoning=f"Offline canned script for {operation or 'an unrecognised operation'}: {approach}.",
            source=source,
        )
```

- [ ] **Step 9: Run the tests**

Run: `cd backend && python -m pytest tests/test_heuristic_synthesis.py tests/test_heuristic_llm.py -v`
Expected: PASS (6 synthesis tests, all heuristic tests)

- [ ] **Step 10: Say so in the README**

In `README.md`, under `### Which model actually runs`, append:

```markdown
**Offline synthesis is canned, not generated.** With no `ANTHROPIC_API_KEY` the executor still
produces a real, runnable script and a real deliverable — but that script is looked up by keyword
from two hand-written templates (`set_difference`, `summary_stats`), not written for the request.
Anything else gets a script that describes its inputs. The bundle's `manifest.json` records which
model produced the generator, so a bundle never overstates where its code came from.
```

- [ ] **Step 11: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: all pass

- [ ] **Step 12: Commit**

```bash
git add backend/ley_khaa/llm/heuristic.py backend/tests/test_heuristic_llm.py \
        backend/tests/test_heuristic_synthesis.py README.md
git commit -m "feat(executor): offline canned synthesis and phrase-shaped input names"
```

---

### Task 8: The validator

**Files:**
- Modify: `backend/ley_khaa/executor/validator.py`
- Test: `backend/tests/test_validator.py`

**Interfaces:**
- Consumes: `Workspace` (Task 3), `SandboxResult` (Task 4), `formats.expected_suffixes` (Task 6), `TaskSpec`.
- Produces: `validate(spec, workspace, result, input_hashes) -> Verdict`, replacing the Task 6 placeholder. `Verdict(ok, reason, checks)` keeps the shape Task 6 already imports. Task 9 calls `validate`.

**Report order.** Every check is computed and kept in `checks` for the manifest, but only the
first failure in report order becomes `reason`. A timeout is reported before a non-zero exit
because "it ran too long" is the actionable half of a fact that would otherwise read as "it
failed" — and a killed script has a non-zero exit code too, so the more specific one has to win.

**A spec rule with nowhere to live.** §6 also lists "any columns named in the spec are present".
`TaskSpec` has no column field — `inputs` holds dataset names, not columns — so there is nothing
to check against. It is deliberately not implemented rather than faked against `inputs`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_validator.py
import csv

from openpyxl import Workbook

from ley_khaa.executor.sandbox import SandboxResult
from ley_khaa.executor.validator import validate
from ley_khaa.executor.workspace import Workspace
from ley_khaa.interpreter.spec import TaskSpec


def _spec(output_format="xlsx") -> TaskSpec:
    return TaskSpec(
        intent="compare", inputs=["a", "b"], operation="set_difference",
        output_format=output_format, certainty=0.9,
    )


def _workspace(tmp_path) -> Workspace:
    workspace = Workspace.create(tmp_path, "t1")
    (workspace.inputs_dir / "a.csv").write_text("ticker\nAAA\n")
    return workspace


def _ok_result() -> SandboxResult:
    return SandboxResult(exit_code=0, stdout="done", stderr="", duration_ms=5, timed_out=False)


def _xlsx(workspace, name="output.xlsx", rows=1):
    book = Workbook()
    book.active.append(["ticker"])
    for index in range(rows):
        book.active.append([f"SYN{index}"])
    book.save(workspace.deliverable_dir / name)


def _csv(workspace, name="output.csv", rows=1):
    with (workspace.deliverable_dir / name).open("w", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["ticker"])
        for index in range(rows):
            writer.writerow([f"SYN{index}"])


def test_a_good_run_passes_every_check(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _xlsx(workspace)
    verdict = validate(_spec(), workspace, _ok_result(), hashes)
    assert verdict.ok
    assert all(verdict.checks.values())


def test_a_timeout_is_reported_as_a_timeout_not_as_a_failure(tmp_path):
    """A killed script also has a non-zero exit code, so the more specific
    reason has to win or the human is told the wrong thing."""
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    result = SandboxResult(exit_code=-1, stdout="", stderr="killed", duration_ms=1, timed_out=True)
    verdict = validate(_spec(), workspace, result, hashes)
    assert not verdict.ok
    assert "too long" in verdict.reason
    assert verdict.checks["within_time_limit"] is False


def test_a_crash_is_reported_plainly(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    result = SandboxResult(
        exit_code=1, stdout="", stderr="Traceback...\nKeyError: 'ticker'", duration_ms=3,
        timed_out=False,
    )
    verdict = validate(_spec(), workspace, result, hashes)
    assert not verdict.ok
    # The traceback belongs in the bundle, never in the question put to a human.
    assert "Traceback" not in verdict.reason
    assert "KeyError" not in verdict.reason


def test_a_clean_exit_with_no_output_file_fails(tmp_path):
    workspace = _workspace(tmp_path)
    verdict = validate(_spec(), workspace, _ok_result(), workspace.input_hashes())
    assert not verdict.ok
    assert verdict.checks["deliverable_exists"] is False


def test_an_empty_output_file_fails(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    (workspace.deliverable_dir / "output.xlsx").write_bytes(b"")
    verdict = validate(_spec(), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert verdict.checks["deliverable_not_empty"] is False


def test_the_wrong_format_fails_and_says_which(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _csv(workspace)
    verdict = validate(_spec("xlsx"), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert "output.csv" in verdict.reason


def test_an_unrecognised_format_is_not_second_guessed(tmp_path):
    """expected_suffixes() has no opinion here, and a check with no opinion must
    not reject a perfectly good deliverable."""
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _csv(workspace)
    verdict = validate(_spec("a nicely formatted table"), workspace, _ok_result(), hashes)
    assert verdict.ok


def test_a_script_that_rewrote_its_inputs_fails(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _xlsx(workspace)
    (workspace.inputs_dir / "a.csv").write_text("ticker\nTAMPERED\n")
    verdict = validate(_spec(), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert verdict.checks["inputs_unmodified"] is False
    assert "reproduc" in verdict.reason


def test_a_header_only_spreadsheet_has_no_rows(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _xlsx(workspace, rows=0)
    verdict = validate(_spec(), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert verdict.checks["has_rows"] is False


def test_a_header_only_csv_has_no_rows(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    _csv(workspace, rows=0)
    verdict = validate(_spec("csv"), workspace, _ok_result(), hashes)
    assert not verdict.ok
    assert verdict.checks["has_rows"] is False


def test_a_non_tabular_deliverable_is_not_row_counted(tmp_path):
    workspace = _workspace(tmp_path)
    hashes = workspace.input_hashes()
    (workspace.deliverable_dir / "output.md").write_text("# report\n\nall good\n")
    verdict = validate(_spec("markdown"), workspace, _ok_result(), hashes)
    assert verdict.ok
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python -m pytest tests/test_validator.py -v`
Expected: FAIL — `ImportError: cannot import name 'validate'`

- [ ] **Step 3: Write the validator**

Replace the whole of `backend/ley_khaa/executor/validator.py` (the Task 6 placeholder):

```python
# backend/ley_khaa/executor/validator.py
"""Did the script actually do the job? (spec §5.10, §6)

Pure: no I/O beyond reading what the run produced, no model call, no state
change. Every rule is recorded in `checks` for the manifest; only the first
failure in report order becomes the `reason` a human sees, and that reason is
always plain English — the traceback lives in the bundle, not in the question.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from ..interpreter.spec import TaskSpec
from .formats import expected_suffixes
from .sandbox import SandboxResult
from .workspace import Workspace

# Suffixes we know how to count rows in. Anything else is not row-checked.
_TABULAR = {".csv", ".xlsx"}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    checks: dict[str, bool]


def _format_matches(spec: TaskSpec, primary: Path | None) -> bool:
    if primary is None:
        return False
    suffixes = expected_suffixes(spec.output_format)
    # No opinion is a pass: rejecting a good deliverable because the request
    # described its format in words we did not anticipate is worse than not
    # checking at all.
    return not suffixes or primary.suffix.lower() in suffixes


def _has_rows(primary: Path | None) -> bool:
    if primary is None:
        return False
    suffix = primary.suffix.lower()
    if suffix not in _TABULAR:
        return True
    try:
        if suffix == ".csv":
            with primary.open(newline="", encoding="utf-8") as handle:
                return len(list(csv.reader(handle))) > 1
        from openpyxl import load_workbook

        book = load_workbook(primary)
        try:
            return book.active.max_row > 1
        finally:
            book.close()
    except Exception:
        # Unreadable is not "empty" but it is certainly not a good deliverable,
        # and the repair attempt is the right next move either way.
        return False


def validate(
    spec: TaskSpec,
    workspace: Workspace,
    result: SandboxResult,
    input_hashes: dict[str, str],
) -> Verdict:
    deliverables = workspace.deliverables()
    primary = deliverables[0] if deliverables else None

    checks = {
        "within_time_limit": not result.timed_out,
        "script_ran": result.exit_code == 0 and not result.timed_out,
        "deliverable_exists": primary is not None,
        "deliverable_not_empty": primary is not None and primary.stat().st_size > 0,
        "format_matches": _format_matches(spec, primary),
        "inputs_unmodified": workspace.input_hashes() == input_hashes,
        "has_rows": _has_rows(primary),
    }

    def fail(reason: str) -> Verdict:
        return Verdict(ok=False, reason=reason, checks=checks)

    if not checks["within_time_limit"]:
        return fail("The generated script ran too long and was stopped.")
    if not checks["script_ran"]:
        return fail("The generated script failed while running.")
    if not checks["deliverable_exists"]:
        return fail("The script finished but produced no output file.")
    if not checks["deliverable_not_empty"]:
        return fail("The script produced an output file, but it is empty.")
    if not checks["format_matches"]:
        return fail(
            f"I was asked for {spec.output_format} but the script produced {primary.name}."
        )
    if not checks["inputs_unmodified"]:
        return fail(
            "The script changed its own input files, so the result cannot be reproduced."
        )
    if not checks["has_rows"]:
        return fail("The output file has a header but no rows in it.")

    return Verdict(
        ok=True,
        reason=f"Produced {primary.name} in {result.duration_ms} ms.",
        checks=checks,
    )
```

- [ ] **Step 4: Run the tests**

Run: `cd backend && python -m pytest tests/test_validator.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add backend/ley_khaa/executor/validator.py backend/tests/test_validator.py
git commit -m "feat(executor): validate the deliverable a sandbox run produced"
```

---

### Task 9: ExecutionRunner — the lane and the repair loop

**Files:**
- Create: `backend/ley_khaa/executor/runner.py`
- Modify: `backend/ley_khaa/executor/workspace.py`
- Modify: `backend/tests/test_workspace.py`
- Test: `backend/tests/test_runner.py`

**Interfaces:**
- Consumes: `resolve_inputs` / `UnresolvedInputs` / `ResolvedInput` (Task 2), `Workspace` / `sha256_file` (Task 3), `pick_sandbox` / `SandboxRunner` (Tasks 4–5), `Synthesizer` / `SynthesizedScript` (Task 6), `validate` / `Verdict` (Task 8), `catalog.CATALOG_SEED` (Task 1).
- Produces: `ExecutionOutcome(verdict, workspace_path, attempts)`; `ExecutionRunner(llm=, messages=, sandbox=None, workspace_root=None)` with `.run(row, spec) -> ExecutionOutcome`; `Workspace.write_run_script(attempt) -> Path`. Task 10 consumes the runner; Task 13 re-runs what `write_run_script` points at.

**Why the sandbox is resolved on first use, not in `__init__`.** A `TaskDriver` — and therefore an
`ExecutionRunner` — is constructed per HTTP request by `build_orchestrator`. `pick_sandbox()` shells
out to probe the Docker daemon, and doing that on every request, including the ones that never
execute anything, would be a subprocess per page load.

**Why `UnresolvedInputs` is caught and returned rather than propagated.** `EXECUTING →
NEEDS_CLARIFICATION` is not a legal edge. Task 10 explains the rest; the short version is that a
failing `Verdict` reaches the human through `_validate`, which keeps `domain/states.py` untouched.

- [ ] **Step 1: Write the failing run.sh test**

Append to `backend/tests/test_workspace.py`:

```python
def test_the_bundle_carries_a_way_to_re_run_it(tmp_path):
    """A bundle a human cannot re-run is a claim, not an audit trail."""
    ws = Workspace.create(tmp_path, "task-1")
    ws.write_generator(2, "print('two')")
    path = ws.write_run_script(2)
    assert path.name == "run.sh"
    assert "generator/attempt_2.py" in path.read_text()
```

- [ ] **Step 2: Add `write_run_script`**

In `backend/ley_khaa/executor/workspace.py`, add the method after `write_generator`:

```python
    def write_run_script(self, attempt: int) -> Path:
        """The human-runnable re-entry point named in spec §5.11.

        Points at the attempt that actually succeeded, not at the last one
        written — a failed final attempt is kept for the audit trail but is not
        what re-running the bundle should execute.
        """
        path = self.generator_dir / "run.sh"
        path.write_text(
            "#!/bin/sh\n"
            "# Re-run the generator that produced this bundle's deliverable.\n"
            "# Run this from the bundle root — the directory holding inputs/.\n"
            f"exec python generator/attempt_{attempt}.py\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path
```

- [ ] **Step 3: Run the workspace tests**

Run: `cd backend && python -m pytest tests/test_workspace.py -v`
Expected: PASS (10 tests)

- [ ] **Step 4: Write the failing runner test**

```python
# backend/tests/test_runner.py
import json

import pytest

from ley_khaa.domain.models import Message
from ley_khaa.executor.runner import ExecutionRunner
from ley_khaa.executor.sandbox import SandboxResult, SandboxUnavailable
from ley_khaa.executor.synthesizer import SynthesizedScript
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


class FakeSandbox:
    """Runs nothing. Each queued step performs the effect a real run would have.

    Runner tests are about the lane and the repair loop, so paying for a real
    interpreter start-up per attempt would buy nothing and cost the suite its
    sub-second runtime.
    """

    name = "fake"

    def __init__(self, steps):
        self.steps = list(steps)
        self.scripts = []

    def run(self, *, script, workspace, timeout_s):
        self.scripts.append(script)
        return self.steps.pop(0)(workspace)


def _crash(_workspace) -> SandboxResult:
    return SandboxResult(
        exit_code=1, stdout="", stderr="KeyError: 'ticker'", duration_ms=4, timed_out=False
    )


def _writes_csv(workspace) -> SandboxResult:
    (workspace / "deliverable" / "output.csv").write_text("ticker\nSYN0000\n")
    return SandboxResult(exit_code=0, stdout="ok", stderr="", duration_ms=7, timed_out=False)


def _boom(_workspace) -> SandboxResult:
    raise SandboxUnavailable("the daemon went away")


def _spec(inputs=None, output_format="csv") -> TaskSpec:
    return TaskSpec(
        intent="compare the universes",
        inputs=inputs if inputs is not None else ["bloomberg universe", "factset"],
        operation="set_difference",
        output_format=output_format,
        certainty=0.9,
    )


def _script(source="print('ok')") -> SynthesizedScript:
    return SynthesizedScript(reasoning="because", source=source)


@pytest.fixture
def task(session):
    messages = MessageRepository(session)
    row = messages.add(
        Message(
            source="slack", client="demo", conversation_id="conv-1",
            author="boss", text="compare them",
        )
    )
    created = TaskRepository(session).create(
        project="demo", title="compare", source_message_ids=[row.id]
    )
    return created, messages


def _runner(tmp_path, task, *, responses, steps):
    row, messages = task
    return row, ExecutionRunner(
        llm=FakeLLM(responses),
        messages=messages,
        sandbox=FakeSandbox(steps),
        workspace_root=tmp_path,
    )


def test_a_clean_first_attempt_is_the_whole_story(tmp_path, task):
    row, runner = _runner(tmp_path, task, responses=[_script()], steps=[_writes_csv])
    outcome = runner.run(row, _spec())
    assert outcome.verdict.ok
    assert outcome.attempts == 1
    assert (tmp_path / f"task-{row.id}" / "deliverable" / "output.csv").is_file()
    assert (tmp_path / f"task-{row.id}" / "generator" / "run.sh").is_file()


def test_a_failure_is_repaired_once_and_both_attempts_are_kept(tmp_path, task):
    """A bundle that hides its first failure is not an audit trail."""
    row, runner = _runner(
        tmp_path, task, responses=[_script("broken"), _script("fixed")],
        steps=[_crash, _writes_csv],
    )
    outcome = runner.run(row, _spec())
    assert outcome.verdict.ok
    assert outcome.attempts == 2
    generator = tmp_path / f"task-{row.id}" / "generator"
    assert (generator / "attempt_1.py").read_text() == "broken"
    assert (generator / "attempt_2.py").read_text() == "fixed"
    # run.sh points at the attempt that worked, not at the last one written.
    assert "attempt_2.py" in (generator / "run.sh").read_text()


def test_the_repair_prompt_carries_the_traceback(tmp_path, task):
    row, runner = _runner(
        tmp_path, task, responses=[_script("broken"), _script("fixed")],
        steps=[_crash, _writes_csv],
    )
    runner.run(row, _spec())
    second = runner.synthesizer.llm.calls[1].user
    assert "broken" in second
    assert "KeyError: 'ticker'" in second


def test_two_failures_escalate_instead_of_looping(tmp_path, task):
    """Decision 5: repair once, then hand it to a human. Not three times, not
    until the token budget runs out."""
    row, runner = _runner(
        tmp_path, task, responses=[_script(), _script()], steps=[_crash, _crash]
    )
    outcome = runner.run(row, _spec())
    assert not outcome.verdict.ok
    assert outcome.attempts == 2
    assert "Traceback" not in outcome.verdict.reason


def test_unresolvable_inputs_cost_nothing(tmp_path, task):
    """§6: a name that resolves to nothing becomes a question BEFORE any model
    call. Spending Opus tokens on a task we already know we cannot start is the
    waste this ordering exists to prevent."""
    row, runner = _runner(tmp_path, task, responses=[], steps=[])
    outcome = runner.run(row, _spec(inputs=["trade blotter"]))
    assert not outcome.verdict.ok
    assert "trade blotter" in outcome.verdict.reason
    assert outcome.attempts == 0
    assert runner.synthesizer.llm.calls == []


def test_a_dead_sandbox_is_not_a_question_for_a_human(tmp_path, task):
    """Infrastructure failure propagates; Task 10 turns it into FAILED. Asking
    a human to answer for a dead daemon is not a question they can answer."""
    row, runner = _runner(tmp_path, task, responses=[_script()], steps=[_boom])
    with pytest.raises(SandboxUnavailable):
        runner.run(row, _spec())


def test_the_manifest_records_what_actually_happened(tmp_path, task):
    row, runner = _runner(
        tmp_path, task, responses=[_script("broken"), _script("fixed")],
        steps=[_crash, _writes_csv],
    )
    runner.run(row, _spec())
    manifest = json.loads((tmp_path / f"task-{row.id}" / "manifest.json").read_text())
    assert manifest["task_id"] == row.id
    assert manifest["lane"] == "synthesis"
    # Never "docker" when a fake ran: a bundle must not overstate its isolation.
    assert manifest["sandbox"] == "fake"
    assert [a["attempt"] for a in manifest["attempts"]] == [1, 2]
    assert manifest["attempts"][0]["ok"] is False
    assert manifest["attempts"][1]["ok"] is True
    assert len(manifest["deliverables"][0]["sha256"]) == 64
    assert {i["file"] for i in manifest["inputs"]} == {
        "bloomberg_universe.csv", "factset_universe.csv"
    }
    assert manifest["spec"]["operation"] == "set_difference"


def test_synthesis_blowing_up_is_a_failed_attempt_not_a_crash(tmp_path, task):
    row, runner = _runner(
        tmp_path, task, responses=[RuntimeError("connection reset"), _script()],
        steps=[_writes_csv],
    )
    outcome = runner.run(row, _spec())
    assert outcome.verdict.ok
    assert outcome.attempts == 2


def test_an_empty_script_is_not_run(tmp_path, task):
    row, runner = _runner(
        tmp_path, task, responses=[_script(""), _script()], steps=[_writes_csv]
    )
    outcome = runner.run(row, _spec())
    assert outcome.verdict.ok
    assert len(runner.sandbox.scripts) == 1
```

- [ ] **Step 5: Run it to verify it fails**

Run: `cd backend && python -m pytest tests/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.executor.runner'`

- [ ] **Step 6: Write the runner**

```python
# backend/ley_khaa/executor/runner.py
"""The whole execution lane, in one place (spec §3, decision 5).

    resolve inputs -> build workspace -> synthesize -> sandbox run -> validate
                                              ^______ repair once ______|

run() never raises for a business failure. It returns a Verdict, and the driver
turns that into a state. The one exception is SandboxUnavailable: a dead daemon
is ley-khaa failing, not the request failing, and the two must not be confused.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..interpreter.spec import TaskSpec
from ..llm.client import LLMClient
from ..llm.router import Stage, model_for
from ..persistence.message_repository import MessageRepository
from ..persistence.orm import TaskRow
from . import catalog
from .resolver import ResolvedInput, UnresolvedInputs, resolve_inputs
from .sandbox import SandboxRunner, SandboxResult, pick_sandbox
from .synthesizer import SynthesizedScript, Synthesizer
from .validator import Verdict, validate
from .workspace import Workspace, sha256_file

logger = logging.getLogger(__name__)

# One synthesis, one repair. Decision 5: then a human, who can see the whole
# bundle, decides — rather than a loop that keeps paying Opus to guess.
_MAX_ATTEMPTS = 2

# Kept out of the escalation text a human reads; the full stderr is in the bundle.
_STDERR_IN_MANIFEST = 2000

_SYNTHESIS_FAILED = "I could not produce a working script for this request."


@dataclass(frozen=True)
class ExecutionOutcome:
    verdict: Verdict
    workspace_path: str
    attempts: int


class ExecutionRunner:
    def __init__(
        self,
        *,
        llm: LLMClient,
        messages: MessageRepository,
        sandbox: SandboxRunner | None = None,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.synthesizer = Synthesizer(llm)
        self.messages = messages
        self._sandbox = sandbox
        self.workspace_root = Path(workspace_root or settings.workspace_root)

    @property
    def sandbox(self) -> SandboxRunner:
        """Resolved on first use, never in __init__.

        A TaskDriver is built per HTTP request, and pick_sandbox() shells out to
        probe the Docker daemon — doing that for every request, including the
        many that execute nothing, would be a subprocess per page load.
        """
        if self._sandbox is None:
            self._sandbox = pick_sandbox()
        return self._sandbox

    def run(self, row: TaskRow, spec: TaskSpec) -> ExecutionOutcome:
        workspace = Workspace.create(self.workspace_root, row.id)

        try:
            resolved = resolve_inputs(spec, row, self.messages)
        except UnresolvedInputs as exc:
            # Returned, not raised: EXECUTING -> NEEDS_CLARIFICATION is not a
            # legal edge, so the question reaches the human through _validate.
            verdict = Verdict(
                ok=False,
                reason=(
                    "I could not find the data for: "
                    + ", ".join(exc.names)
                    + ". Can you attach it, or tell me which dataset to use?"
                ),
                checks={"inputs_resolved": False},
            )
            self._write_manifest(workspace, row, spec, resolved=[], attempts=[], verdict=verdict)
            return ExecutionOutcome(verdict, str(workspace.root), 0)

        workspace.write_inputs(resolved)
        input_hashes = workspace.input_hashes()

        attempts: list[dict] = []
        previous: SynthesizedScript | None = None
        last: SandboxResult | None = None
        verdict = Verdict(ok=False, reason=_SYNTHESIS_FAILED, checks={})

        for number in range(1, _MAX_ATTEMPTS + 1):
            try:
                script = self._write_attempt(spec, resolved, previous, last, verdict)
            except _NoScript as exc:
                # previous/last stay as they were, so the next pass is a plain
                # retry when nothing has run yet and a repair when something has.
                attempts.append({"attempt": number, "error": str(exc)})
                verdict = Verdict(
                    ok=False, reason=_SYNTHESIS_FAILED, checks={"synthesis_produced_a_script": False}
                )
                continue

            path = workspace.write_generator(number, script.source)
            result = self.sandbox.run(
                script=path, workspace=workspace.root, timeout_s=settings.sandbox_timeout_seconds
            )
            verdict = validate(spec, workspace, result, input_hashes)
            attempts.append(
                {
                    "attempt": number,
                    "exit_code": result.exit_code,
                    "duration_ms": result.duration_ms,
                    "timed_out": result.timed_out,
                    "ok": verdict.ok,
                    "reason": verdict.reason,
                    "checks": verdict.checks,
                    "reasoning": script.reasoning,
                    "stderr_tail": result.stderr[-_STDERR_IN_MANIFEST:],
                }
            )
            previous, last = script, result
            if verdict.ok:
                workspace.write_run_script(number)
                break

        self._write_manifest(
            workspace, row, spec, resolved=resolved, attempts=attempts, verdict=verdict
        )
        return ExecutionOutcome(verdict, str(workspace.root), len(attempts))

    def _write_attempt(
        self,
        spec: TaskSpec,
        resolved: list[ResolvedInput],
        previous: SynthesizedScript | None,
        last: SandboxResult | None,
        verdict: Verdict,
    ) -> SynthesizedScript:
        try:
            script = (
                self.synthesizer.synthesize(spec, resolved)
                if previous is None or last is None
                else self.synthesizer.repair(
                    spec, resolved, previous=previous.source, result=last, verdict=verdict
                )
            )
        except Exception as exc:  # transport, refusal, malformed output
            logger.exception("synthesis call failed")
            raise _NoScript(f"{type(exc).__name__}: {exc}") from exc
        if not script.source.strip():
            raise _NoScript("the model returned an empty script")
        return script

    def _write_manifest(
        self,
        workspace: Workspace,
        row: TaskRow,
        spec: TaskSpec,
        *,
        resolved: list[ResolvedInput],
        attempts: list[dict],
        verdict: Verdict,
    ) -> None:
        workspace.write_manifest(
            {
                "task_id": row.id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "lane": "synthesis",
                # The sandbox that ACTUALLY ran, never the one we hoped for.
                "sandbox": self.sandbox.name,
                "models": {Stage.SYNTHESIS.value: model_for(Stage.SYNTHESIS).model},
                "catalog_seed": catalog.CATALOG_SEED,
                "spec": spec.model_dump(mode="json"),
                "inputs": [
                    {"name": i.name, "file": i.filename, "source": i.source, "sha256": i.sha256}
                    for i in resolved
                ],
                "attempts": attempts,
                "verdict": {
                    "ok": verdict.ok,
                    "reason": verdict.reason,
                    "checks": verdict.checks,
                },
                "deliverables": [
                    {"file": p.name, "sha256": sha256_file(p)} for p in workspace.deliverables()
                ],
                # An .xlsx is a zip that embeds timestamps, so re-running the
                # generator reproduces the VALUES, not the bytes. Saying so here
                # keeps the bundle from implying a claim it cannot support.
                "reproducibility": "cell values for .xlsx; bytes for csv, json and text",
            }
        )


class _NoScript(Exception):
    """This attempt produced nothing runnable. Internal to the loop above."""
```

- [ ] **Step 7: Run the tests**

Run: `cd backend && python -m pytest tests/test_runner.py -v`
Expected: PASS (9 tests)

- [ ] **Step 8: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: all pass

- [ ] **Step 9: Commit**

```bash
git add backend/ley_khaa/executor/runner.py backend/ley_khaa/executor/workspace.py \
        backend/tests/test_runner.py backend/tests/test_workspace.py
git commit -m "feat(executor): execution runner, repair loop, and bundle manifest"
```

---
### Task 10: Schema, and wiring the driver onto the runner

**Files:**
- Modify: `backend/ley_khaa/persistence/orm.py`
- Modify: `backend/ley_khaa/persistence/repository.py`
- Create: `backend/ley_khaa/alembic/versions/0003_executor.py`
- Modify: `backend/ley_khaa/orchestrator/driver.py`
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_driver.py`
- Modify: `backend/tests/test_driver_actions.py`
- Modify: `backend/tests/test_api.py`
- Test: `backend/tests/test_driver_execution.py`

**Interfaces:**
- Consumes: `ExecutionRunner`, `ExecutionOutcome` (Task 9); `SandboxUnavailable` (Task 4).
- Produces: `TaskRow.workspace_path: str | None`, `TaskRow.execution_verdict: dict | None`; `TaskRepository.save_execution(task_id, *, workspace_path, verdict) -> TaskRow`; `TaskDriver.executor`. Tasks 11 and 12 read the two columns.

**Why a failing execution routes through the verdict.** `EXECUTING → NEEDS_CLARIFICATION` is not a
legal edge, and `VALIDATING → NEEDS_CLARIFICATION` already is. So `_execute` persists what happened
and always moves to `VALIDATING`; `_validate` reads that record and decides. The cost is that
`_validate` becomes a thin step acting on a decision made moments earlier. What it buys is decision
6 — `domain/states.py` is not edited in this phase, and an execute/validate ping-pong is
structurally impossible rather than merely avoided. `SandboxUnavailable` is the one thing that does
*not* go through the verdict: `EXECUTING → FAILED` is legal, and a dead daemon is not a question a
human can answer (§6).

- [ ] **Step 1: Add the columns to the model**

In `backend/ley_khaa/persistence/orm.py`, add to `TaskRow` after `clarification_rounds`:

```python
    # The Output Bundle root (spec §5.11), surfaced by the dashboard.
    workspace_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # The serialized Verdict _execute produced and _validate acts on.
    execution_verdict: Mapped[dict | None] = mapped_column(JSON, nullable=True)
```

- [ ] **Step 2: Add the repository write**

In `backend/ley_khaa/persistence/repository.py`, add after `record_failure`:

```python
    def save_execution(self, task_id: str, *, workspace_path: str, verdict: dict) -> TaskRow:
        """Persist where the bundle is and what the run came to.

        One write for both, because a workspace_path without its verdict is a
        bundle nobody can interpret and a verdict without its path is a claim
        with no evidence behind it.
        """
        row = self._row(task_id)
        row.workspace_path = workspace_path
        row.execution_verdict = verdict
        self.session.commit()
        self.session.refresh(row)
        return row
```

- [ ] **Step 3: Write the migration**

```python
# backend/ley_khaa/alembic/versions/0003_executor.py
"""phase 3: output bundle path and execution verdict

Revision ID: 0003_executor
Revises: 0002_autonomy
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_executor"
down_revision = "0002_autonomy"
branch_labels = None
depends_on = None

_TASK_COLUMNS = [
    sa.Column("workspace_path", sa.String(), nullable=True),
    sa.Column("execution_verdict", sa.JSON(), nullable=True),
]


def upgrade() -> None:
    for column in _TASK_COLUMNS:
        op.add_column("tasks", column)


def downgrade() -> None:
    for column in reversed(_TASK_COLUMNS):
        op.drop_column("tasks", column.name)
```

- [ ] **Step 4: Run the drift guard**

Run: `cd backend && python -m pytest tests/test_migrations.py -v`
Expected: PASS — `test_migrations_match_the_models` is what proves the migration and the model
agree. If it fails, the column definitions differ; fix the migration, not the test.

- [ ] **Step 5: Keep the bundles out of the repo during tests**

In `backend/tests/conftest.py`, add to the imports and the env pins at the top (before any
`ley_khaa` import):

```python
import tempfile

os.environ["LEY_KHAA_DISABLE_STARTUP"] = "1"
os.environ["LEY_KHAA_LLM"] = "heuristic"
os.environ["LEY_KHAA_DEBOUNCE_SECONDS"] = "0"
os.environ["LEY_KHAA_SANDBOX"] = "subprocess"
# Otherwise every test that executes a task writes a bundle into ./task-workspaces
# in whatever directory pytest was invoked from.
os.environ.setdefault("LEY_KHAA_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="ley-khaa-tests-"))
```

- [ ] **Step 6: Add the fixture that keeps the HITL suite about transitions**

Also in `backend/tests/conftest.py`, after the `session` fixture:

```python
@pytest.fixture
def stub_execution(monkeypatch, tmp_path):
    """Make _execute a no-op that succeeds.

    The approve / reject / override / edit_spec tests are about transitions.
    Without this, each of them would synthesize and run a real script, which
    both slows the suite and couples the human-in-the-loop tests to executor
    behaviour that tests/test_driver_execution.py and
    tests/test_executor_end_to_end.py already cover directly.
    """
    from ley_khaa.executor.runner import ExecutionOutcome
    from ley_khaa.executor.validator import Verdict

    def _run(self, row, spec):
        return ExecutionOutcome(
            verdict=Verdict(ok=True, reason="stubbed", checks={}),
            workspace_path=str(tmp_path / f"task-{row.id}"),
            attempts=1,
        )

    monkeypatch.setattr("ley_khaa.executor.runner.ExecutionRunner.run", _run)
```

- [ ] **Step 7: Write the failing driver-execution test**

```python
# backend/tests/test_driver_execution.py
"""What _execute and _validate now do, and what they refuse to do."""
import pytest

from ley_khaa.domain.models import Message
from ley_khaa.domain.states import TaskState
from ley_khaa.executor.runner import ExecutionOutcome
from ley_khaa.executor.sandbox import SandboxUnavailable
from ley_khaa.executor.validator import Verdict
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.llm.client import FakeLLM
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


class StubRunner:
    """Stands in for ExecutionRunner so the driver's own branches are the
    subject. The runner has its own suite."""

    def __init__(self, outcome=None, raises=None):
        self.outcome = outcome
        self.raises = raises
        self.calls = 0

    def run(self, row, spec):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.outcome


def _spec() -> TaskSpec:
    return TaskSpec(
        intent="compare", inputs=["bloomberg universe", "factset"],
        operation="set_difference", output_format="xlsx", certainty=0.95,
    )


@pytest.fixture
def executing(session):
    """A task claimed into EXECUTING, which is where _execute picks it up."""
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(
        Message(source="s", client="c", conversation_id="conv-1", author="boss", text="compare")
    )
    task = repo.create(project="default", title="t", source_message_ids=[row.id])
    repo.save_spec(task.id, _spec())
    repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(task.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    repo.claim(task.id, expected=TaskState.INTERPRETED, target=TaskState.EXECUTING)
    driver = TaskDriver(
        repo, llm=FakeLLM([]), messages=messages, candidates=CandidateRepository(session)
    )
    return repo, driver, task


def test_a_good_run_lands_in_done_with_its_bundle_recorded(executing):
    repo, driver, task = executing
    driver.executor = StubRunner(
        ExecutionOutcome(Verdict(True, "Produced output.xlsx in 12 ms.", {"has_rows": True}),
                         "/bundles/task-1", 1)
    )
    result = driver.advance(task.id)
    assert result.state == TaskState.DONE.value
    assert result.workspace_path == "/bundles/task-1"
    assert result.execution_verdict["ok"] is True
    assert result.execution_verdict["attempts"] == 1


def test_a_failed_run_becomes_a_question_not_a_dead_end(executing):
    """VALIDATING -> NEEDS_CLARIFICATION is a legal edge and EXECUTING ->
    NEEDS_CLARIFICATION is not, which is the whole reason the verdict is
    persisted rather than acted on in place."""
    repo, driver, task = executing
    driver.executor = StubRunner(
        ExecutionOutcome(
            Verdict(False, "The script finished but produced no output file.", {}),
            "/bundles/task-1", 2,
        )
    )
    result = driver.advance(task.id)
    assert result.state == TaskState.NEEDS_CLARIFICATION.value
    assert result.open_question == "The script finished but produced no output file."
    # The bundle is still recorded: a failure a human cannot inspect is not one
    # they can act on.
    assert result.workspace_path == "/bundles/task-1"


def test_a_dead_sandbox_fails_the_task_rather_than_asking_a_human(executing):
    """§6: infrastructure failure is ley-khaa's problem. Asking someone to
    answer for a daemon that died is not a question."""
    repo, driver, task = executing
    driver.executor = StubRunner(raises=SandboxUnavailable("the daemon went away"))
    result = driver.advance(task.id)
    assert result.state == TaskState.FAILED.value
    assert "sandbox" in result.failure_reason
    assert result.open_question is None


def test_execution_runs_exactly_once_per_pass(executing):
    """advance() is re-entrant and the sweeper runs concurrently with HTTP
    handlers. Executing twice would mean paying Opus twice and racing two
    sandboxes over one workspace."""
    repo, driver, task = executing
    runner = StubRunner(ExecutionOutcome(Verdict(True, "ok", {}), "/bundles/task-1", 1))
    driver.executor = runner
    driver.advance(task.id)
    driver.advance(task.id)
    assert runner.calls == 1


def test_a_task_with_no_spec_never_reaches_execution(session):
    """_execute validates row.spec, so a task that somehow arrived without one
    must not blow up inside the executor."""
    repo = TaskRepository(session)
    messages = MessageRepository(session)
    row = messages.add(
        Message(source="s", client="c", conversation_id="conv-1", author="boss", text="compare")
    )
    task = repo.create(project="default", title="t", source_message_ids=[row.id])
    repo.claim(task.id, expected=TaskState.RECEIVED, target=TaskState.CLASSIFIED)
    repo.claim(task.id, expected=TaskState.CLASSIFIED, target=TaskState.INTERPRETED)
    repo.claim(task.id, expected=TaskState.INTERPRETED, target=TaskState.EXECUTING)
    driver = TaskDriver(
        repo, llm=FakeLLM([]), messages=messages, candidates=CandidateRepository(session)
    )
    driver.executor = StubRunner(ExecutionOutcome(Verdict(True, "ok", {}), "/b", 1))
    result = driver.advance(task.id)
    assert result.state == TaskState.FAILED.value
    assert "specification" in result.failure_reason
```

- [ ] **Step 8: Run it to verify it fails**

Run: `cd backend && python -m pytest tests/test_driver_execution.py -v`
Expected: FAIL — `AttributeError: 'TaskRow' object has no attribute 'workspace_path'` is fixed by
step 1, so the real failure is `assert 'done' == ...` on the stub never being called: `_execute` is
still the Phase 2 stub.

- [ ] **Step 9: Rewire the driver**

In `backend/ley_khaa/orchestrator/driver.py`, add the imports:

```python
from ..executor.runner import ExecutionRunner
from ..executor.sandbox import SandboxUnavailable
```

Add the runner to `__init__`, after `self.interpreter`:

```python
        # Constructing this is cheap: the sandbox itself is resolved on first
        # use, so a driver built for a request that executes nothing never
        # probes the Docker daemon.
        self.executor = ExecutionRunner(llm=llm, messages=messages)
```

Replace `_execute` and `_validate`:

```python
    def _execute(self, row: TaskRow) -> bool:
        try:
            spec = TaskSpec.model_validate(row.spec or {})
        except ValidationError:
            # Nothing to execute, and no question worth asking: a task reaching
            # EXECUTING without a valid spec is our bug, not the human's.
            logger.exception("task %s reached execution with no usable spec", row.id)
            if self.repo.claim(row.id, expected=TaskState.EXECUTING, target=TaskState.FAILED):
                self.repo.record_failure(row.id, "no valid specification to execute")
            return False

        try:
            outcome = self.executor.run(row, spec)
        except SandboxUnavailable as exc:
            # Infrastructure, not the request. A dead daemon is not a question a
            # human can answer (spec §6), so this fails rather than escalating.
            logger.exception("sandbox unavailable while executing task %s", row.id)
            if self.repo.claim(row.id, expected=TaskState.EXECUTING, target=TaskState.FAILED):
                self.repo.record_failure(row.id, f"the sandbox was unavailable: {exc}")
            return False

        self.repo.save_execution(
            row.id,
            workspace_path=outcome.workspace_path,
            verdict={
                "ok": outcome.verdict.ok,
                "reason": outcome.verdict.reason,
                "checks": outcome.verdict.checks,
                "attempts": outcome.attempts,
            },
        )
        return self.repo.claim(row.id, expected=TaskState.EXECUTING, target=TaskState.VALIDATING)

    def _validate(self, row: TaskRow) -> bool:
        """Act on the verdict _execute just recorded.

        Deliberately thin. The alternative — deciding inside _execute — needs an
        EXECUTING -> NEEDS_CLARIFICATION edge, and adding one is what makes an
        execute/validate loop possible in the first place (decision 6).
        """
        verdict = row.execution_verdict or {}
        if verdict.get("ok"):
            self.repo.set_open_question(row.id, None)
            return self.repo.claim(row.id, expected=TaskState.VALIDATING, target=TaskState.DONE)

        self.repo.set_open_question(
            row.id, verdict.get("reason") or "The run did not produce a usable result."
        )
        return self.repo.claim(
            row.id, expected=TaskState.VALIDATING, target=TaskState.NEEDS_CLARIFICATION
        )
```

And add the `ValidationError` import at the top:

```python
from pydantic import ValidationError
```

- [ ] **Step 10: Run the new tests**

Run: `cd backend && python -m pytest tests/test_driver_execution.py -v`
Expected: PASS (5 tests)

- [ ] **Step 11: Point the transition tests at the stub**

Run the full suite first to see the damage:

Run: `cd backend && python -m pytest -q`
Expected: FAIL — `AssertionError: FakeLLM exhausted: more parse() calls than queued responses` in
the ten tests that now really execute.

Add the `stub_execution` fixture argument to each of them. In
`backend/tests/test_driver.py`:

```python
def test_a_low_risk_confident_task_runs_straight_through(session, stub_execution):
def test_a_human_pinned_mode_beats_the_recommendation(session, stub_execution):
def test_advance_on_a_finished_task_is_a_no_op(session, stub_execution):
```

In `backend/tests/test_driver_actions.py`:

```python
def test_approve_releases_a_parked_task(session, stub_execution):
def test_rejecting_a_finished_task_does_not_corrupt_its_record(session, stub_execution):
def test_editing_a_finished_task_is_a_conflict(session, stub_execution):
def test_overriding_the_mode_of_a_finished_task_is_a_conflict(session, stub_execution):
def test_overriding_to_auto_releases_the_task_on_the_spot(session, stub_execution):
```

In `backend/tests/test_api.py`:

```python
def test_approve_runs_the_task(client, stub_execution):
def test_overriding_the_mode_to_auto_releases_the_task(client, stub_execution):
```

Leave `backend/tests/test_end_to_end.py` alone — those two tests *should* execute for real. They
are the demo, and Task 13 adds assertions to them.

- [ ] **Step 12: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: all pass

- [ ] **Step 13: Commit**

```bash
git add backend/ley_khaa/persistence/orm.py backend/ley_khaa/persistence/repository.py \
        backend/ley_khaa/alembic/versions/0003_executor.py backend/ley_khaa/orchestrator/driver.py \
        backend/tests/conftest.py backend/tests/test_driver_execution.py \
        backend/tests/test_driver.py backend/tests/test_driver_actions.py backend/tests/test_api.py
git commit -m "feat(executor): wire the driver onto the runner and persist the verdict"
```

---

### Task 11: The bundle API

**Files:**
- Modify: `backend/ley_khaa/api/app.py`
- Modify: `backend/ley_khaa/api/schemas.py`
- Test: `backend/tests/test_bundle_api.py`

**Interfaces:**
- Consumes: `Workspace` (Task 3), `TaskRow.workspace_path` (Task 10).
- Produces: `BundleOut(task_id, root, manifest, files, deliverables)`; `TaskOut.workspace_path`, `TaskOut.execution_verdict`; `GET /tasks/{id}/bundle`, `.../bundle/file?path=`, `.../bundle/deliverable`, `.../bundle/download`. Task 12 consumes all four.

**A fourth endpoint §5.1 does not list.** §5.1 names three. §5.2 also requires the dashboard to
download the deliverable itself, and `bundle/file` returns JSON-wrapped text for the code viewer —
useless for an `.xlsx`. `bundle/deliverable` streams the actual file. The alternative, making
`bundle/file` content-negotiate, would give the code viewer two response shapes to handle for no
gain.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_bundle_api.py
import io
import json
import zipfile

import pytest

from ley_khaa.executor.workspace import Workspace
from ley_khaa.persistence.repository import TaskRepository


@pytest.fixture
def bundled(session, tmp_path):
    """A task with a bundle on disk, as Task 10 would have left it."""
    repo = TaskRepository(session)
    task = repo.create(project="demo", title="compare", source_message_ids=[])
    workspace = Workspace.create(tmp_path, task.id)
    (workspace.inputs_dir / "bloomberg_universe.csv").write_text("ticker\nSYN0000\n")
    workspace.write_generator(1, "print('hello')")
    workspace.write_run_script(1)
    (workspace.deliverable_dir / "output.xlsx").write_bytes(b"PK\x03\x04\xff\xfe\x00 not really a zip")
    workspace.write_manifest({"task_id": task.id, "lane": "synthesis", "sandbox": "subprocess"})
    repo.save_execution(
        task.id, workspace_path=str(workspace.root), verdict={"ok": True, "reason": "done"}
    )
    return task, workspace


def test_the_bundle_endpoint_returns_the_manifest_and_a_file_listing(client, bundled):
    task, _ = bundled
    body = client.get(f"/tasks/{task.id}/bundle").json()
    assert body["manifest"]["lane"] == "synthesis"
    assert set(body["files"]) >= {
        "manifest.json",
        "inputs/bloomberg_universe.csv",
        "generator/attempt_1.py",
        "generator/run.sh",
        "deliverable/output.xlsx",
    }
    assert body["deliverables"] == ["deliverable/output.xlsx"]


def test_a_task_with_no_bundle_is_a_404(client, session):
    task = TaskRepository(session).create(project="demo", title="t", source_message_ids=[])
    assert client.get(f"/tasks/{task.id}/bundle").status_code == 404


def test_the_generator_source_can_be_read(client, bundled):
    task, _ = bundled
    body = client.get(
        f"/tasks/{task.id}/bundle/file", params={"path": "generator/attempt_1.py"}
    ).json()
    assert body["content"] == "print('hello')"


@pytest.mark.parametrize(
    "path", ["../../../etc/passwd", "/etc/passwd", "generator/../../../../etc/passwd"]
)
def test_the_file_endpoint_refuses_to_escape_the_bundle(client, bundled, path):
    """A viewer that can be talked into reading /etc/passwd is not a viewer."""
    task, _ = bundled
    assert client.get(f"/tasks/{task.id}/bundle/file", params={"path": path}).status_code == 400


def test_a_missing_file_inside_the_bundle_is_a_404(client, bundled):
    task, _ = bundled
    response = client.get(f"/tasks/{task.id}/bundle/file", params={"path": "generator/nope.py"})
    assert response.status_code == 404


def test_a_binary_file_is_not_returned_as_text(client, bundled):
    """The .xlsx has a download endpoint; the text viewer must not mangle it."""
    task, _ = bundled
    response = client.get(
        f"/tasks/{task.id}/bundle/file", params={"path": "deliverable/output.xlsx"}
    )
    assert response.status_code == 415


def test_the_deliverable_downloads(client, bundled):
    task, _ = bundled
    response = client.get(f"/tasks/{task.id}/bundle/deliverable")
    assert response.status_code == 200
    assert response.content.startswith(b"PK")
    assert "output.xlsx" in response.headers["content-disposition"]


def test_the_whole_bundle_downloads_as_a_zip(client, bundled):
    task, _ = bundled
    response = client.get(f"/tasks/{task.id}/bundle/download")
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert "generator/attempt_1.py" in archive.namelist()
    assert json.loads(archive.read("manifest.json"))["lane"] == "synthesis"


def test_the_task_payload_carries_the_bundle_path_and_verdict(client, bundled):
    task, workspace = bundled
    body = client.get(f"/tasks/{task.id}").json()
    assert body["workspace_path"] == str(workspace.root)
    assert body["execution_verdict"]["ok"] is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && python -m pytest tests/test_bundle_api.py -v`
Expected: FAIL — 404 on every bundle route; the endpoints do not exist yet.

- [ ] **Step 3: Extend the schemas**

In `backend/ley_khaa/api/schemas.py`, add the two fields to `TaskOut`, after `failure_reason`:

```python
    # The Output Bundle root on disk (spec §5.11), and what the run came to.
    workspace_path: str | None = None
    execution_verdict: dict[str, Any] | None = None
```

And add the new model at the end of the file:

```python
class BundleOut(BaseModel):
    task_id: str
    root: str
    manifest: dict[str, Any]
    # Every file in the bundle, as paths relative to the root, so the dashboard
    # can hand them straight back to the file endpoint.
    files: list[str]
    deliverables: list[str]
```

- [ ] **Step 4: Add the endpoints**

In `backend/ley_khaa/api/app.py`, extend the imports:

```python
import io
import zipfile
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..executor.workspace import Workspace
from .schemas import (
    AnswerIn,
    BundleOut,
    CandidateOut,
    IntakeOut,
    MessageIn,
    MessageOut,
    ModeIn,
    RejectIn,
    SpecPatchIn,
    TaskOut,
)
```

Then append the routes at the end of the file:

```python
# A generator script is a few KB. Anything this size is not source code, and
# streaming it into a JSON string would be a denial of service on the browser.
_MAX_INLINE_BYTES = 1_000_000


def _bundle_root(session: Session, task_id: str) -> Path:
    row = _require_task(session, task_id)
    if not row.workspace_path:
        raise HTTPException(status_code=404, detail="this task has no bundle yet")
    root = Path(row.workspace_path)
    if not root.is_dir():
        # The row points at a bundle that is no longer on disk — a wiped volume,
        # or a database restored beside a different workspace root.
        raise HTTPException(status_code=404, detail="the bundle is no longer on disk")
    return root


@app.get("/tasks/{task_id}/bundle", response_model=BundleOut)
def get_bundle(task_id: str, session: Session = Depends(get_session)) -> BundleOut:
    root = _bundle_root(session, task_id)
    files = sorted(
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
    )
    return BundleOut(
        task_id=task_id,
        root=str(root),
        manifest=Workspace(root).read_manifest(),
        files=files,
        deliverables=[name for name in files if name.startswith("deliverable/")],
    )


@app.get("/tasks/{task_id}/bundle/file")
def get_bundle_file(
    task_id: str, path: str, session: Session = Depends(get_session)
) -> dict[str, str]:
    root = _bundle_root(session, task_id)
    target = (root / path).resolve()
    # resolve() first, THEN compare: without it "generator/../../.." is a
    # perfectly ordinary-looking relative path that lands outside the bundle.
    # An absolute `path` also lands here, because root / "/etc/passwd" is
    # "/etc/passwd".
    if not target.is_relative_to(root.resolve()):
        raise HTTPException(status_code=400, detail="path escapes the bundle")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="no such file in the bundle")
    if target.stat().st_size > _MAX_INLINE_BYTES:
        raise HTTPException(status_code=413, detail="file too large to view; download it instead")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=415, detail="not a text file; use the download endpoints"
        )
    return {"path": path, "content": content}


@app.get("/tasks/{task_id}/bundle/deliverable")
def download_deliverable(task_id: str, session: Session = Depends(get_session)) -> FileResponse:
    """The deliverable itself (spec §5.2). Separate from bundle/file because an
    .xlsx is not text and the code viewer's JSON envelope cannot carry it."""
    root = _bundle_root(session, task_id)
    produced = Workspace(root).deliverables()
    if not produced:
        raise HTTPException(status_code=404, detail="this bundle has no deliverable")
    primary = produced[0]
    return FileResponse(primary, filename=primary.name)


@app.get("/tasks/{task_id}/bundle/download")
def download_bundle(task_id: str, session: Session = Depends(get_session)) -> StreamingResponse:
    root = _bundle_root(session, task_id)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(root))
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="task-{task_id}-bundle.zip"'},
    )
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && python -m pytest tests/test_bundle_api.py -v`
Expected: PASS (11 tests, counting the three parametrized traversal cases)

- [ ] **Step 6: Run the full suite**

Run: `cd backend && python -m pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add backend/ley_khaa/api/app.py backend/ley_khaa/api/schemas.py backend/tests/test_bundle_api.py
git commit -m "feat(api): serve the output bundle, its files, and its downloads"
```

---

### Task 12: The bundle panel

**Files:**
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/BundlePanel.tsx`
- Create: `frontend/src/BundlePanel.test.tsx`
- Modify: `frontend/src/TaskDetail.tsx`
- Modify: `frontend/src/TaskDetail.test.tsx`

**Interfaces:**
- Consumes: `GET /tasks/{id}/bundle`, `.../bundle/file?path=`, `.../bundle/deliverable`, `.../bundle/download` (Task 11).
- Produces: `Bundle` type, `fetchBundle`, `fetchBundleFile`, `bundleDownloadUrl`, `deliverableUrl`; `<BundlePanel taskId={...} />`. Nothing later consumes these.

- [ ] **Step 1: Extend the API client**

In `frontend/src/api.ts`, add the two new `Task` fields (after `failure_reason`):

```ts
  workspace_path: string | null;
  execution_verdict: Record<string, unknown> | null;
```

And add, after `patchTaskSpec`:

```ts
export type Bundle = {
  task_id: string;
  root: string;
  manifest: Record<string, any>;
  files: string[];
  deliverables: string[];
};

export async function fetchBundle(id: string): Promise<Bundle> {
  const res = await fetch(`${BASE}/tasks/${id}/bundle`);
  if (!res.ok) throw new Error(`fetchBundle failed: ${res.status}`);
  return res.json();
}

export async function fetchBundleFile(id: string, path: string): Promise<string> {
  const res = await fetch(`${BASE}/tasks/${id}/bundle/file?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(`fetchBundleFile failed: ${res.status}`);
  return (await res.json()).content;
}

// Plain hrefs, not fetches: the browser's own download handling is what should
// deal with a binary body, and an anchor keeps that out of our hands.
export const deliverableUrl = (id: string) => `${BASE}/tasks/${id}/bundle/deliverable`;
export const bundleDownloadUrl = (id: string) => `${BASE}/tasks/${id}/bundle/download`;
```

- [ ] **Step 2: Write the failing panel test**

```tsx
// frontend/src/BundlePanel.test.tsx
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import BundlePanel from "./BundlePanel";

const bundle = {
  task_id: "t1",
  root: "/work/task-workspaces/task-t1",
  manifest: {
    lane: "synthesis",
    sandbox: "subprocess",
    models: { synthesis: "claude-opus-5" },
    attempts: [
      { attempt: 1, ok: false, reason: "The generated script failed while running." },
      { attempt: 2, ok: true, reason: "Produced output.xlsx in 812 ms.", reasoning: "keyed on ticker" },
    ],
    verdict: { ok: true, reason: "Produced output.xlsx in 812 ms." },
  },
  files: [
    "manifest.json",
    "inputs/bloomberg_universe.csv",
    "generator/attempt_1.py",
    "generator/attempt_2.py",
    "deliverable/output.xlsx",
  ],
  deliverables: ["deliverable/output.xlsx"],
};

const okJson = (body: unknown) => ({ ok: true, json: async () => body });

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url.includes("/bundle/file")
        ? okJson({ path: "generator/attempt_2.py", content: "print('the real script')" })
        : okJson(bundle),
    ),
  );
});
afterEach(cleanup);

test("summarises how the deliverable was produced", async () => {
  render(<BundlePanel taskId="t1" />);
  expect(await screen.findByText(/synthesis/)).toBeTruthy();
  expect(screen.getByText(/subprocess/)).toBeTruthy();
  expect(screen.getByText(/claude-opus-5/)).toBeTruthy();
  expect(screen.getByText(/2 attempts/)).toBeTruthy();
});

test("shows the code that actually ran, on demand", async () => {
  render(<BundlePanel taskId="t1" />);
  fireEvent.click(await screen.findByRole("button", { name: "generator/attempt_2.py" }));
  await waitFor(() => expect(screen.getByText(/the real script/)).toBeTruthy());
});

test("names the sandbox that really ran, not the one we wanted", async () => {
  render(<BundlePanel taskId="t1" />);
  // A panel that says "docker" over a subprocess run would make the bundle
  // overstate its own isolation — the one thing the manifest exists to prevent.
  expect(await screen.findByText(/subprocess/)).toBeTruthy();
  expect(screen.queryByText(/docker/)).toBeNull();
});

test("offers the deliverable and the whole bundle for download", async () => {
  render(<BundlePanel taskId="t1" />);
  const deliverable = (await screen.findByRole("link", { name: /deliverable/i })) as HTMLAnchorElement;
  expect(deliverable.href).toContain("/tasks/t1/bundle/deliverable");
  const whole = screen.getByRole("link", { name: /bundle/i }) as HTMLAnchorElement;
  expect(whole.href).toContain("/tasks/t1/bundle/download");
});

test("says nothing loudly when there is no bundle", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 404 })));
  render(<BundlePanel taskId="t1" />);
  expect(await screen.findByText(/no bundle/i)).toBeTruthy();
});
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd frontend && npm test`
Expected: FAIL — `Failed to resolve import "./BundlePanel"`

- [ ] **Step 4: Write the panel**

```tsx
// frontend/src/BundlePanel.tsx
import { useEffect, useState } from "react";
import { bundleDownloadUrl, deliverableUrl, fetchBundle, fetchBundleFile, type Bundle } from "./api";

export default function BundlePanel({ taskId }: { taskId: string }) {
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [missing, setMissing] = useState(false);
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [source, setSource] = useState<string>("");

  useEffect(() => {
    let live = true;
    fetchBundle(taskId)
      .then((b) => live && setBundle(b))
      .catch(() => live && setMissing(true));
    return () => {
      live = false;
    };
  }, [taskId]);

  if (missing) return <p className="text-sm text-gray-500">No bundle for this task yet.</p>;
  if (!bundle) return <p className="text-sm text-gray-500">Loading the bundle…</p>;

  const manifest = bundle.manifest ?? {};
  const attempts = (manifest.attempts as unknown[]) ?? [];
  const generators = bundle.files.filter((f) => f.startsWith("generator/") && f.endsWith(".py"));

  const open = (path: string) => {
    setOpenFile(path);
    fetchBundleFile(taskId, path)
      .then(setSource)
      .catch((e) => setSource(String(e)));
  };

  return (
    <section className="rounded border border-gray-200 p-3 space-y-3">
      <h3 className="font-semibold">Output bundle</h3>

      <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm">
        <dt className="text-gray-500">lane</dt>
        <dd>{String(manifest.lane ?? "—")}</dd>
        <dt className="text-gray-500">sandbox</dt>
        {/* Reported, never inferred: a bundle must not overstate its isolation. */}
        <dd>{String(manifest.sandbox ?? "—")}</dd>
        <dt className="text-gray-500">model</dt>
        <dd>{String((manifest.models as Record<string, string>)?.synthesis ?? "—")}</dd>
        <dt className="text-gray-500">attempts</dt>
        <dd>{attempts.length} attempts</dd>
      </dl>

      {generators.length > 0 && (
        <div className="space-y-1">
          <p className="text-sm text-gray-500">The code that produced this:</p>
          <div className="flex flex-wrap gap-2">
            {generators.map((path) => (
              <button
                key={path}
                aria-label={path}
                onClick={() => open(path)}
                className={`rounded border px-2 py-0.5 text-xs ${
                  openFile === path
                    ? "border-blue-500 bg-blue-50 text-blue-800"
                    : "border-gray-200 text-gray-600"
                }`}
              >
                {path}
              </button>
            ))}
          </div>
          {openFile && (
            <pre className="max-h-72 overflow-auto rounded bg-gray-50 p-2 text-xs">{source}</pre>
          )}
        </div>
      )}

      <div className="flex gap-3 text-sm">
        {bundle.deliverables.length > 0 && (
          <a className="text-blue-700 underline" href={deliverableUrl(taskId)}>
            Download the deliverable
          </a>
        )}
        <a className="text-blue-700 underline" href={bundleDownloadUrl(taskId)}>
          Download the whole bundle
        </a>
      </div>
    </section>
  );
}
```

- [ ] **Step 5: Mount it**

In `frontend/src/TaskDetail.tsx`, add the import:

```tsx
import BundlePanel from "./BundlePanel";
```

And render it just before the closing `</div>` of the outer container, after the `waiting` block:

```tsx
      {task.workspace_path && <BundlePanel taskId={task.id} />}
```

- [ ] **Step 6: Keep the existing detail test compiling**

In `frontend/src/TaskDetail.test.tsx`, add the two new fields to the `task()` factory, after
`failure_reason: null,`:

```tsx
  workspace_path: null,
  execution_verdict: null,
```

- [ ] **Step 7: Run the frontend suite**

Run: `cd frontend && npm test`
Expected: PASS

- [ ] **Step 8: Typecheck and build**

Run: `cd frontend && npm run build`
Expected: no TypeScript errors

- [ ] **Step 9: Commit**

```bash
git add frontend/src/api.ts frontend/src/BundlePanel.tsx frontend/src/BundlePanel.test.tsx \
        frontend/src/TaskDetail.tsx frontend/src/TaskDetail.test.tsx
git commit -m "feat(dashboard): show the output bundle, its generator, and its downloads"
```

---

### Task 13: End to end, reproducibility, and the release

**Files:**
- Test: `backend/tests/test_executor_end_to_end.py`
- Modify: `backend/tests/test_end_to_end.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `backend/pyproject.toml`

**Interfaces:**
- Consumes: everything above.
- Produces: the release.

**What the reproducibility test can and cannot claim.** An `.xlsx` is a zip that embeds
timestamps, so two runs of the same script produce different bytes. Comparing parsed cell values is
the real claim — the *result* reproduces — and CSV is compared byte for byte because there it can
be. The manifest says exactly this, so the bundle never implies a byte-identical guarantee.

- [ ] **Step 1: Write the failing end-to-end test**

```python
# backend/tests/test_executor_end_to_end.py
"""§9: a fresh clone with no ANTHROPIC_API_KEY produces a real spreadsheet.

Nothing here is mocked. The heuristic LLM synthesizes, the subprocess sandbox
runs it, the validator judges it, and the bundle lands on disk.
"""
import csv
import json
from pathlib import Path

from openpyxl import load_workbook

from ley_khaa.executor.sandbox import SubprocessSandbox


def _cells(path: Path) -> list[tuple]:
    book = load_workbook(path)
    try:
        return [tuple(row) for row in book.active.iter_rows(values_only=True)]
    finally:
        book.close()


def _run_the_golden_conversation(client) -> dict:
    client.post("/simulate/messy_universe_check")
    task = client.get("/tasks").json()[0]
    return client.post(f"/tasks/{task['id']}/mode", json={"mode": "auto"}).json()


def test_the_golden_conversation_produces_a_real_spreadsheet(client):
    task = _run_the_golden_conversation(client)
    assert task["state"] == "done"

    root = Path(task["workspace_path"])
    deliverable = root / "deliverable" / "output.xlsx"
    assert deliverable.is_file()
    # Task 1 guarantees bloomberg holds exactly 5 tickers factset lacks.
    assert len(_cells(deliverable)) == 6


def test_the_bundle_records_how_the_spreadsheet_was_made(client):
    task = _run_the_golden_conversation(client)
    root = Path(task["workspace_path"])
    manifest = json.loads((root / "manifest.json").read_text())

    assert manifest["lane"] == "synthesis"
    # conftest pins the subprocess sandbox; the manifest must say so rather than
    # claiming the isolation it did not have.
    assert manifest["sandbox"] == "subprocess"
    assert manifest["verdict"]["ok"] is True
    assert [i["file"] for i in manifest["inputs"]] == [
        "bloomberg_universe.csv",
        "factset_universe.csv",
    ]
    assert len(manifest["deliverables"][0]["sha256"]) == 64
    assert (root / "generator" / "attempt_1.py").is_file()
    assert (root / "generator" / "run.sh").is_file()


def test_the_bundle_re_runs_to_the_same_spreadsheet(client):
    """The claim the whole Output Bundle rests on."""
    task = _run_the_golden_conversation(client)
    root = Path(task["workspace_path"])
    deliverable = root / "deliverable" / "output.xlsx"

    original = _cells(deliverable)
    deliverable.unlink()

    script = root / "generator" / "attempt_1.py"
    result = SubprocessSandbox().run(script=script, workspace=root, timeout_s=60)
    assert result.ok, result.stderr
    # Values, not bytes: an .xlsx is a zip and embeds a timestamp.
    assert _cells(deliverable) == original


def test_a_csv_bundle_re_runs_byte_for_byte(client):
    """Where a byte-level claim IS available, make it."""
    client.post("/simulate/ambiguous_report_request")
    task = next(
        t for t in client.get("/tasks").json() if t["state"] == "needs_clarification"
    )
    client.post(f"/tasks/{task['id']}/answer", json={"text": "as a csv please"})
    done = client.post(f"/tasks/{task['id']}/approve").json()
    assert done["state"] == "done"

    root = Path(done["workspace_path"])
    deliverable = root / "deliverable" / "output.csv"
    original = deliverable.read_bytes()
    assert len(list(csv.reader(original.decode().splitlines()))) > 1

    deliverable.unlink()
    result = SubprocessSandbox().run(
        script=root / "generator" / "attempt_1.py", workspace=root, timeout_s=60
    )
    assert result.ok, result.stderr
    assert deliverable.read_bytes() == original


def test_the_dashboard_can_reach_the_bundle_over_the_api(client):
    task = _run_the_golden_conversation(client)
    bundle = client.get(f"/tasks/{task['id']}/bundle").json()
    assert bundle["deliverables"] == ["deliverable/output.xlsx"]

    source = client.get(
        f"/tasks/{task['id']}/bundle/file", params={"path": "generator/attempt_1.py"}
    ).json()["content"]
    assert "write_rows" in source

    assert client.get(f"/tasks/{task['id']}/bundle/deliverable").status_code == 200
```

- [ ] **Step 2: Run it**

Run: `cd backend && python -m pytest tests/test_executor_end_to_end.py -v`
Expected: PASS (5 tests). If `test_the_golden_conversation_produces_a_real_spreadsheet` lands in
`needs_clarification` instead, the input names did not resolve — re-read Task 7's opening note.

- [ ] **Step 3: Strengthen the existing end-to-end assertions**

In `backend/tests/test_end_to_end.py`, the two existing tests now run the real executor. Add one
assertion to each so they say so.

After `assert released["mode_override"] == "auto"` in the first test:

```python
    # Phase 3: "done" now means a file exists, not that a stub walked the states.
    assert Path(released["workspace_path"], "deliverable", "output.xlsx").is_file()
```

And after the last line of the second test, replace it with:

```python
    approved = client.post(f"/tasks/{task['id']}/approve").json()
    assert approved["state"] == TaskState.DONE.value
    assert Path(approved["workspace_path"], "deliverable", "output.csv").is_file()
```

Add the import at the top of the file:

```python
from pathlib import Path
```

- [ ] **Step 4: Run the whole backend suite**

Run: `cd backend && python -m pytest -q`
Expected: all pass, no warnings

- [ ] **Step 5: Prove it against the real sandbox**

```bash
docker build -t ley-khaa-sandbox backend/sandbox
cd backend && python -m pytest tests/test_sandbox_contract.py -v -m docker
```
Expected: the `docker` parameters run and pass.

- [ ] **Step 6: Prove the fresh-clone claim**

This is §9's definition of done and cannot be replaced by a test run:

```bash
cd /tmp && rm -rf ley-khaa-dod && git clone https://github.com/ruttantai/ley-khaa ley-khaa-dod
cd ley-khaa-dod && git checkout <this branch> && unset ANTHROPIC_API_KEY && docker compose up --build
```

Then, in another shell:

```bash
curl -s localhost:8000/tasks | python -m json.tool | grep -E 'state|workspace_path'
curl -s "localhost:8000/tasks/<id>/bundle" | python -m json.tool
curl -sI localhost:5173
```
Expected: the seeded task reaches `awaiting_approval`; flipping it to Auto
(`curl -X POST localhost:8000/tasks/<id>/mode -H 'content-type: application/json' -d '{"mode":"auto"}'`)
reaches `done` with a bundle whose manifest reports `"sandbox": "docker"`; the frontend answers 200.
Open the dashboard, expand the task, and confirm the bundle panel renders the generator source and
that the deliverable downloads.

- [ ] **Step 7: Update the README**

Replace the Status paragraph's stub sentence and bump the phase table row:

```markdown
**v0.4.0 — the synthesis-first executor.** An approved task now does real work: its `TaskSpec`
becomes resolved inputs (attachments first, a seeded synthetic catalog second), a Python script
synthesized for the request, a run inside a locked-down Docker sandbox with no network, and a
validated deliverable. Everything lands in a reproducible **Output Bundle** —
`task-workspaces/task-<id>/` holding the deliverable, every generator attempt including the failed
ones, the exact inputs, and a `manifest.json` recording which sandbox actually ran, which model
wrote the code, and the sha256 of every file. A crash or a failed validation is repaired once from
the traceback and then handed to a human. No Docker daemon? The executor falls back to a
capped, environment-scrubbed subprocess, says so loudly, and stamps it into the manifest.
```

| 3 | `v0.4.0` | Synthesis-first executor, validator, Output Bundle | ✅ shipped |

Add a section after `### Which model actually runs`:

```markdown
### Where synthesized code runs

Docker, by default: `--network none`, read-only rootfs, non-root, 512 MB, 1 CPU, 64 pids, killed on
a wall-clock timeout. The image (`backend/sandbox/Dockerfile`) carries the standard library,
`openpyxl` and `python-docx` — deliberately the same set the backend has, so the fallback cannot
run something the real sandbox couldn't.

With no daemon reachable, `SubprocessSandbox` takes over so the Docker-free dev loop keeps working.
It caps CPU and memory and scrubs the environment — your `ANTHROPIC_API_KEY` is not visible to
synthesized code — but it **cannot remove network access**. It warns once per process, and the
bundle's manifest records `"sandbox": "subprocess"`. Set `LEY_KHAA_SANDBOX=docker` to refuse the
fallback entirely.
```

- [ ] **Step 8: Update the CHANGELOG**

Under `## [Unreleased]`, add the release section:

```markdown
## [0.4.0] — 2026-08-25

### Added
- Synthesis-first executor (§5.10): `TaskSpec` → resolved inputs → a synthesized Python script →
  a sandboxed run → a validated deliverable, all behind one `ExecutionRunner.run()`.
- Input resolution: message attachments first, then a Faker-seeded catalog of synthetic securities
  datasets. A name that matches neither becomes a clarification **before** any model call.
- Sandboxes: `DockerSandbox` (no network, read-only rootfs, non-root, capped, killed on timeout) and
  `SubprocessSandbox` (capped and environment-scrubbed, but *not* network-isolated). One contract
  test both must pass. The manifest records which one ran.
- Reproducible Output Bundle (§5.11): `task-workspaces/task-<id>/` with the deliverable, every
  generator attempt, the frozen inputs, `run.sh`, and a `manifest.json` carrying the sandbox, the
  model, the catalog seed, per-attempt verdicts, and sha256 for every file.
- Validator: time limit, clean exit, deliverable present, non-empty, format matching the request,
  inputs unmodified, and at least one row. Failures escalate in plain English; the traceback stays
  in the bundle.
- Repair once, then escalate (§6): a crash or a failed validation is re-synthesized from the
  traceback exactly once. Both attempts are kept.
- `Stage.SYNTHESIS` routes to Opus at 16,000 max tokens.
- Bundle API: `GET /tasks/{id}/bundle`, `.../bundle/file?path=` (path-traversal guarded),
  `.../bundle/deliverable`, `.../bundle/download`.
- Dashboard bundle panel: how the deliverable was produced, the code that produced it, and
  downloads.
- Offline synthesis: `HeuristicLLM` returns real, runnable canned scripts, so a fresh clone with no
  `ANTHROPIC_API_KEY` still produces a genuine `.xlsx`. Canned, not generated — the README says so.

### Changed
- `TaskDriver._execute` and `_validate` are no longer stubs. `_execute` runs the lane and persists
  a verdict; `_validate` acts on it. The state machine is unchanged.
- The offline interpreter matches multi-word source names ("bloomberg universe") as one input
  rather than emitting an ambiguous bare "universe" that resolves to nothing.
- `tasks` gains `workspace_path` and `execution_verdict` (Alembic `0003_executor`).
```

- [ ] **Step 9: Bump the version**

In `backend/pyproject.toml`:

```toml
version = "0.4.0"
```

(`frontend/package.json` carries no version field; leave it alone.)

- [ ] **Step 10: Full verification**

```bash
cd backend && python -m pytest -q
cd ../frontend && npm test && npm run build
```
Expected: both green, no new warnings.

- [ ] **Step 11: Commit and tag**

```bash
git add README.md CHANGELOG.md backend/pyproject.toml backend/tests/test_end_to_end.py \
        backend/tests/test_executor_end_to_end.py
git commit -m "docs: release v0.4.0 — the synthesis-first executor"
```

Then open the PR, let CI go green, merge, and tag the merge commit:

```bash
git tag -a v0.4.0 -m "v0.4.0 — synthesis-first executor, sandbox, and the reproducible Output Bundle"
git push origin v0.4.0
```

---

## Self-review notes

Recorded rather than silently resolved, because each one is a place the plan
knowingly departs from the spec.

1. **`ResolvedInput.path` → `filename` + `content`.** §3.2 sketches a `path`. The path belongs to
   the workspace, not the resolver; Task 2 explains it.
2. **No `pandas`/`numpy` in the sandbox image.** §4.1 lists them. Task 5 explains why installing
   them would make the two sandboxes disagree.
3. **`inputs/` is not mounted read-only.** §4.1 promises it; Task 3 enforces immutability by
   re-hashing instead, and the validator's `inputs_unmodified` check is the enforcement.
4. **"Any columns named in the spec are present" is not implemented.** §6 lists it, but `TaskSpec`
   has no column field to check against. Faking it against `inputs` would reject good work.
5. **A fourth bundle endpoint.** §5.1 lists three; §5.2 needs the deliverable itself downloadable,
   and the text viewer cannot carry an `.xlsx`. Task 11 explains.
6. **The offline interpreter changed.** Not in the spec at all, but §9 is unreachable without it —
   the golden conversation's own input names did not resolve. Task 7 explains.

Spec sections and where they land: §5.10 → Tasks 1–9; §5.11 → Tasks 3, 9, 11, 12; §5.12 → Task 6's
system prompt plus the sandbox image (Task 5); §6's error table → Tasks 8, 9, 10, 11; §7's testing
requirements → Tasks 4, 5 (contract), 9 (`FakeSandbox`), 13 (reproducibility, offline E2E), 8
(validator table), 2 (resolver table), 12 (panel); §9's definition of done → Task 13.
