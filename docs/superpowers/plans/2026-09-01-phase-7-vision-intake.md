# Phase 7 — Vision Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a pasted image understood — extracted once via Claude vision, frozen as a reproducible checkpoint, and usable both as context for the interpreter and as data for a synthesized script.

**Architecture:** A `VisionExtractor` sits behind a cache keyed on the SHA-256 of the image bytes; that row *is* the frozen checkpoint. An `ImageFetcher` turns a channel URL into bytes under an explicit security boundary. `LLMClient` gains one method so every stand-in stays offline-deterministic. Two consumers read the result: the interpreter prints the summary, the resolver binds the content as an input.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy + Alembic, Pydantic v2, `anthropic` SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-phase-7-vision-intake-design.md` — read it first; it is the binding authority and this plan argues from it.

## Global Constraints

- **Python is the worktree-local venv**: `../.venv/bin/python` from `backend/`. The repo-root `.venv` is installed editable against the MAIN checkout — using it silently tests the wrong code.
- **Backend tests**: `cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q`. `mkdir -p "$HOME/tmp"` first — this Mac runs Docker via Colima, which mounts only `$HOME`; without `TMPDIR` the 9 `[docker]` params fail misleadingly.
- **Frontend**: `cd frontend && npm test && npm run typecheck`.
- **The bar is 0 failures, 0 skipped, 0 warnings.** A skip is a retired assertion, not a pass.
- **No test may reach the network.** `conftest` pins `LEY_KHAA_LLM=heuristic`, `LEY_KHAA_DEBOUNCE_SECONDS=0`, `LEY_KHAA_DISPATCH=inline`.
- **Haiku 4.5 must never receive `thinking`** — pre-4.6 models 400 on it. The existing `choice.supports_thinking` gate handles this; do not bypass it.
- **`LLMClient.name` records who ACTUALLY did the work**, never the model the router would have picked. An offline run must never credit `claude-opus-5`.
- **Every new non-null string column needs `server_default=text("''")`**, not `server_default=""` — Alembic's SQLite comparator cannot strip quotes from a zero-length default literal and the drift guard false-positives.
- **Default model is `claude-opus-5`.** `router.OPUS` is already that; do not introduce a new model string.

---

## File Structure

| File | Responsibility |
|---|---|
| `ley_khaa/vision/__init__.py` | new package |
| `ley_khaa/vision/contract.py` | `VisionExtraction` (the model's output shape) |
| `ley_khaa/vision/fetcher.py` | `ImageFetcher` — URL → bytes, and the security boundary |
| `ley_khaa/vision/extractor.py` | `VisionExtractor` — cache lookup, model call, degradation |
| `ley_khaa/persistence/image_extraction_repository.py` | the cache's storage |
| `ley_khaa/persistence/orm.py` | `ImageExtractionRow` |
| `ley_khaa/alembic/versions/0008_vision.py` | the migration |
| `ley_khaa/llm/client.py` | `extract_image` on the protocol, `AnthropicLLM`, `FakeLLM` |
| `ley_khaa/llm/heuristic.py` | `HeuristicLLM.extract_image` — the offline stand-in |
| `ley_khaa/interpreter/interpreter.py` | `_render` gains the summary |
| `ley_khaa/executor/resolver.py` | images become bindable; `ResolvedInput` gains provenance |
| `ley_khaa/executor/runner.py` | threads the extractor into `resolve_inputs` |
| `ley_khaa/orchestrator/driver.py` | constructs the extractor, hands it to both consumers |
| `ley_khaa/config.py` | three new settings |

**Why a new `vision/` package** rather than folding into `llm/`: the fetcher is HTTP-and-credentials, the extractor is cache-and-policy, and neither is an LLM client. `llm/` stays "how we talk to a model".

---

## Task 1: Schema — `image_extractions`

**Files:**
- Modify: `backend/ley_khaa/persistence/orm.py`
- Create: `backend/ley_khaa/alembic/versions/0008_vision.py`
- Test: `backend/tests/test_migrations.py` (existing drift guard — must stay green)

**Interfaces:**
- Produces: `ImageExtractionRow` with columns `image_sha256` (PK), `kind`, `content`, `summary`, `media_type`, `byte_size`, `model`, `created_at`.

- [ ] **Step 1: Add the ORM row**

In `backend/ley_khaa/persistence/orm.py`, after `DeadLetterRow`:

```python
class ImageExtractionRow(Base):
    """One image, read once (spec §3.2).

    Keyed on the SHA-256 of the image BYTES, not on a message id or an
    attachment index. The same screenshot pasted in two messages therefore
    costs one Opus call, the identity survives re-drives and repair loops, and
    it is the thing a manifest can attest.

    `content` is TEXT rather than JSON for the same reason DeadLetterRow.payload
    is: Postgres's `json` type has no equality operator, and this column is
    written once and only ever read.

    An empty `content` is the "was not read" record (spec §3.6) — no vision
    backend, or an extraction that failed. It is stored rather than skipped so
    a second drive does not retry a fetch that will fail again.
    """

    __tablename__ = "image_extractions"

    image_sha256: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, default="text", server_default=text("'text'"))
    content: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    summary: Mapped[str] = mapped_column(Text, default="", server_default=text("''"))
    media_type: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    byte_size: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Who ACTUALLY produced this — LLMClient.name, never the router's pick.
    model: Mapped[str] = mapped_column(String, default="", server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
```

Check the imports at the top of the file already include `String`, `Text`, `Integer`, `DateTime`, and `text` from `sqlalchemy`. Add whichever are missing.

- [ ] **Step 2: Run the drift guard and watch it FAIL**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_migrations.py -q
```

Expected: FAIL — the guard compares the ORM metadata against the migrated schema and `image_extractions` exists in one and not the other. **This failure is the point**: it proves the guard would catch a missing migration.

- [ ] **Step 3: Write the migration**

Create `backend/ley_khaa/alembic/versions/0008_vision.py`, following `0007_channels.py`'s shape exactly:

```python
"""phase 7: vision extraction checkpoints

Revision ID: 0008_vision
Revises: 0007_channels
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_vision"
down_revision = "0007_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_extractions",
        sa.Column("image_sha256", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False, server_default=sa.text("'text'")),
        sa.Column("content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("media_type", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("image_extractions")
```

- [ ] **Step 4: Run the drift guard and watch it PASS**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_migrations.py -q
```

Expected: PASS (3 passed). If it still fails on a server-default mismatch, the cause is a bare `server_default=""` somewhere — it must be `sa.text("''")`.

- [ ] **Step 5: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/persistence/orm.py backend/ley_khaa/alembic/versions/0008_vision.py
git commit -m "$(cat <<'EOF'
feat(schema): add the image extraction checkpoint table

Keyed on the sha256 of the image bytes, not a message id: the same screenshot
pasted twice then costs one model call, the identity survives re-drives and
repair loops, and it is the thing a manifest can attest. An empty content is
the "was not read" record, stored rather than skipped so a second drive does
not retry a fetch that will fail again.
EOF
)"
```

---

## Task 2: The extraction contract and its repository

**Files:**
- Create: `backend/ley_khaa/vision/__init__.py` (empty)
- Create: `backend/ley_khaa/vision/contract.py`
- Create: `backend/ley_khaa/persistence/image_extraction_repository.py`
- Test: `backend/tests/test_image_extraction_repository.py`

**Interfaces:**
- Consumes: `ImageExtractionRow` (Task 1).
- Produces:
  - `VisionExtraction(kind: Literal["table","text"], content: str, summary: str)`
  - `ImageExtractionRepository(session)` with `get(image_sha256) -> ImageExtractionRow | None` and
    `record(*, image_sha256, extraction, media_type, byte_size, model) -> ImageExtractionRow`.
  - `sha256_of(image: bytes) -> str`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_image_extraction_repository.py`:

```python
import pytest

from ley_khaa.persistence.image_extraction_repository import (
    ImageExtractionRepository,
    sha256_of,
)
from ley_khaa.vision.contract import VisionExtraction

IMAGE = b"\x89PNG\r\n\x1a\n-not-really-a-png"


def _extraction(**over) -> VisionExtraction:
    return VisionExtraction(
        kind=over.pop("kind", "table"),
        content=over.pop("content", "a,b\n1,2"),
        summary=over.pop("summary", "a two-column table"),
    )


def test_a_hash_is_stable_and_content_addressed():
    assert sha256_of(IMAGE) == sha256_of(bytes(IMAGE))
    assert sha256_of(IMAGE) != sha256_of(IMAGE + b"x")


def test_an_unknown_image_has_no_row(session):
    assert ImageExtractionRepository(session).get(sha256_of(IMAGE)) is None


def test_a_recorded_extraction_comes_back(session):
    repo = ImageExtractionRepository(session)
    repo.record(
        image_sha256=sha256_of(IMAGE),
        extraction=_extraction(),
        media_type="image/png",
        byte_size=len(IMAGE),
        model="heuristic",
    )

    row = repo.get(sha256_of(IMAGE))
    assert row is not None
    assert row.kind == "table"
    assert row.content == "a,b\n1,2"
    assert row.summary == "a two-column table"
    assert row.media_type == "image/png"
    assert row.byte_size == len(IMAGE)
    assert row.model == "heuristic"


def test_recording_the_same_image_twice_updates_rather_than_raising(session):
    """A re-extraction can happen legitimately — two workers racing on the same
    image. The second write must not blow up on the primary key."""
    repo = ImageExtractionRepository(session)
    repo.record(
        image_sha256=sha256_of(IMAGE), extraction=_extraction(),
        media_type="image/png", byte_size=1, model="heuristic",
    )
    repo.record(
        image_sha256=sha256_of(IMAGE),
        extraction=_extraction(content="x,y\n3,4", summary="different"),
        media_type="image/png", byte_size=1, model="anthropic",
    )

    row = repo.get(sha256_of(IMAGE))
    assert row.content == "x,y\n3,4"
    assert row.model == "anthropic"


def test_an_unread_record_is_storable(session):
    """The degradation record (spec §3.6): empty content is how "was not read"
    is expressed, and it must round-trip like any other."""
    repo = ImageExtractionRepository(session)
    repo.record(
        image_sha256=sha256_of(IMAGE),
        extraction=_extraction(kind="text", content="", summary="chart.png was not read"),
        media_type="image/png", byte_size=0, model="heuristic",
    )

    row = repo.get(sha256_of(IMAGE))
    assert row.content == ""
    assert "was not read" in row.summary


@pytest.mark.parametrize("kind", ["png", "csv", "", "TABLE"])
def test_a_kind_outside_the_closed_set_is_rejected(kind):
    """kind decides the checkpoint's file extension. A model returning
    something else must fail validation rather than produce a file whose
    extension lies about its contents."""
    with pytest.raises(Exception):
        VisionExtraction(kind=kind, content="", summary="")
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_image_extraction_repository.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.vision'`.

- [ ] **Step 3: Write the contract**

Create `backend/ley_khaa/vision/__init__.py` (empty file), then `backend/ley_khaa/vision/contract.py`:

```python
from typing import Literal

from pydantic import BaseModel


class VisionExtraction(BaseModel):
    """What a model returns for one image (spec §3.1).

    Two fields rather than one because the consumers have different budgets:
    the interpreter needs a sentence it can afford inside a prompt, the
    resolver needs the whole CSV as bytes to compute on. Truncating `content`
    to serve the interpreter would hand it half a row of CSV, which is worse
    than a sentence.

    `kind` is a closed Literal on purpose — it decides the checkpoint's file
    extension, so a model answering "png" must fail structured-output
    validation rather than produce an `.png` file full of prose.
    """

    kind: Literal["table", "text"]
    content: str
    summary: str
```

- [ ] **Step 4: Write the repository**

Create `backend/ley_khaa/persistence/image_extraction_repository.py`:

```python
import hashlib

from sqlalchemy.orm import Session

from ..vision.contract import VisionExtraction
from .orm import ImageExtractionRow


def sha256_of(image: bytes) -> str:
    """The identity of an image, and the cache key (spec §3.2)."""
    return hashlib.sha256(image).hexdigest()


class ImageExtractionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, image_sha256: str) -> ImageExtractionRow | None:
        return self.session.get(ImageExtractionRow, image_sha256)

    def record(
        self,
        *,
        image_sha256: str,
        extraction: VisionExtraction,
        media_type: str,
        byte_size: int,
        model: str,
    ) -> ImageExtractionRow:
        """Upsert, not insert.

        Two workers can legitimately reach the same unread image at once — one
        per project, by design since Phase 5 — and the loser of that race must
        not raise on the primary key. Last write wins: both wrote the same
        image, so neither result is more correct than the other.
        """
        row = self.session.get(ImageExtractionRow, image_sha256)
        if row is None:
            row = ImageExtractionRow(image_sha256=image_sha256)
            self.session.add(row)
        row.kind = extraction.kind
        row.content = extraction.content
        row.summary = extraction.summary
        row.media_type = media_type
        row.byte_size = byte_size
        row.model = model
        self.session.commit()
        self.session.refresh(row)
        return row
```

- [ ] **Step 5: Run the tests and watch them pass**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_image_extraction_repository.py -q
```

Expected: PASS (9 passed — the parametrized kind test contributes 4).

- [ ] **Step 6: Mutation-test the two guarantees**

1. Change `kind: Literal["table", "text"]` to `kind: str` → `test_a_kind_outside_the_closed_set_is_rejected` must fail on all four params. Restore.
2. In `record`, replace the get-or-create with a bare `self.session.add(ImageExtractionRow(...))` → `test_recording_the_same_image_twice_updates_rather_than_raising` must fail with an IntegrityError. Restore.

**Report the actual outcome of each.** If a mutation does not fail, say so rather than reporting a non-result — find an alternate mutation that discriminates, or state that the assertion does not guard what it claims.

- [ ] **Step 7: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/vision backend/ley_khaa/persistence/image_extraction_repository.py backend/tests/test_image_extraction_repository.py
git commit -m "$(cat <<'EOF'
feat(vision): add the extraction contract and its store

kind is a closed Literal because it decides the checkpoint's file extension —
a model answering "png" must fail validation rather than write an .png full of
prose. record() upserts rather than inserts: two project workers can reach the
same unread image at once, and the loser of that race must not raise.
EOF
)"
```

---

## Task 3: `extract_image` on the seam, and the offline stand-ins

**Files:**
- Modify: `backend/ley_khaa/llm/client.py` (protocol + `FakeLLM`)
- Modify: `backend/ley_khaa/llm/heuristic.py`
- Test: `backend/tests/test_llm_vision_seam.py`

**Interfaces:**
- Consumes: `VisionExtraction` (Task 2), `ModelChoice` (existing).
- Produces: `LLMClient.extract_image(*, choice, system, user, image, media_type, output_format) -> T`
  on all three implementations.

**Why this task exists separately from Task 4.** `AnthropicLLM` is never instantiated in tests. Getting the offline implementations right is what keeps CI green and the demo working, and it is fully testable; the real client is not. Do not merge these.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_llm_vision_seam.py`:

```python
from ley_khaa.llm.client import FakeLLM
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.llm.router import Stage, model_for
from ley_khaa.vision.contract import VisionExtraction

IMAGE = b"\x89PNG\r\n\x1a\nfake"
# model_for is a module-level FUNCTION — there is no ModelRouter class.
CHOICE = model_for(Stage.VISION_EXTRACTION)


def test_the_heuristic_client_returns_an_unread_record():
    """No vision backend must degrade, never raise: the zero-account demo has
    to complete (spec §3.6)."""
    result = HeuristicLLM().extract_image(
        choice=CHOICE, system="s", user="chart.png",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )

    assert isinstance(result, VisionExtraction)
    assert result.content == "", "empty content IS the 'was not read' signal"
    assert "chart.png" in result.summary
    assert result.kind == "text"


def test_the_heuristic_client_is_deterministic():
    """Two calls on the same image agree, or the cache's byte-identity test
    would pass for the wrong reason."""
    llm = HeuristicLLM()
    kwargs = dict(
        choice=CHOICE, system="s", user="chart.png",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )
    assert llm.extract_image(**kwargs) == llm.extract_image(**kwargs)


def test_the_heuristic_client_never_touches_the_image_bytes():
    """It has no vision. If it ever appears to read one, something has been
    wired to a real backend by accident."""
    llm = HeuristicLLM()
    a = llm.extract_image(
        choice=CHOICE, system="s", user="chart.png",
        image=b"totally different bytes", media_type="image/png",
        output_format=VisionExtraction,
    )
    b = llm.extract_image(
        choice=CHOICE, system="s", user="chart.png",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )
    assert a == b


def test_the_fake_client_returns_its_queued_response():
    queued = VisionExtraction(kind="table", content="a,b\n1,2", summary="a table")
    llm = FakeLLM([queued])

    result = llm.extract_image(
        choice=CHOICE, system="s", user="u",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )

    assert result is queued


def test_the_fake_client_records_the_call_like_parse_does():
    llm = FakeLLM([VisionExtraction(kind="text", content="x", summary="y")])
    llm.extract_image(
        choice=CHOICE, system="sys", user="usr",
        image=IMAGE, media_type="image/png", output_format=VisionExtraction,
    )

    assert llm.calls, "a vision call must be recorded, or a call-counter test cannot see it"
    assert llm.calls[-1].choice == CHOICE


def test_every_client_reports_a_name():
    """The manifest records who ACTUALLY produced an extraction."""
    assert HeuristicLLM().name == "heuristic"
    assert FakeLLM([]).name
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_llm_vision_seam.py -q
```

Expected: FAIL — `AttributeError: 'HeuristicLLM' object has no attribute 'extract_image'`.

- [ ] **Step 3: Extend the protocol**

In `backend/ley_khaa/llm/client.py`, add to the `LLMClient` Protocol, directly after `parse`:

```python
    def extract_image(
        self,
        *,
        choice: ModelChoice,
        system: str,
        user: str,
        image: bytes,
        media_type: str,
        output_format: type[T],
    ) -> T:
        """Read one image into a structured result (spec §3.4).

        Separate from parse() rather than an optional argument on it: every
        implementation must consciously answer "what do I do with an image?",
        and the offline ones answer "nothing, and I say so" — which is a
        different behaviour, not a degenerate case of text parsing.
        """
        ...
```

- [ ] **Step 4: Implement it on `FakeLLM`**

Find `FakeLLM.parse` in the same file and add alongside it, recording the call exactly as `parse` does so a call counter can see vision calls:

```python
    def extract_image(
        self, *, choice: ModelChoice, system: str, user: str,
        image: bytes, media_type: str, output_format: type[T],
    ) -> T:
        return self.parse(choice=choice, system=system, user=user, output_format=output_format)
```

Delegating to `parse` is deliberate: `FakeLLM`'s whole job is to hand back the next queued response and record that it was asked, and that logic must not exist twice.

- [ ] **Step 5: Implement it on `HeuristicLLM`**

In `backend/ley_khaa/llm/heuristic.py`, add to the class:

```python
    def extract_image(
        self, *, choice: ModelChoice, system: str, user: str,
        image: bytes, media_type: str, output_format: type[T],
    ) -> T:
        """The offline stand-in has no vision, and says so (spec §3.6).

        `image` is deliberately unread: this class is regex over text, and a
        result that varied with the bytes would mean something had been wired
        to a real backend by accident. `user` carries the filename so the
        summary can name what it could not read.
        """
        return output_format(
            kind="text",
            content="",
            summary=(
                f"{user or 'an image'} was attached but not read: "
                "no vision backend is configured (set ANTHROPIC_API_KEY)."
            ),
        )
```

No import changes are needed: `heuristic.py` already has `from .router import ModelChoice` and defines `T = TypeVar("T", bound=BaseModel)` at line 16.

- [ ] **Step 6: Run the tests and watch them pass**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_llm_vision_seam.py -q
```

Expected: PASS (6 passed).

- [ ] **Step 7: Mutation-test**

1. Make `HeuristicLLM.extract_image` return `content=str(len(image))` → `test_the_heuristic_client_never_touches_the_image_bytes` must fail. Restore.
2. Make `FakeLLM.extract_image` construct a fresh result instead of delegating to `parse` → `test_the_fake_client_records_the_call_like_parse_does` must fail on the empty `calls` list. Restore.

**Report the actual outcome of each**, including any that does not discriminate.

- [ ] **Step 8: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/llm/client.py backend/ley_khaa/llm/heuristic.py backend/tests/test_llm_vision_seam.py
git commit -m "$(cat <<'EOF'
feat(llm): add extract_image to the seam and its offline stand-ins

A separate method rather than an argument on parse(): every implementation has
to consciously answer "what do I do with an image?", and the offline ones
answer "nothing, and I say so" — a different behaviour, not a degenerate case
of text parsing. HeuristicLLM deliberately ignores the bytes, and a test pins
that: a result varying with the image would mean something had been wired to a
real backend by accident.
EOF
)"
```

---

## Task 4: `AnthropicLLM.extract_image` — the real call

**Files:**
- Modify: `backend/ley_khaa/llm/client.py`
- Test: `backend/tests/test_anthropic_vision_call.py`

**Interfaces:**
- Consumes: `VisionExtraction`, `ModelChoice`.
- Produces: nothing new — fills in the third implementation of `extract_image`.

**`AnthropicLLM` is never instantiated from tests against the network.** It takes an injected
`client` in its constructor (`AnthropicLLM(client=...)`), which is how the request shape gets
asserted without a key. That injection point already exists — use it, do not add a new one.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_anthropic_vision_call.py`:

```python
import base64

from ley_khaa.llm.client import AnthropicLLM
from ley_khaa.llm.router import Stage, model_for
from ley_khaa.vision.contract import VisionExtraction

IMAGE = b"\x89PNG\r\n\x1a\nfake-bytes"


class _Recorder:
    """Stands in for anthropic.Anthropic. Records the request it was handed."""

    def __init__(self, parsed):
        self.parsed = parsed
        self.kwargs = None
        self.messages = self

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return type("R", (), {"parsed_output": self.parsed})()


def _call(choice=None):
    parsed = VisionExtraction(kind="table", content="a,b\n1,2", summary="a table")
    rec = _Recorder(parsed)
    result = AnthropicLLM(client=rec).extract_image(
        choice=choice or model_for(Stage.VISION_EXTRACTION),
        system="read this image",
        user="chart.png",
        image=IMAGE,
        media_type="image/png",
        output_format=VisionExtraction,
    )
    return rec, result


def test_the_image_is_sent_as_a_base64_content_block():
    rec, _ = _call()
    content = rec.kwargs["messages"][0]["content"]

    image_block = content[0]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"
    assert image_block["source"]["data"] == base64.standard_b64encode(IMAGE).decode()


def test_the_image_block_precedes_the_text():
    """Anthropic's documented ordering for vision requests."""
    rec, _ = _call()
    content = rec.kwargs["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "text"]


def test_the_parsed_output_is_returned():
    _, result = _call()
    assert isinstance(result, VisionExtraction)
    assert result.content == "a,b\n1,2"


def test_the_vision_stage_routes_to_opus_with_its_own_budget():
    rec, _ = _call()
    assert rec.kwargs["model"] == "claude-opus-5"
    assert rec.kwargs["max_tokens"] == 8000


def test_thinking_is_sent_only_when_the_model_supports_it():
    """Haiku 4.5 returns 400 on `thinking`. The existing supports_thinking gate
    is the guard and must not be bypassed for vision."""
    from dataclasses import replace

    choice = model_for(Stage.VISION_EXTRACTION)
    rec, _ = _call(replace(choice, supports_thinking=False))
    assert "thinking" not in rec.kwargs

    rec, _ = _call(replace(choice, supports_thinking=True))
    assert rec.kwargs["thinking"] == {"type": "adaptive"}


def test_no_image_bytes_appear_in_the_text_prompt():
    """A base64 payload pasted into the text block would double the token bill
    and silently truncate the prompt."""
    rec, _ = _call()
    text_block = rec.kwargs["messages"][0]["content"][1]
    assert base64.standard_b64encode(IMAGE).decode() not in text_block["text"]
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_anthropic_vision_call.py -q
```

Expected: FAIL — `AttributeError: 'AnthropicLLM' object has no attribute 'extract_image'`.

- [ ] **Step 3: Implement it**

In `backend/ley_khaa/llm/client.py`, add to `AnthropicLLM` directly after `parse`:

```python
    def extract_image(
        self, *, choice: ModelChoice, system: str, user: str,
        image: bytes, media_type: str, output_format: type[T],
    ) -> T:
        """parse(), with an image content block ahead of the text.

        The block order is Anthropic's documented shape for vision. The bytes
        go in the image block and nowhere else: pasting base64 into the text
        would double the token bill for no benefit.
        """
        import base64

        kwargs: dict[str, Any] = {
            "model": choice.model,
            "max_tokens": choice.max_tokens,
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(image).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": user},
                    ],
                }
            ],
            "output_format": output_format,
        }
        # Same gate as parse(): adaptive thinking exists only on the 5-series,
        # and sending it to Haiku 4.5 is a 400.
        if choice.supports_thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        response = self._client.messages.parse(**kwargs)
        return response.parsed_output
```

- [ ] **Step 4: Run the tests and watch them pass**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_anthropic_vision_call.py -q
```

Expected: PASS (6 passed).

- [ ] **Step 5: Mutation-test**

1. Swap the content-block order (text first) → `test_the_image_block_precedes_the_text` must fail.
2. Delete the `if choice.supports_thinking:` guard so `thinking` is always sent →
   `test_thinking_is_sent_only_when_the_model_supports_it` must fail on its first assertion.
3. Append the base64 string to the text block → `test_no_image_bytes_appear_in_the_text_prompt`
   must fail.

Restore after each. **Report the actual outcome of each.**

- [ ] **Step 6: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/llm/client.py backend/tests/test_anthropic_vision_call.py
git commit -m "$(cat <<'EOF'
feat(llm): send an image to Claude for structured extraction

parse() with an image content block ahead of the text, asserted through the
constructor's existing client injection so the request shape is pinned without
a key or a network call. The supports_thinking gate is reused rather than
reimplemented — Haiku 4.5 still 400s on `thinking`.
EOF
)"
```

---

## Task 5: `ImageFetcher` — the security boundary

**Files:**
- Create: `backend/ley_khaa/vision/fetcher.py`
- Test: `backend/tests/test_image_fetcher.py`

**Interfaces:**
- Produces:
  - `class FetchRefused(Exception)` — carries a human-readable reason.
  - `ImageFetcher(*, allowed_hosts: frozenset[str], max_bytes: int, slack_token: str = "", timeout: float = 10.0)`
  - `.fetch(url: str) -> tuple[bytes, str]` returning `(image_bytes, media_type)`, raising
    `FetchRefused` for every rejection.

**This is the phase's security surface and the reason it is its own task.** Resolving a Slack
`url_private` means handing the bot token to an HTTP client whose URL came from a platform payload.
Four rules, each with its own test:

1. https only, and the host must be allowlisted.
2. **The Slack token is attached only to Slack hosts** — an allowlisted non-Slack host gets no
   `Authorization` header.
3. **Redirects are not followed** — a 302 to an off-allowlist host would defeat rules 1 and 2.
4. The body is capped as it is read, not by `Content-Length`, which is attacker-controlled.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_image_fetcher.py`:

```python
import pytest

from ley_khaa.vision.fetcher import FetchRefused, ImageFetcher

ALLOWED = frozenset({"files.slack.com", "cdn.discordapp.com"})
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


class _Response:
    def __init__(self, body=PNG, status=200, content_type="image/png"):
        self._body = body
        self.status_code = status
        self.headers = {"Content-Type": content_type}

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def close(self):
        pass


class _Transport:
    """Records what the fetcher tried to send."""

    def __init__(self, response=None):
        self.response = response or _Response()
        self.calls = []

    def __call__(self, url, *, headers, timeout, allow_redirects):
        self.calls.append(
            {"url": url, "headers": headers, "timeout": timeout, "allow_redirects": allow_redirects}
        )
        return self.response


def _fetcher(transport, **over):
    return ImageFetcher(
        allowed_hosts=over.pop("allowed_hosts", ALLOWED),
        max_bytes=over.pop("max_bytes", 1024),
        slack_token=over.pop("slack_token", "xoxb-secret"),
        transport=transport,
    )


def test_an_allowlisted_slack_url_is_fetched():
    t = _Transport()
    data, media_type = _fetcher(t).fetch("https://files.slack.com/f/abc.png")
    assert data == PNG
    assert media_type == "image/png"


def test_the_slack_token_is_sent_to_a_slack_host():
    t = _Transport()
    _fetcher(t).fetch("https://files.slack.com/f/abc.png")
    assert t.calls[0]["headers"]["Authorization"] == "Bearer xoxb-secret"


def test_the_slack_token_is_NEVER_sent_to_a_non_slack_host():
    """THE rule of this module. A payload-supplied URL on an allowlisted but
    non-Slack host must not receive the workspace's bot token."""
    t = _Transport()
    _fetcher(t).fetch("https://cdn.discordapp.com/attachments/1/2/a.png")

    headers = t.calls[0]["headers"]
    assert "Authorization" not in headers
    assert "xoxb-secret" not in repr(headers)


def test_redirects_are_not_followed():
    """A 302 to an off-allowlist host would defeat both the allowlist and the
    token rule, because the check already passed by then."""
    t = _Transport()
    _fetcher(t).fetch("https://files.slack.com/f/abc.png")
    assert t.calls[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://files.slack.com/f/abc.png",          # not https
        "https://evil.example.com/a.png",            # not allowlisted
        "https://files.slack.com.evil.com/a.png",    # suffix trick
        "ftp://files.slack.com/a.png",               # not http at all
        "https://127.0.0.1/a.png",                   # not allowlisted
        "not a url",
    ],
)
def test_a_disallowed_url_is_refused_before_any_request(url):
    t = _Transport()
    with pytest.raises(FetchRefused):
        _fetcher(t).fetch(url)
    assert t.calls == [], "a refused URL must never be requested"


def test_a_body_over_the_cap_is_refused():
    t = _Transport(_Response(body=b"y" * 5000))
    with pytest.raises(FetchRefused):
        _fetcher(t, max_bytes=1024).fetch("https://files.slack.com/f/big.png")


def test_the_cap_is_enforced_on_the_body_not_on_content_length():
    """Content-Length is attacker-controlled. A small declared length with a
    huge body must still be refused."""
    response = _Response(body=b"y" * 5000)
    response.headers["Content-Length"] = "10"
    with pytest.raises(FetchRefused):
        _fetcher(_Transport(response), max_bytes=1024).fetch("https://files.slack.com/f/lie.png")


def test_a_non_image_content_type_is_refused():
    t = _Transport(_Response(content_type="text/html"))
    with pytest.raises(FetchRefused):
        _fetcher(t).fetch("https://files.slack.com/f/login.html")


def test_a_non_200_is_refused():
    t = _Transport(_Response(status=403))
    with pytest.raises(FetchRefused):
        _fetcher(t).fetch("https://files.slack.com/f/private.png")


def test_the_refusal_reason_never_contains_the_token():
    """A FetchRefused reaches a dead letter, and a dead letter is read by a
    human in a browser."""
    t = _Transport(_Response(status=403))
    try:
        _fetcher(t).fetch("https://files.slack.com/f/private.png")
    except FetchRefused as exc:
        assert "xoxb-secret" not in str(exc)
    else:
        pytest.fail("expected FetchRefused")
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_image_fetcher.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.vision.fetcher'`.

- [ ] **Step 3: Implement it**

Create `backend/ley_khaa/vision/fetcher.py`:

```python
"""Turn a channel attachment URL into image bytes, under an explicit boundary.

This is the phase's security surface. Resolving a Slack `url_private` means
handing the workspace's bot token to an HTTP client whose URL arrived inside a
platform payload, so every rule below exists to bound what that can do.
"""
from __future__ import annotations

from urllib.parse import urlparse

_SLACK_HOSTS = frozenset({"files.slack.com"})
_CHUNK = 64 * 1024


class FetchRefused(Exception):
    """The URL was not fetched. The reason is safe to show a human."""


class ImageFetcher:
    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        slack_token: str = "",
        timeout: float = 10.0,
        transport=None,
    ) -> None:
        self.allowed_hosts = allowed_hosts
        self.max_bytes = max_bytes
        self._slack_token = slack_token
        self.timeout = timeout
        # Injected so the boundary is testable without a network. Defaults to
        # requests.get, which is already a dependency of the backend.
        self._transport = transport

    def _get(self, url, *, headers, timeout, allow_redirects):
        if self._transport is not None:
            return self._transport(url, headers=headers, timeout=timeout, allow_redirects=allow_redirects)
        import requests

        return requests.get(
            url, headers=headers, timeout=timeout, allow_redirects=allow_redirects, stream=True
        )

    def fetch(self, url: str) -> tuple[bytes, str]:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise FetchRefused(f"refusing a non-https image url ({parsed.scheme or 'no scheme'})")
        host = parsed.hostname or ""
        # Exact host match, never a suffix test: "files.slack.com.evil.com"
        # ends with an allowlisted name and is a different host entirely.
        if host not in self.allowed_hosts:
            raise FetchRefused(f"image host {host!r} is not allowlisted")

        headers = {}
        # THE rule: the token goes to Slack and nowhere else. An allowlisted
        # host is not automatically a trusted recipient of a credential.
        if host in _SLACK_HOSTS and self._slack_token:
            headers["Authorization"] = f"Bearer {self._slack_token}"

        response = self._get(
            url,
            headers=headers,
            timeout=self.timeout,
            # A 302 to an off-allowlist host would defeat both checks above,
            # because they already passed on the original URL.
            allow_redirects=False,
        )
        try:
            if response.status_code != 200:
                raise FetchRefused(f"image url returned HTTP {response.status_code}")
            media_type = (response.headers.get("Content-Type") or "").split(";")[0].strip()
            if not media_type.startswith("image/"):
                raise FetchRefused(f"image url served {media_type or 'no content type'}")

            body = bytearray()
            for chunk in response.iter_content(chunk_size=_CHUNK):
                body.extend(chunk)
                # Checked as the body arrives. Content-Length is written by the
                # server and cannot be trusted to bound anything.
                if len(body) > self.max_bytes:
                    raise FetchRefused(f"image exceeds {self.max_bytes} bytes")
            return bytes(body), media_type
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
```

- [ ] **Step 4: Run the tests and watch them pass**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_image_fetcher.py -q
```

Expected: PASS (15 passed — the parametrized URL test contributes 6).

- [ ] **Step 5: Mutation-test every rule**

Each of these must fail its named test. Restore after each, from a saved copy — **never
`git checkout`, which would discard uncommitted work in the same file.**

1. Change the host check to `if not any(host.endswith(h) for h in self.allowed_hosts)` →
   `test_a_disallowed_url_is_refused_before_any_request[https://files.slack.com.evil.com/a.png]`
   must fail. This is the suffix trick and it is easy to write by accident.
2. Attach the `Authorization` header unconditionally →
   `test_the_slack_token_is_NEVER_sent_to_a_non_slack_host` must fail.
3. `allow_redirects=True` → `test_redirects_are_not_followed` must fail.
4. Bound on `int(response.headers.get("Content-Length", 0))` instead of the accumulated body →
   `test_the_cap_is_enforced_on_the_body_not_on_content_length` must fail while
   `test_a_body_over_the_cap_is_refused` still passes. That pairing is what makes the cap
   untunable in either direction.

**Report the actual outcome of each.**

- [ ] **Step 6: Confirm `requests` is already a dependency**

```bash
grep -n "requests" backend/pyproject.toml || echo "NOT A DEPENDENCY — add requests to [project].dependencies, pinned, and rerun pip install -e '.[dev]'"
```

If it is absent, add it pinned in the same style as `slack_sdk==3.44.0`, then
`cd backend && ../.venv/bin/pip install -e '.[dev]'`. **The sandbox image is untouched** —
synthesized code has no business fetching URLs.

- [ ] **Step 7: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/vision/fetcher.py backend/tests/test_image_fetcher.py backend/pyproject.toml
git commit -m "$(cat <<'EOF'
feat(vision): fetch image bytes under an explicit boundary

Resolving a Slack url_private hands the workspace bot token to an HTTP client
whose URL arrived in a platform payload. Four rules, each with its own test:
https and an EXACT host match (a suffix test would accept
files.slack.com.evil.com); the token goes to Slack hosts and nowhere else;
redirects are not followed, since a 302 defeats both checks after they have
passed; and the size cap is enforced on the body as it arrives, because
Content-Length is written by the server.
EOF
)"
```

---

## Task 6: `VisionExtractor` — the cache, and degradation

**Files:**
- Create: `backend/ley_khaa/vision/extractor.py`
- Test: `backend/tests/test_vision_extractor.py`

**Interfaces:**
- Consumes: `ImageExtractionRepository`, `sha256_of` (Task 2); `LLMClient.extract_image` (Task 3);
  `ImageFetcher`, `FetchRefused` (Task 5); `DeadLetterRepository.record(*, source, kind, reason, payload=None)`.
- Produces:
  ```python
  VisionExtractor(
      *, llm, extractions: ImageExtractionRepository,
      fetcher: ImageFetcher | None = None,
      dead_letter: Callable[..., None] | None = None,
      enabled: bool = True,
  )
  ```
  with `.extract(attachment: dict) -> ImageExtractionRow` — **always a row, never `None`**.

**The one rule: an unreadable image never blocks a task** (spec §3.6). Everything here either
returns a real extraction or returns the unread record.

**Bytes come from one of two places.** An attachment's `content` is a URL when it came from a
channel (Phase 6 stores Slack's `url_private` / Discord's `url` there) and base64 when it came
through the API or dashboard. The extractor decides by looking at the string, not by a new field.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_vision_extractor.py`:

```python
import base64

from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.persistence.image_extraction_repository import (
    ImageExtractionRepository,
    sha256_of,
)
from ley_khaa.vision.contract import VisionExtraction
from ley_khaa.vision.extractor import VisionExtractor
from ley_khaa.vision.fetcher import FetchRefused

PNG = b"\x89PNG\r\n\x1a\nbytes"
B64 = base64.standard_b64encode(PNG).decode()


def _image(content=B64, name="chart.png"):
    return {"kind": "image", "name": name, "content": content}


class _CountingLLM:
    """Wraps the REAL offline client and counts calls.

    A canned fake would let a broken cache pass by returning the same thing
    twice; only a counter around the real client proves no call was made.
    """

    def __init__(self, inner=None, result=None):
        self.inner = inner or HeuristicLLM()
        self.result = result
        self.calls = []
        self.name = self.inner.name

    def extract_image(self, **kwargs):
        self.calls.append(kwargs)
        if self.result is not None:
            return self.result
        return self.inner.extract_image(**kwargs)

    def parse(self, **kwargs):
        return self.inner.parse(**kwargs)


class _StubFetcher:
    def __init__(self, result=(PNG, "image/png"), error=None):
        self.result = result
        self.error = error
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.result


def _extractor(session, **over):
    return VisionExtractor(
        llm=over.pop("llm", _CountingLLM()),
        extractions=ImageExtractionRepository(session),
        fetcher=over.pop("fetcher", _StubFetcher()),
        dead_letter=over.pop("dead_letter", None),
        enabled=over.pop("enabled", True),
    )


def test_an_inline_base64_image_is_extracted(session):
    llm = _CountingLLM(result=VisionExtraction(kind="table", content="a,b\n1,2", summary="a table"))
    row = _extractor(session, llm=llm).extract(_image())

    assert row.kind == "table"
    assert row.content == "a,b\n1,2"
    assert row.image_sha256 == sha256_of(PNG)
    assert len(llm.calls) == 1


def test_a_url_image_is_fetched_then_extracted(session):
    fetcher = _StubFetcher()
    llm = _CountingLLM(result=VisionExtraction(kind="text", content="hello", summary="a note"))

    row = _extractor(session, llm=llm, fetcher=fetcher).extract(
        _image(content="https://files.slack.com/f/a.png")
    )

    assert fetcher.urls == ["https://files.slack.com/f/a.png"]
    assert row.content == "hello"


def test_the_second_extraction_of_the_same_image_makes_NO_model_call(session):
    """The phase's cache claim, proven by a counter around the real client
    rather than by two runs happening to agree."""
    llm = _CountingLLM(result=VisionExtraction(kind="table", content="a,b\n1,2", summary="t"))
    extractor = _extractor(session, llm=llm)

    first = extractor.extract(_image())
    assert len(llm.calls) == 1

    second = extractor.extract(_image())

    assert llm.calls == llm.calls[:1], "a cache hit must make no further call"
    assert len(llm.calls) == 1
    assert second.content == first.content
    assert second.image_sha256 == first.image_sha256


def test_the_cache_is_keyed_on_bytes_not_on_filename(session):
    """The same screenshot pasted twice under different names is one call."""
    llm = _CountingLLM(result=VisionExtraction(kind="text", content="x", summary="s"))
    extractor = _extractor(session, llm=llm)

    extractor.extract(_image(name="one.png"))
    extractor.extract(_image(name="two.png"))

    assert len(llm.calls) == 1


def test_a_different_image_is_a_different_key(session):
    llm = _CountingLLM(result=VisionExtraction(kind="text", content="x", summary="s"))
    extractor = _extractor(session, llm=llm)

    extractor.extract(_image())
    extractor.extract(_image(content=base64.standard_b64encode(b"different").decode()))

    assert len(llm.calls) == 2


def test_with_no_vision_backend_the_record_says_it_was_not_read(session):
    """The offline path. The zero-account demo must complete (spec §3.6)."""
    row = _extractor(session, llm=_CountingLLM()).extract(_image())

    assert row.content == ""
    assert "chart.png" in row.summary
    assert row.model == "heuristic", "the manifest must credit who ACTUALLY did it"


def test_a_disabled_extractor_makes_no_call_and_returns_the_unread_record(session):
    llm = _CountingLLM(result=VisionExtraction(kind="table", content="a", summary="s"))
    row = _extractor(session, llm=llm, enabled=False).extract(_image())

    assert llm.calls == []
    assert row.content == ""


def test_a_refused_fetch_dead_letters_and_still_returns_a_record(session):
    dead = []
    fetcher = _StubFetcher(error=FetchRefused("image host 'evil.example.com' is not allowlisted"))

    row = _extractor(
        session, fetcher=fetcher, dead_letter=lambda **kw: dead.append(kw)
    ).extract(_image(content="https://evil.example.com/a.png"))

    assert row.content == "", "a refused fetch must not block the task"
    assert len(dead) == 1
    assert dead[0]["kind"] == "inbound"
    assert "not allowlisted" in dead[0]["reason"]


def test_a_model_error_dead_letters_and_still_returns_a_record(session):
    class _Boom:
        name = "anthropic"

        def extract_image(self, **kwargs):
            raise RuntimeError("the model is down")

    dead = []
    row = _extractor(session, llm=_Boom(), dead_letter=lambda **kw: dead.append(kw)).extract(_image())

    assert row.content == ""
    assert len(dead) == 1
    assert "the model is down" in dead[0]["reason"]


def test_a_failed_extraction_is_STORED_so_it_is_not_retried(session):
    """Otherwise every re-drive re-attempts a fetch that will fail again, and a
    task that repairs three times pays for it three times."""
    calls = []

    class _Boom:
        name = "anthropic"

        def extract_image(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("down")

    extractor = _extractor(session, llm=_Boom(), dead_letter=lambda **kw: None)
    extractor.extract(_image())
    extractor.extract(_image())

    assert len(calls) == 1, "the unread record must be cached like any other"


def test_a_non_image_attachment_is_refused(session):
    """The caller filters, but this is the last line before a text blob is sent
    to a vision model as if it were a picture."""
    llm = _CountingLLM()
    extractor = _extractor(session, llm=llm)
    row = extractor.extract({"kind": "table", "name": "a.csv", "content": "a,b\n1,2"})

    assert row.content == ""
    assert llm.calls == []
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_vision_extractor.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.vision.extractor'`.

- [ ] **Step 3: Implement it**

Create `backend/ley_khaa/vision/extractor.py`:

```python
"""Read an image once, and remember it (spec §3.2, §3.6).

One rule governs this module: an unreadable image NEVER blocks a task. Every
path either returns a real extraction or returns the unread record, and the
unread record is stored like any other so a re-drive does not retry a failure.
"""
from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Callable

from ..llm.router import Stage, model_for
from ..persistence.image_extraction_repository import ImageExtractionRepository, sha256_of
from ..persistence.orm import ImageExtractionRow
from .contract import VisionExtraction
from .fetcher import FetchRefused, ImageFetcher

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You read one image and return its content as structured data. If the image is a table, "
    "chart or spreadsheet, set kind='table' and put the data in content as CSV with a header "
    "row. Otherwise set kind='text' and put what the image says in content. summary is one "
    "short sentence describing what the image is."
)


class VisionExtractor:
    def __init__(
        self,
        *,
        llm,
        extractions: ImageExtractionRepository,
        fetcher: ImageFetcher | None = None,
        dead_letter: Callable[..., None] | None = None,
        enabled: bool = True,
    ) -> None:
        self.llm = llm
        self.extractions = extractions
        self.fetcher = fetcher
        self.dead_letter = dead_letter
        self.enabled = enabled

    def extract(self, attachment: dict) -> ImageExtractionRow:
        """One image -> its checkpoint row. Always returns a row."""
        name = str(attachment.get("name") or "an image")
        if attachment.get("kind") != "image":
            # The callers filter, but this is the last line before a CSV would
            # be handed to a vision model as if it were a picture.
            return self._unread(b"", name, reason="not an image attachment", media_type="")

        try:
            image, media_type = self._bytes_for(attachment)
        except (FetchRefused, ValueError, binascii.Error) as exc:
            reason = f"could not read {name}: {exc}"
            self._record_drop(reason, name)
            return self._unread(b"", name, reason=reason, media_type="")

        digest = sha256_of(image)
        cached = self.extractions.get(digest)
        if cached is not None:
            return cached

        if not self.enabled:
            return self._store(digest, image, media_type, self._unread_extraction(name, "vision is disabled"))

        try:
            extraction = self.llm.extract_image(
                choice=model_for(Stage.VISION_EXTRACTION),
                system=_SYSTEM,
                user=name,
                image=image,
                media_type=media_type,
                output_format=VisionExtraction,
            )
        except Exception as exc:  # noqa: BLE001 - an unread image must not fail a task
            logger.exception("extracting %s failed", name)
            reason = f"could not read {name}: {type(exc).__name__}: {exc}"
            self._record_drop(reason, name)
            # STORED, not just returned: otherwise every re-drive retries a
            # failure that will fail again, and a task that repairs three times
            # pays for it three times.
            return self._store(digest, image, media_type, self._unread_extraction(name, reason))

        return self._store(digest, image, media_type, extraction)

    # -- helpers ---------------------------------------------------------

    def _bytes_for(self, attachment: dict) -> tuple[bytes, str]:
        content = str(attachment.get("content") or "")
        if content.startswith("http://") or content.startswith("https://"):
            if self.fetcher is None:
                raise FetchRefused("no image fetcher is configured")
            return self.fetcher.fetch(content)
        if not content:
            raise ValueError("the attachment carries no content")
        # validate=True so a text blob raises here rather than decoding into
        # garbage bytes that get billed to a vision call.
        return base64.standard_b64decode(content, validate=True), "image/png"

    def _unread_extraction(self, name: str, reason: str) -> VisionExtraction:
        return VisionExtraction(kind="text", content="", summary=f"{name} was not read: {reason}")

    def _unread(self, image: bytes, name: str, *, reason: str, media_type: str) -> ImageExtractionRow:
        """An unread record for something that never got as far as a cache key."""
        return ImageExtractionRow(
            image_sha256=sha256_of(image),
            kind="text",
            content="",
            summary=f"{name} was not read: {reason}",
            media_type=media_type,
            byte_size=len(image),
            model=getattr(self.llm, "name", ""),
        )

    def _store(
        self, digest: str, image: bytes, media_type: str, extraction: VisionExtraction
    ) -> ImageExtractionRow:
        return self.extractions.record(
            image_sha256=digest,
            extraction=extraction,
            media_type=media_type,
            byte_size=len(image),
            # LLMClient.name, so the manifest credits who ACTUALLY produced it.
            model=getattr(self.llm, "name", ""),
        )

    def _record_drop(self, reason: str, name: str) -> None:
        if self.dead_letter is None:
            return
        try:
            self.dead_letter(source="vision", kind="inbound", reason=reason, payload={"name": name})
        except Exception:  # noqa: BLE001
            logger.exception("could not dead-letter a failed extraction")
```

- [ ] **Step 4: Run the tests and watch them pass**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_vision_extractor.py -q
```

Expected: PASS (11 passed).

- [ ] **Step 5: Mutation-test the cache and the degradation**

1. Delete the `cached is not None` early return →
   `test_the_second_extraction_of_the_same_image_makes_NO_model_call` must fail on `len(calls) == 2`.
2. Key on `attachment["name"]` instead of `sha256_of(image)` →
   `test_the_cache_is_keyed_on_bytes_not_on_filename` must fail.
3. In the model-error branch, return the unread record **without** storing it →
   `test_a_failed_extraction_is_STORED_so_it_is_not_retried` must fail with 2 calls.
4. Re-raise instead of catching in the model-error branch →
   `test_a_model_error_dead_letters_and_still_returns_a_record` must fail with the RuntimeError
   escaping.

Restore each from a saved copy — **never `git checkout` while the file has uncommitted work.**
**Report the actual outcome of each.**

- [ ] **Step 6: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/vision/extractor.py backend/tests/test_vision_extractor.py
git commit -m "$(cat <<'EOF'
feat(vision): extract an image once and remember it

The cache row IS the frozen checkpoint, keyed on the sha256 of the bytes — so
the same screenshot pasted under two names costs one call, and the identity
survives re-drives and repair loops. Proven by a counter wrapped around the
REAL offline client, not a canned fake that would agree with itself.

A failed extraction is STORED, not just returned: otherwise a task that repairs
three times retries a dead URL three times. Nothing here can fail a task.
EOF
)"
```

---

## Task 7: Configuration

**Files:**
- Modify: `backend/ley_khaa/config.py`
- Modify: `docker-compose.yml`
- Test: `backend/tests/test_vision_config.py`

**Interfaces:**
- Produces: `settings.vision_enabled: bool`, `settings.image_hosts: str`, `settings.image_max_bytes: int`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_vision_config.py`:

```python
from dataclasses import replace

from ley_khaa.adapters.base import channel_set
from ley_khaa.config import Settings


def test_vision_is_on_by_default():
    assert Settings().vision_enabled is True


def test_vision_can_be_turned_off():
    assert replace(Settings(), vision_enabled=False).vision_enabled is False


def test_the_default_host_allowlist_covers_both_platforms():
    hosts = channel_set(Settings().image_hosts)
    assert "files.slack.com" in hosts
    assert "cdn.discordapp.com" in hosts
    assert "media.discordapp.net" in hosts


def test_the_allowlist_parses_with_the_same_helper_channels_use():
    """One parser for every comma-separated allowlist in the project, so the
    empty-means-empty rule cannot drift between them."""
    assert channel_set("a.example, b.example") == frozenset({"a.example", "b.example"})
    assert channel_set("") == frozenset()


def test_the_byte_cap_has_a_sane_default():
    assert Settings().image_max_bytes == 5 * 1024 * 1024
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_vision_config.py -q
```

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'vision_enabled'`.

- [ ] **Step 3: Add the settings**

In `backend/ley_khaa/config.py`, inside the frozen `Settings` dataclass, beside the other
`LEY_KHAA_*` fields:

```python
    # Vision intake (spec §5 of the phase 7 design). Off means an image is
    # carried but not read, which is the same shape as having no API key.
    vision_enabled: bool = os.getenv("LEY_KHAA_VISION", "on") != "off"
    # Exact hostnames an image may be fetched from. Parsed with
    # adapters.base.channel_set, the same helper the channel allowlists use —
    # one parser, so "empty means empty" cannot drift between them.
    image_hosts: str = os.getenv(
        "LEY_KHAA_IMAGE_HOSTS",
        "files.slack.com,cdn.discordapp.com,media.discordapp.net",
    )
    image_max_bytes: int = int(os.getenv("LEY_KHAA_IMAGE_MAX_BYTES", str(5 * 1024 * 1024)))
```

- [ ] **Step 4: Pass them through compose**

In `docker-compose.yml`, under `backend.environment`, after the channel adapter block:

```yaml
      # Vision intake (phase 7). Unset -> defaults: on, both platforms' CDNs,
      # 5 MB. LEY_KHAA_VISION=off carries images without reading them.
      LEY_KHAA_VISION: ${LEY_KHAA_VISION:-}
      LEY_KHAA_IMAGE_HOSTS: ${LEY_KHAA_IMAGE_HOSTS:-}
      LEY_KHAA_IMAGE_MAX_BYTES: ${LEY_KHAA_IMAGE_MAX_BYTES:-}
```

**Careful:** an unset variable passed as `""` would make `LEY_KHAA_IMAGE_HOSTS` an EMPTY allowlist
rather than the default, and empty means empty. Verify with Step 5's test; if the empty string wins
over the default, change the compose lines to omit the variables entirely rather than pass blanks.

- [ ] **Step 5: Prove the compose passthrough cannot silently empty the allowlist**

Add to `backend/tests/test_vision_config.py`:

```python
def test_an_empty_env_var_does_not_silently_empty_the_allowlist(monkeypatch):
    """docker-compose passes ${VAR:-} for an unset variable, which arrives as
    "". If that beat the default, every image fetch would be refused and the
    only symptom would be dead letters."""
    monkeypatch.setenv("LEY_KHAA_IMAGE_HOSTS", "")
    from importlib import reload

    import ley_khaa.config as config_module

    reload(config_module)
    try:
        assert channel_set(config_module.Settings().image_hosts) != frozenset(), (
            "an empty LEY_KHAA_IMAGE_HOSTS must fall back to the default, "
            "or compose's ${VAR:-} silently disables all image fetching"
        )
    finally:
        monkeypatch.delenv("LEY_KHAA_IMAGE_HOSTS", raising=False)
        reload(config_module)
```

Make it pass by reading the variable with a falsy-safe default:

```python
    image_hosts: str = os.getenv("LEY_KHAA_IMAGE_HOSTS") or (
        "files.slack.com,cdn.discordapp.com,media.discordapp.net"
    )
```

`or` rather than `os.getenv(name, default)`: the two-argument form returns `""` for a variable that
is set-but-empty, which is exactly what compose sends.

- [ ] **Step 6: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/config.py docker-compose.yml backend/tests/test_vision_config.py
git commit -m "$(cat <<'EOF'
feat(config): add the vision settings and pass them through compose

image_hosts reads with `or` rather than a two-argument getenv: compose sends
${VAR:-} as an empty string for an unset variable, and "" would otherwise beat
the default and silently refuse every image fetch — with dead letters as the
only symptom. A test pins that.
EOF
)"
```

---

## Task 8: The interpreter reads the summary

**Files:**
- Modify: `backend/ley_khaa/interpreter/interpreter.py`
- Test: `backend/tests/test_interpreter_vision.py`

**Interfaces:**
- Consumes: `VisionExtractor.extract` (Task 6).
- Produces: `Interpreter(llm, messages, extractor=None)` — a third, optional constructor argument.

**Optional, defaulting to `None`.** Every existing construction site (`TaskDriver`, and any test
building an `Interpreter` directly) keeps working unchanged, and with no extractor the rendering is
byte-identical to today's. That is what keeps the ~840 existing tests meaningful rather than
rewritten.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_interpreter_vision.py`:

```python
from ley_khaa.interpreter.interpreter import _render
from ley_khaa.persistence.orm import ImageExtractionRow


class _Row:
    """The two attributes _render reads from a message."""

    def __init__(self, attachments):
        self.id = "m1"
        self.author = "ana"
        self.text = "compare these"
        self.attachments = attachments


class _Task:
    title = "compare the holdings"


class _Extractor:
    def __init__(self, summary="a table of holdings", content="a,b\n1,2"):
        self.row = ImageExtractionRow(
            image_sha256="d" * 64, kind="table", content=content,
            summary=summary, media_type="image/png", byte_size=10, model="anthropic",
        )
        self.seen = []

    def extract(self, attachment):
        self.seen.append(attachment)
        return self.row


IMAGE = {"kind": "image", "name": "chart.png", "content": "https://files.slack.com/f/a.png"}
TABLE = {"kind": "table", "name": "h.csv", "content": "a,b\n1,2"}


def test_without_an_extractor_the_rendering_is_unchanged():
    """Every pre-phase-7 caller must see exactly what it saw before."""
    out = _render(_Task(), [_Row([IMAGE])], extractor=None)
    assert "attachment: image named chart.png" in out
    assert "a table of holdings" not in out


def test_an_image_summary_reaches_the_prompt():
    """This IS 'understood via vision' — the interpreter can now reason about
    what the picture contained."""
    out = _render(_Task(), [_Row([IMAGE])], extractor=_Extractor())
    assert "a table of holdings" in out


def test_a_non_image_attachment_is_never_sent_to_the_extractor():
    extractor = _Extractor()
    _render(_Task(), [_Row([TABLE])], extractor=extractor)
    assert extractor.seen == []


def test_an_unread_image_still_renders_its_name():
    """Degradation must leave the interpreter able to say what it could not
    read, rather than silently dropping the attachment."""
    extractor = _Extractor(summary="chart.png was not read: no vision backend", content="")
    out = _render(_Task(), [_Row([IMAGE])], extractor=extractor)

    assert "chart.png" in out
    assert "was not read" in out


def test_the_full_extracted_content_is_NOT_pasted_into_the_prompt():
    """summary exists precisely so a 5000-row CSV does not blow the prompt."""
    big = "col\n" + "\n".join(str(i) for i in range(5000))
    out = _render(_Task(), [_Row([IMAGE])], extractor=_Extractor(content=big))
    assert big not in out
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_interpreter_vision.py -q
```

Expected: FAIL — `TypeError: _render() got an unexpected keyword argument 'extractor'`.

- [ ] **Step 3: Thread the extractor through**

In `backend/ley_khaa/interpreter/interpreter.py`, change the constructor:

```python
    def __init__(self, llm: LLMClient, messages: MessageRepository, extractor=None) -> None:
        self.llm = llm
        self.messages = messages
        # Optional so every pre-phase-7 caller is unchanged, and so a run with
        # no vision backend renders exactly what it rendered before.
        self.extractor = extractor
```

Change the `interpret` call site (`user = _render(task, rows)`) to:

```python
        user = _render(task, rows, extractor=self.extractor)
```

And `_render` itself:

```python
def _render(task: TaskRow, rows: list[MessageRow], extractor=None) -> str:
    lines = ["## Request", f"title: {task.title}", "", "## Messages"]
    for row in rows:
        lines.append(f"[{row.id}] {row.author}: {row.text}")
        for attachment in row.attachments or []:
            line = f"    attachment: {attachment['kind']} named {attachment['name']}"
            # The SUMMARY, never the content: a table's full CSV would crowd out
            # the conversation this prompt exists to interpret.
            if extractor is not None and attachment.get("kind") == "image":
                summary = extractor.extract(attachment).summary
                if summary:
                    line += f" — {summary}"
            lines.append(line)
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests and watch them pass**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_interpreter_vision.py -q
```

Expected: PASS (5 passed).

- [ ] **Step 5: Mutation-test**

1. Append `extractor.extract(attachment).content` instead of `.summary` →
   `test_the_full_extracted_content_is_NOT_pasted_into_the_prompt` must fail.
2. Drop the `attachment.get("kind") == "image"` guard →
   `test_a_non_image_attachment_is_never_sent_to_the_extractor` must fail.

**Report the actual outcome of each.**

- [ ] **Step 6: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/interpreter/interpreter.py backend/tests/test_interpreter_vision.py
git commit -m "$(cat <<'EOF'
feat(interpreter): let an image's summary reach the prompt

The summary, never the content: a 5000-row CSV would crowd out the very
conversation the prompt exists to interpret, which is why the contract carries
both fields. The extractor is optional and defaults to None, so every
pre-phase-7 caller renders byte-identically to before.
EOF
)"
```

---

## Task 9: An image becomes a bindable input

**Files:**
- Modify: `backend/ley_khaa/executor/resolver.py`
- Modify: `backend/ley_khaa/executor/runner.py`
- Test: `backend/tests/test_resolver_vision.py`

**Interfaces:**
- Consumes: `VisionExtractor.extract` (Task 6).
- Produces:
  - `ResolvedInput` gains `extracted_from: str | None = None` (the image sha256) and
    `extracted_by: str | None = None` (the producing model).
  - `resolve_inputs(spec, task, messages, extractor=None)` — a fourth, optional argument.

**The manifest needs no changes.** `runner.py:451` already writes
`{"name", "file", "source", "sha256"}` per input, so `source: "vision"` flows for free. But that
`sha256` is `ResolvedInput.sha256`, which hashes the extracted CONTENT — the spec's DoD wants the
IMAGE's hash and the producing model too, which is what the two new fields carry.

**`_TEXTUAL`'s comment becomes false in this task.** It currently reads *"An IMAGE attachment needs
vision extraction, which is not built in this phase."* Correct it in Step 3 — a false comment in
the module that decides what a script computes on is exactly the class this project keeps fixing.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_resolver_vision.py`:

```python
import pytest

from ley_khaa.executor.resolver import UnresolvedInputs, resolve_inputs
from ley_khaa.interpreter.spec import TaskSpec
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.orm import ImageExtractionRow
from ley_khaa.persistence.repository import TaskRepository
from ley_khaa.domain.models import Attachment, Message


class _Extractor:
    def __init__(self, content="ticker,qty\nAAA,10", kind="table"):
        self.row = ImageExtractionRow(
            image_sha256="a" * 64, kind=kind, content=content,
            summary="a holdings table", media_type="image/png",
            byte_size=99, model="anthropic",
        )

    def extract(self, attachment):
        return self.row


def _task_with_image(session, *, name="holdings.png"):
    messages = MessageRepository(session)
    row = messages.add(
        Message(
            source="discord", client="g", conversation_id="c1", author="ana",
            text="compare these",
            attachments=[Attachment(kind="image", name=name, content="https://cdn.discordapp.com/a.png")],
        )
    )
    task = TaskRepository(session).create(
        project="default", title="compare", source_message_ids=[row.id]
    )
    return task, messages


def _spec(**over) -> TaskSpec:
    return TaskSpec(
        intent="compare", inputs=over.pop("inputs", ["holdings"]),
        operation="set_difference", output_format="csv",
        recipient="me", urgency="normal", missing_fields=[],
        certainty=0.9, **over,
    )


def test_an_image_with_an_extraction_becomes_an_input(session):
    task, messages = _task_with_image(session)

    resolved = resolve_inputs(_spec(), task, messages, extractor=_Extractor())

    assert len(resolved) == 1
    assert resolved[0].content == "ticker,qty\nAAA,10"
    assert resolved[0].source == "vision"


def test_the_provenance_reaches_the_resolved_input(session):
    """The spec's DoD: the manifest must attest the IMAGE's hash and the model
    that read it, neither of which ResolvedInput.sha256 carries."""
    task, messages = _task_with_image(session)

    resolved = resolve_inputs(_spec(), task, messages, extractor=_Extractor())

    assert resolved[0].extracted_from == "a" * 64
    assert resolved[0].extracted_by == "anthropic"
    assert resolved[0].sha256 != resolved[0].extracted_from, (
        "sha256 hashes the extracted CONTENT; extracted_from is the IMAGE"
    )


def test_the_checkpoint_is_named_for_the_image_and_its_kind(session):
    task, messages = _task_with_image(session, name="holdings.png")

    resolved = resolve_inputs(_spec(), task, messages, extractor=_Extractor())

    assert resolved[0].filename == "extracted_holdings.csv"


def test_a_text_extraction_lands_as_txt_not_csv(session):
    task, messages = _task_with_image(session, name="note.png")

    resolved = resolve_inputs(
        _spec(inputs=["note"]), task, messages,
        extractor=_Extractor(kind="text", content="just some words"),
    )

    assert resolved[0].filename == "extracted_note.txt"


def test_an_unread_image_is_NOT_bindable(session):
    """Empty content is the 'was not read' signal. Binding it would hand a
    script an empty file and let it compute a confident wrong answer."""
    task, messages = _task_with_image(session)

    with pytest.raises(UnresolvedInputs):
        resolve_inputs(_spec(), task, messages, extractor=_Extractor(content=""))


def test_without_an_extractor_an_image_is_still_ignored(session):
    """The offline path is byte-identical to pre-phase-7 behaviour."""
    task, messages = _task_with_image(session)

    with pytest.raises(UnresolvedInputs):
        resolve_inputs(_spec(), task, messages, extractor=None)
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_resolver_vision.py -q
```

Expected: FAIL — `TypeError: resolve_inputs() got an unexpected keyword argument 'extractor'`.

- [ ] **Step 3: Implement it**

In `backend/ley_khaa/executor/resolver.py`:

**(a)** Correct the now-false comment and widen the set:

```python
# Only these carry literal content the executor can compute on. An IMAGE
# attachment carries a URL or base64, so it is computable only once vision has
# extracted it — which is what `extractor` in resolve_inputs() does, turning it
# into a synthetic textual attachment before matching (phase 7, spec §3.5).
_TEXTUAL = {AttachmentKind.TABLE.value, AttachmentKind.TEXT.value}
```

**(b)** Add the provenance fields to `ResolvedInput`:

```python
@dataclass(frozen=True)
class ResolvedInput:
    name: str       # the spec input name this satisfies
    filename: str   # what it is called inside inputs/
    content: str
    source: str     # "attachment" | "catalog" | "vision"
    # Set only for source == "vision". sha256 below hashes the extracted
    # CONTENT; these say which IMAGE it came from and who read it, which is
    # what makes a vision-sourced run auditable.
    extracted_from: str | None = None
    extracted_by: str | None = None
```

**(c)** Make images bindable in `_attachments_for`:

```python
def _attachments_for(task: TaskRow, messages: MessageRepository, extractor=None) -> list[dict]:
    rows = messages.get_many(list(task.source_message_ids or []))
    found: list[dict] = []
    for row in rows:
        for attachment in row.attachments or []:
            if attachment.get("kind") in _TEXTUAL:
                found.append(attachment)
                continue
            if extractor is None or attachment.get("kind") != AttachmentKind.IMAGE.value:
                continue
            record = extractor.extract(attachment)
            # Empty content is the "was not read" signal. Binding it would hand
            # a generated script an empty file and let it compute a confident
            # wrong answer, which is worse than asking the human.
            if not record.content:
                continue
            stem = (attachment.get("name") or "image").rsplit(".", 1)[0]
            suffix = "csv" if record.kind == "table" else "txt"
            found.append(
                {
                    "kind": AttachmentKind.TEXT.value,
                    "name": f"extracted_{stem}.{suffix}",
                    "content": record.content,
                    # Carried on the synthetic attachment so resolve_inputs can
                    # stamp provenance without a second extractor call.
                    "_vision": {"from": record.image_sha256, "by": record.model},
                }
            )
    return found
```

**(d)** Stamp provenance in `resolve_inputs`, and take the new argument:

```python
def resolve_inputs(
    spec: TaskSpec, task: TaskRow, messages: MessageRepository, extractor=None
) -> list[ResolvedInput]:
    attachments = _attachments_for(task, messages, extractor)
```

and in the attachment branch, replace the `ResolvedInput(...)` construction with:

```python
        if hit is not None:
            vision = hit.get("_vision")
            resolved.append(
                ResolvedInput(
                    name=name,
                    filename=_unique(
                        _safe_basename(hit.get("name", "")) or f"{name}.csv", taken_filenames
                    ),
                    content=hit.get("content", ""),
                    source="vision" if vision else "attachment",
                    extracted_from=(vision or {}).get("from"),
                    extracted_by=(vision or {}).get("by"),
                )
            )
            continue
```

**(e)** In `backend/ley_khaa/executor/runner.py`, `ExecutionRunner` takes the extractor and passes
it on. Add `extractor=None` to its `__init__`, store it as `self.extractor`, and change line ~138:

```python
            resolved = resolve_inputs(spec, row, self.messages, extractor=self.extractor)
```

- [ ] **Step 4: Run the tests and watch them pass**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_resolver_vision.py -q
```

Expected: PASS (6 passed).

- [ ] **Step 5: Run the FULL suite — this is the step this task exists to survive**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
```

`ResolvedInput` gained two fields with defaults, so every existing construction site should be
unaffected. If anything fails, **do not weaken the assertion to make it green** — re-read what the
test meant and fix the cause. Record every test you touched and why in your report.

- [ ] **Step 6: Mutation-test**

1. Bind an unread image anyway (drop the `if not record.content: continue`) →
   `test_an_unread_image_is_NOT_bindable` must fail.
2. Always use the `csv` suffix → `test_a_text_extraction_lands_as_txt_not_csv` must fail.
3. Set `source="attachment"` for a vision hit → `test_an_image_with_an_extraction_becomes_an_input`
   must fail on the source assertion.

**Report the actual outcome of each.**

- [ ] **Step 7: Commit**

```bash
git add backend/ley_khaa/executor/resolver.py backend/ley_khaa/executor/runner.py backend/tests/test_resolver_vision.py
git commit -m "$(cat <<'EOF'
feat(executor): let an extracted image become a script input

An image with a non-empty extraction becomes a synthetic textual attachment and
binds by filename stem exactly as a pasted CSV does, landing in inputs/ as
extracted_<stem>.csv or .txt. An UNREAD image stays unbindable on purpose:
handing a generated script an empty file lets it compute a confident wrong
answer, which is worse than asking the human.

ResolvedInput gains extracted_from and extracted_by, because its existing
sha256 hashes the extracted content while the manifest needs to attest which
IMAGE it came from and who read it. Also corrects _TEXTUAL's comment, which
said vision "is not built in this phase" and has just stopped being true.
EOF
)"
```

---

## Task 10: Wire the extractor into the driver

**Files:**
- Modify: `backend/ley_khaa/orchestrator/driver.py`
- Modify: `backend/ley_khaa/api/app.py`
- Test: `backend/tests/test_vision_wiring.py`

**Interfaces:**
- Consumes: everything from Tasks 5, 6, 7.
- Produces: `TaskDriver(..., extractor=None)`; `build_vision_extractor(session) -> VisionExtractor`
  in `api/app.py`.

**This is the seam that Phase 6's review proved is easy to leave untested.** Four wiring lines were
mutated there with the whole suite staying green — a system that connects, logs, and silently does
nothing. Every line here gets a test that fails without it.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_vision_wiring.py`:

```python
from dataclasses import replace

from ley_khaa.api import app as app_module
from ley_khaa.config import settings as real_settings
from ley_khaa.crystallizer.gate import ReadinessGate
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.orchestrator.driver import TaskDriver
from ley_khaa.persistence.candidate_repository import CandidateRepository
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.persistence.repository import TaskRepository


def _driver(session, extractor=None):
    return TaskDriver(
        TaskRepository(session), llm=HeuristicLLM(),
        messages=MessageRepository(session),
        candidates=CandidateRepository(session),
        extractor=extractor,
    )


def test_the_driver_hands_the_extractor_to_the_interpreter(session):
    sentinel = object()
    assert _driver(session, sentinel).interpreter.extractor is sentinel


def test_the_driver_hands_the_extractor_to_the_executor(session):
    """Both consumers, or an image is understood but never computed on."""
    sentinel = object()
    assert _driver(session, sentinel).executor.extractor is sentinel


def test_a_driver_with_no_extractor_still_works(session):
    driver = _driver(session)
    assert driver.interpreter.extractor is None
    assert driver.executor.extractor is None


def test_build_vision_extractor_wires_a_real_fetcher(session):
    extractor = app_module.build_vision_extractor(session)

    assert extractor.fetcher is not None
    assert "files.slack.com" in extractor.fetcher.allowed_hosts
    assert extractor.fetcher.max_bytes == real_settings.image_max_bytes


def test_build_vision_extractor_respects_the_off_switch(session, monkeypatch):
    monkeypatch.setattr(app_module, "settings", replace(real_settings, vision_enabled=False))
    assert app_module.build_vision_extractor(session).enabled is False


def test_the_fetcher_is_given_the_slack_token_from_settings(session, monkeypatch):
    monkeypatch.setattr(
        app_module, "settings", replace(real_settings, slack_bot_token="xoxb-wired")
    )
    extractor = app_module.build_vision_extractor(session)
    assert extractor.fetcher._slack_token == "xoxb-wired"


def test_build_orchestrator_gives_its_driver_an_extractor(session):
    """The line that connects all of the above to the running application. It
    was mutated in phase 6's review with the whole suite staying green."""
    driver = app_module.build_orchestrator(session).driver
    assert driver.interpreter.extractor is not None
    assert driver.executor.extractor is not None


def test_the_extractor_dead_letters_through_the_repository(session):
    """A refused fetch has to become a visible dead letter, not a log line."""
    from ley_khaa.persistence.dead_letter_repository import DeadLetterRepository

    extractor = app_module.build_vision_extractor(session)
    extractor.dead_letter(source="vision", kind="inbound", reason="refused", payload={"name": "a.png"})

    rows = DeadLetterRepository(session).list()
    assert len(rows) == 1 and rows[0].source == "vision"
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_vision_wiring.py -q
```

Expected: FAIL — `TypeError: TaskDriver.__init__() got an unexpected keyword argument 'extractor'`.

- [ ] **Step 3: Take the argument in `TaskDriver`**

In `backend/ley_khaa/orchestrator/driver.py`, add `extractor=None` to the keyword-only arguments and
pass it to both consumers:

```python
        self.interpreter = Interpreter(llm, messages, extractor=extractor)
        self.executor = ExecutionRunner(
            llm=llm, messages=messages, workflows=workflows, extractor=extractor
        )
```

**Both**, not one. An extractor given only to the interpreter produces a system that understands an
image and then cannot compute on it — which looks like it works right up until a task needs the data.

- [ ] **Step 4: Build it in `api/app.py`**

Add the imports and the builder beside `build_orchestrator`:

```python
from ..persistence.image_extraction_repository import ImageExtractionRepository
from ..vision.extractor import VisionExtractor
from ..vision.fetcher import ImageFetcher
```

```python
def build_vision_extractor(session: Session) -> VisionExtractor:
    """The extractor for one unit of work.

    Its dead_letter writes on its OWN session rather than this one: a drop is
    recorded even when the caller's transaction is about to roll back, which is
    the same discipline _record_dead_letter follows for the adapters.
    """
    return VisionExtractor(
        llm=build_llm(settings.llm_backend),
        extractions=ImageExtractionRepository(session),
        fetcher=ImageFetcher(
            allowed_hosts=channel_set(settings.image_hosts),
            max_bytes=settings.image_max_bytes,
            # The bot token, so a Slack url_private can be resolved. The fetcher
            # attaches it to Slack hosts only.
            slack_token=settings.slack_bot_token,
        ),
        dead_letter=_record_dead_letter,
        enabled=settings.vision_enabled,
    )
```

`channel_set` is already imported in `app.py` if Phase 6 left it there; if not, add
`from ..adapters.base import channel_set`.

Then, in `build_orchestrator`, pass it down:

```python
        projects=ProjectRepository(session),
        notifier=current_notifier(),
        extractor=build_vision_extractor(session),
    )
```

`Orchestrator.__init__` must accept `extractor=None` and forward it to its `TaskDriver`.

- [ ] **Step 5: Run the tests and watch them pass**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_vision_wiring.py -q
```

Expected: PASS (8 passed).

- [ ] **Step 6: Mutation-test EVERY wiring line**

This is the task's whole point. Each must fail its named test:

1. Drop `extractor=extractor` from the `Interpreter(...)` call →
   `test_the_driver_hands_the_extractor_to_the_interpreter` must fail.
2. Drop it from the `ExecutionRunner(...)` call →
   `test_the_driver_hands_the_extractor_to_the_executor` must fail.
3. Drop `extractor=build_vision_extractor(session)` from `build_orchestrator` →
   `test_build_orchestrator_gives_its_driver_an_extractor` must fail.
4. Pass `fetcher=None` in `build_vision_extractor` →
   `test_build_vision_extractor_wires_a_real_fetcher` must fail.
5. Pass `slack_token=""` → `test_the_fetcher_is_given_the_slack_token_from_settings` must fail.
6. Pass `enabled=True` unconditionally →
   `test_build_vision_extractor_respects_the_off_switch` must fail.

**Report the actual outcome of each.** If any does not fail, that line is unpinned and needs a
better test — that is exactly the defect phase 6's review found six times.

- [ ] **Step 7: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/orchestrator/driver.py backend/ley_khaa/orchestrator/orchestrator.py backend/ley_khaa/api/app.py backend/tests/test_vision_wiring.py
git commit -m "$(cat <<'EOF'
feat(startup): give the driver a vision extractor

Both consumers, not one: an extractor handed only to the interpreter produces a
system that understands an image and then cannot compute on it, which looks
like it works until a task needs the data.

Every wiring line has a test that fails without it. Phase 6's whole-branch
review mutated four equivalent lines with the entire suite staying green — a
system that connects, logs its allowlist, and silently does nothing.
EOF
)"
```

---

## Task 11: The claim, offline, end to end

**Files:**
- Test: `backend/tests/test_vision_loop.py`

**Interfaces:** consumes everything. Produces no new code.

**This is §7 of the spec in executable form.** If a test here fails, the defect is in an earlier
task — fix it there, add the regression test there, and say so in your report. Do not weaken an
assertion in this file to make it pass.

- [ ] **Step 1: Write the test file**

Create `backend/tests/test_vision_loop.py`:

```python
"""Phase 7's claim, offline: no network, no key, real everything else."""
import base64
import json

from ley_khaa.domain.models import Attachment, Message
from ley_khaa.executor.workspace import MANIFEST_NAME
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.persistence.image_extraction_repository import (
    ImageExtractionRepository,
    sha256_of,
)
from ley_khaa.persistence.message_repository import MessageRepository
from ley_khaa.vision.contract import VisionExtraction
from ley_khaa.vision.extractor import VisionExtractor

PNG = b"\x89PNG\r\n\x1a\nholdings"
B64 = base64.standard_b64encode(PNG).decode()


class _CountingLLM:
    """The REAL offline client, wrapped in a counter."""

    def __init__(self, result=None):
        self.inner = HeuristicLLM()
        self.result = result
        self.calls = []
        self.name = "heuristic" if result is None else "anthropic"

    def extract_image(self, **kwargs):
        self.calls.append(kwargs)
        return self.result if self.result is not None else self.inner.extract_image(**kwargs)

    def __getattr__(self, item):
        return getattr(self.inner, item)


def _image_message(session, name="holdings.png"):
    return MessageRepository(session).add(
        Message(
            source="dashboard", client="me", conversation_id="c1", author="ana",
            text="compare the holdings against the portfolio",
            attachments=[Attachment(kind="image", name=name, content=B64)],
        )
    )


def test_an_image_is_read_once_and_only_once(session):
    """The reproducibility claim. Asserted with a counter around the real
    client, and re-asserted inside the test so it cannot degrade into 'two
    calls happened to agree'."""
    llm = _CountingLLM(VisionExtraction(kind="table", content="t,q\nAAA,10", summary="holdings"))
    extractor = VisionExtractor(
        llm=llm, extractions=ImageExtractionRepository(session), fetcher=None
    )
    attachment = {"kind": "image", "name": "holdings.png", "content": B64}

    first = extractor.extract(attachment)
    assert len(llm.calls) == 1

    second = extractor.extract(attachment)

    assert len(llm.calls) == 1, "the second read must come from the checkpoint"
    assert second.content == first.content
    assert second.image_sha256 == sha256_of(PNG)


def test_the_checkpoint_survives_a_fresh_extractor(session):
    """A re-drive builds a new extractor on a new session. The checkpoint is
    the DB row, not in-process state."""
    result = VisionExtraction(kind="table", content="t,q\nAAA,10", summary="holdings")
    attachment = {"kind": "image", "name": "holdings.png", "content": B64}

    first_llm = _CountingLLM(result)
    VisionExtractor(
        llm=first_llm, extractions=ImageExtractionRepository(session), fetcher=None
    ).extract(attachment)

    second_llm = _CountingLLM(result)
    row = VisionExtractor(
        llm=second_llm, extractions=ImageExtractionRepository(session), fetcher=None
    ).extract(attachment)

    assert second_llm.calls == [], "a new extractor must still hit the stored checkpoint"
    assert row.content == "t,q\nAAA,10"


def test_offline_an_image_is_carried_not_read_and_the_task_still_completes(session, client):
    """The zero-account invariant: no key, and the demo still works."""
    resp = client.post(
        "/messages",
        json={
            "text": "compare the holdings against the portfolio and send it as a csv",
            "attachments": [{"kind": "image", "name": "holdings.png", "content": B64}],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["task_ids"], "an image attachment must not stop a task forming"


def test_the_manifest_credits_who_actually_read_the_image(session):
    """Offline, the manifest must say heuristic — never claude-opus-5."""
    llm = _CountingLLM()  # no result -> the real offline stand-in
    extractor = VisionExtractor(
        llm=llm, extractions=ImageExtractionRepository(session), fetcher=None
    )

    row = extractor.extract({"kind": "image", "name": "holdings.png", "content": B64})

    assert row.model == "heuristic"
    assert row.content == ""


def test_the_whole_offline_path_opens_no_socket(session, monkeypatch):
    """'Offline' enforced rather than claimed."""
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("the offline vision path opened a socket")

    monkeypatch.setattr(socket.socket, "connect", forbidden)

    extractor = VisionExtractor(
        llm=_CountingLLM(), extractions=ImageExtractionRepository(session), fetcher=None
    )
    extractor.extract({"kind": "image", "name": "holdings.png", "content": B64})
```

- [ ] **Step 2: Run it**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_vision_loop.py -v
```

Expected: PASS (5 passed), **0 skipped**. If the `client` fixture is not available in this file's
scope, import it the way `tests/test_api.py` does rather than inventing a new one.

- [ ] **Step 3: Mutation-test the loop from outside**

Mutate in the module each belongs to, restore from a saved copy after each:

1. In `extractor.py`, drop the cache lookup → `test_an_image_is_read_once_and_only_once` and
   `test_the_checkpoint_survives_a_fresh_extractor` must both fail.
2. In `extractor.py`, hardcode `model="claude-opus-5"` in `_store` →
   `test_the_manifest_credits_who_actually_read_the_image` must fail.
3. In `heuristic.py`, make `extract_image` raise instead of returning the unread record →
   `test_offline_an_image_is_carried_not_read_and_the_task_still_completes` must fail.

**Report the actual outcome of each.**

- [ ] **Step 4: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/tests/test_vision_loop.py
git commit -m "$(cat <<'EOF'
test(vision): prove the phase's claim offline, end to end

An image is read once and only once, the checkpoint survives a fresh extractor
on a new session, an offline run carries the image without reading it and still
completes, the manifest credits heuristic rather than claude-opus-5, and a
socket guard makes "offline" enforced rather than claimed.

The call counter wraps the REAL offline client: a canned fake would let a broken
cache pass by agreeing with itself.
EOF
)"
```

---

## Task 12: Documentation and the honest limits

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `docs/GETTING_STARTED.md`
- Modify: `docs/superpowers/specs/2026-08-28-phase-5-backlog.md`

**Interfaces:** none. Ships no code.

**The rule that scopes it: fix statements that are FALSE.** Two are created by this phase.

- [ ] **Step 1: Correct what this phase falsified**

Search for and fix every one:

```bash
grep -rn "not built yet\|Vision intake\|vision" README.md docs/GETTING_STARTED.md | head -20
```

- `docs/GETTING_STARTED.md` §9 "What is not built yet" lists **Vision intake**. Remove that bullet —
  only the Ollama fallback remains of the §11 items.
- Any docstring or comment saying vision "is not built": Task 9 already fixed `_TEXTUAL`'s; confirm
  with `grep -rn "vision" backend/ley_khaa --include='*.py' | grep -i "not built\|later\|phase 7"`
  and correct whatever else it finds.

- [ ] **Step 2: Add the README section**

In `README.md`, after `### Channels`:

```markdown
### Images

Paste a screenshot into a channel or the dashboard and ley-khaa reads it — a table becomes data a
generated script can compute on, anything else becomes context the interpreter can reason about.

**The extraction is frozen.** An image is read once, keyed by a hash of its bytes, and every later
step — a repair attempt, a re-drive, a second task quoting the same screenshot — reuses that stored
result rather than re-reading the picture. That is what makes a run with an image in it
reproducible.

| Variable | Default | Meaning |
|---|---|---|
| `LEY_KHAA_VISION` | `on` | `off` carries images without reading them |
| `LEY_KHAA_IMAGE_HOSTS` | Slack + Discord CDNs | exact hostnames an image may be fetched from |
| `LEY_KHAA_IMAGE_MAX_BYTES` | `5242880` | hard cap on a fetched image |

**Limits, stated plainly.** With no `ANTHROPIC_API_KEY` an image is carried but not read, and the
task proceeds on the text alone — the bundle manifest records that no extraction happened. There is
no re-extraction: freezing is what makes a re-run reproducible, so if a table is misread the
checkpoint stays wrong until its row is cleared. Images are never stored, only their extraction.
```

- [ ] **Step 3: Add the CHANGELOG entry**

In `CHANGELOG.md`, under `## [Unreleased]`:

```markdown
## [0.8.0] — 2026-09-01

### Added
- **Vision intake (§5.2, §11).** A pasted image is read through Claude vision and frozen as a
  reproducible checkpoint. `Stage.VISION_EXTRACTION` has existed in the model router since 0.3.0
  and nothing called it; it now routes a real call.
- One extraction serves two consumers: the interpreter gets a one-line summary so the request is
  understood, and the resolver binds the extracted content as a script input under
  `inputs/extracted_<stem>.csv`, with the manifest attesting `source: "vision"`, the image's hash
  and the model that read it.
- `ImageFetcher` with an explicit boundary — https only, exact host allowlist, **the Slack bot
  token attached to Slack hosts and nowhere else**, no redirects, and a size cap enforced on the
  body rather than on the server-supplied `Content-Length`.
- `LLMClient.extract_image`, satisfied by all three implementations, so the offline stand-in stays
  deterministic and CI never reaches the network.

### Known limits
- An image with no vision backend is carried, not read; the task proceeds on its text and the
  manifest records that no extraction happened.
- **No re-extraction.** If vision misreads a table the frozen checkpoint stays wrong until its row
  is deleted — freezing is what makes a re-run reproducible.
- Images are not stored, only their extraction, so an image whose URL has expired cannot be re-read.
- The offline fallback planned for 0.9.0 is text-only, so **vision will not work on the offline
  path**. A local vision-capable model is roadmap.
```

- [ ] **Step 4: Update GETTING_STARTED**

Add a short section after §5.5 describing pasting a screenshot into an allowlisted channel, and in
§9 leave only the Ollama fallback plus the limits above.

- [ ] **Step 5: Verify every claim you just wrote**

Run each and confirm the docs match:

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
grep -rn "not built" ../docs/GETTING_STARTED.md          # must not mention vision
grep -c "LEY_KHAA_VISION" ../docker-compose.yml          # must be 1
for v in LEY_KHAA_VISION LEY_KHAA_IMAGE_HOSTS LEY_KHAA_IMAGE_MAX_BYTES; do
  grep -q "$v" ley_khaa/config.py && echo "  $v OK" || echo "  $v *** DOCUMENTED BUT NOT READ ***"
done
```

**Every variable the README documents must exist in `config.py`.** A config table naming a variable
nothing reads is the false-statement class in its quietest form.

- [ ] **Step 6: Commit**

```bash
git add README.md CHANGELOG.md docs/GETTING_STARTED.md
git commit -m "$(cat <<'EOF'
docs: document vision intake and its honest limits

Removes "Vision intake" from GETTING_STARTED's "not built yet" — the Ollama
fallback is now the only §11 item left. States the three limits plainly rather
than leaving them to be discovered: an image is carried-not-read with no API
key, there is no re-extraction because freezing is what makes a re-run
reproducible, and 0.9.0's offline fallback will be text-only.
EOF
)"
```

---

## Final verification, before the whole-branch review

- [ ] `cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q` → **0 failures, 0 skipped, 0 warnings.**
- [ ] `TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -m docker -q` → 9 passed, 0 skipped.
- [ ] `TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_migrations.py -q` → drift guard green.
- [ ] `cd frontend && npm test && npm run typecheck` → green, tsc silent.
- [ ] `git grep -n "xoxb\|xapp" -- backend/ley_khaa` → only the redaction regexes.
- [ ] **Prove the token boundary by hand**: the fetcher's `Authorization` header appears only for
      `files.slack.com`. Name the test that proves it in the PR body.
- [ ] Every §7 line of the spec has a test that would fail if it stopped being true. Walk the list
      and name the test for each in the PR body.
- [ ] **Whole-branch review on Opus.** Four phases running, it has found what per-task reviews
      structurally cannot. Expect it to find something.

---

## Self-review of this plan

**Spec coverage.** §3.1 contract → Task 2. §3.2 extractor + cache → Tasks 2, 6. §3.3 fetcher and its
four rules → Task 5. §3.4 `LLMClient` extension → Tasks 3 (stand-ins) and 4 (real). §3.5 both
consumers → Tasks 8 (interpreter) and 9 (resolver). §3.6 degradation → Tasks 3, 6, and the offline
tests in 11. §4 data model → Task 1. §5 configuration → Task 7. §6 testing → every task's mutation
step plus Task 11. §7 DoD → Task 11 and the final verification. §8 known limits → Task 12.

**One defect the pre-scan caught before it was written:** the plan's first draft used
`ModelRouter().model_for(...)`. There is no `ModelRouter` class — `model_for` is a module-level
function — so every test in Task 3 would have failed at import. This is the same root cause as
Phase 3's eight plan defects: reference code written before checking the interface is wrong wherever
it guesses.

**Two findings that removed work rather than adding it.** `runner.py` already writes
`{"name", "file", "source", "sha256"}` per input, so `source: "vision"` needs no manifest change —
a task disappeared. And `workspace.write_inputs` writes `item.content` to `inputs/item.filename`
with no type knowledge, so a vision checkpoint lands correctly with zero workspace changes.

**A trap worth naming for the executor.** `ResolvedInput.sha256` hashes the extracted CONTENT, not
the image. The spec's DoD asks the manifest to attest the IMAGE's hash and the producing model,
which is why Task 9 adds two fields rather than reusing `sha256`. Reading these as the same thing
would produce a manifest that looks complete and attests the wrong artifact.

**Type consistency.** `VisionExtraction(kind, content, summary)` is constructed in Tasks 2, 3, 4, 6,
11 with those names. `extract_image(*, choice, system, user, image, media_type, output_format)` is
identical across the protocol and all three implementations. `VisionExtractor.extract(attachment)`
returns `ImageExtractionRow` at every call site (Tasks 8, 9, 11). `ImageFetcher.fetch(url)` returns
`(bytes, str)` in Tasks 5 and 6.

**Placeholder scan:** every step carries its actual content — no "add error handling", no "similar
to Task N", no test named without its body.
