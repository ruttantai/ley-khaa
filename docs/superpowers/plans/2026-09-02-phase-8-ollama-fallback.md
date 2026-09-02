# Phase 8 — Ollama offline fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a keyless clone a real local model instead of the regex stand-in, by adding a third implementation of the existing `LLMClient` protocol that talks to a local Ollama daemon.

**Architecture:** One new file, `llm/ollama.py`, holding `OllamaLLM`. It is generic where `HeuristicLLM` is hand-written: Ollama accepts a JSON schema as its `format` parameter, and every output type in this codebase is a Pydantic model, so one `parse` covers every stage. `build_llm` gains an `ollama` branch that probes once at startup and falls back loudly to the heuristic. The protocol, `AnthropicLLM` and `HeuristicLLM` are untouched.

**Tech Stack:** Python 3.12, Pydantic v2, `ollama` 0.6.2, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-phase-8-ollama-fallback-design.md` — read it first; it is the binding authority and this plan argues from it.

## Global Constraints

- **Python is the worktree-local venv**: `../.venv/bin/python` from `backend/`. The repo-root `.venv` is installed editable against the MAIN checkout — using it silently tests the wrong code.
- **Backend tests**: `cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q`. `mkdir -p "$HOME/tmp"` first — this Mac runs Docker via Colima, which mounts only `$HOME`; without `TMPDIR` the 9 `[docker]` params fail misleadingly.
- **The bar is 0 failures, 0 skipped, 0 warnings.** A skip is a retired assertion, not a pass.
- **No test may reach the network, and none may reach `localhost:11434`.** `conftest` pins `LEY_KHAA_LLM=heuristic`; every `OllamaLLM` in a test takes an injected client double.
- **`LLMClient.name` records who ACTUALLY did the work.** An Ollama run must never be creditable to `claude-opus-5`, and a fallback to the heuristic must report `heuristic`.
- **Every new setting must be read falsy-safe** — `os.getenv(NAME) or default`, never `os.getenv(NAME, default)`. Compose passes `${VAR:-}`, which SETS the variable to `""`, so the two-argument form returns `""` and the default never fires. That exact bug crashed the backend at import in Phase 7.
- **The Anthropic path must end byte-for-byte unchanged.** No edit to `AnthropicLLM`, `HeuristicLLM`, or the `LLMClient` protocol.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/ley_khaa/llm/ollama.py` | `OllamaLLM` — the third `LLMClient` implementation |
| `backend/ley_khaa/llm/factory.py` | the `ollama` branch: probe, loud fallback |
| `backend/ley_khaa/config.py` | three new settings |
| `backend/pyproject.toml` | the pinned `ollama` dependency |
| `backend/tests/test_ollama_client.py` | `OllamaLLM` behaviour |
| `backend/tests/test_ollama_selection.py` | `build_llm` probing and fallback |
| `docker-compose.yml`, `README.md`, `docs/GETTING_STARTED.md`, `CHANGELOG.md`, the backlog | docs and wiring |

---

## VERIFIED API FACTS — I checked these against the installed `ollama` 0.6.2, do not re-derive them

- `ollama.Client(host: str | None = None, **kwargs)`.
- `Client.chat(model=..., messages=..., *, format=..., options=..., ...)` — `format` and `options` are both real keyword parameters.
- `Client.list() -> ListResponse`, which has `.models`, a list whose entries carry `.model` (the model's name string).
- **A dead daemon raises `builtins.ConnectionError`** — NOT `ollama.RequestError` and NOT `ollama.ResponseError`. Catching only the `ollama.*` types means the fallback never fires and the app crashes at startup. The module defines exactly two exception types, `RequestError` and `ResponseError`; catch all three.
- **`ChatResponse.message.content` is `Optional[str]`.** It can be `None`. `model_validate_json(None)` raises a confusing `TypeError`, so guard it explicitly — this is the same defect class as Phase 7's `parsed_output is None` Critical.

---

## Task 1: Configuration and the pinned dependency

**Files:**
- Modify: `backend/ley_khaa/config.py`
- Modify: `backend/pyproject.toml`
- Test: `backend/tests/test_ollama_config.py` (create)

**Interfaces:**
- Produces: `settings.ollama_model: str`, `settings.ollama_host: str`. (`settings.llm_backend` already exists and needs no change — it is a free-form string that `build_llm` switches on.)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_ollama_config.py`:

```python
import importlib

from ley_khaa import config as config_module


def _reload():
    return importlib.reload(config_module).Settings()


def test_the_defaults_are_a_local_daemon_and_a_named_model():
    s = config_module.Settings()
    assert s.ollama_model == "qwen2.5"
    assert s.ollama_host == "http://localhost:11434"


def test_an_empty_model_env_var_falls_back_to_the_default(monkeypatch):
    """compose passes ${VAR:-}, which SETS the variable to "". The two-argument
    os.getenv form would return "" here and the default would never fire."""
    monkeypatch.setenv("LEY_KHAA_OLLAMA_MODEL", "")
    try:
        assert _reload().ollama_model == "qwen2.5"
    finally:
        monkeypatch.delenv("LEY_KHAA_OLLAMA_MODEL", raising=False)
        importlib.reload(config_module)


def test_an_empty_host_env_var_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("LEY_KHAA_OLLAMA_HOST", "")
    try:
        assert _reload().ollama_host == "http://localhost:11434"
    finally:
        monkeypatch.delenv("LEY_KHAA_OLLAMA_HOST", raising=False)
        importlib.reload(config_module)


def test_the_env_vars_are_actually_read(monkeypatch):
    monkeypatch.setenv("LEY_KHAA_OLLAMA_MODEL", "llama3.1")
    monkeypatch.setenv("LEY_KHAA_OLLAMA_HOST", "http://ollama:11434")
    try:
        s = _reload()
        assert s.ollama_model == "llama3.1"
        assert s.ollama_host == "http://ollama:11434"
    finally:
        monkeypatch.delenv("LEY_KHAA_OLLAMA_MODEL", raising=False)
        monkeypatch.delenv("LEY_KHAA_OLLAMA_HOST", raising=False)
        importlib.reload(config_module)
```

- [ ] **Step 2: Run them and watch them FAIL**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_ollama_config.py -q
```

Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'ollama_model'`.

- [ ] **Step 3: Add the settings**

In `backend/ley_khaa/config.py`, after `image_max_bytes`:

```python
    # Ollama offline fallback (phase 8 design §4). The local model is used for
    # EVERY stage: the router's Claude model id is ignored, because someone
    # running Ollama typically has exactly one model pulled and requiring two
    # is friction on the very path this exists to serve.
    ollama_model: str = os.getenv("LEY_KHAA_OLLAMA_MODEL") or "qwen2.5"
    ollama_host: str = os.getenv("LEY_KHAA_OLLAMA_HOST") or "http://localhost:11434"
```

- [ ] **Step 4: Pin the dependency**

In `backend/pyproject.toml`, in `dependencies`, after `"requests==2.34.2",`:

```
    "ollama==0.6.2",
```

Then install it into the worktree venv:

```bash
cd backend && ../.venv/bin/python -m pip install -q -e ".[dev]"
```

- [ ] **Step 5: Run the tests and watch them PASS**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_ollama_config.py -q
```

Expected: 4 passed.

- [ ] **Step 6: Mutate, to prove the falsy-safe guard is pinned**

Change `os.getenv("LEY_KHAA_OLLAMA_MODEL") or "qwen2.5"` to
`os.getenv("LEY_KHAA_OLLAMA_MODEL", "qwen2.5")` and re-run.
Expected: `test_an_empty_model_env_var_falls_back_to_the_default` FAILS (`assert '' == 'qwen2.5'`).
**Record what you actually observed, then revert the mutation.**

- [ ] **Step 7: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/config.py backend/pyproject.toml backend/tests/test_ollama_config.py
git commit -m "feat(config): add the ollama backend settings"
```

---

## Task 2: `OllamaLLM` — the third implementation

**Files:**
- Create: `backend/ley_khaa/llm/ollama.py`
- Test: `backend/tests/test_ollama_client.py` (create)

**Interfaces:**
- Consumes: `ModelChoice` (`llm/router.py`), `VisionExtraction` (`vision/contract.py`), `settings` (Task 1).
- Produces: `OllamaLLM(model: str, host: str = "", client: Any | None = None)` with
  `name: str` (an INSTANCE attribute, `f"ollama:{model}"`),
  `parse(*, choice, system, user, output_format) -> T`, and
  `extract_image(*, choice, system, user, image, media_type, output_format) -> T`.

**Note `name` is an instance attribute here**, unlike `AnthropicLLM.name = "anthropic"` and
`HeuristicLLM.name = "heuristic"`, which are class attributes. The protocol declares `name: str` and
an instance attribute satisfies it. It has to be per-instance because it carries the model.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_ollama_client.py`:

```python
import pytest
from pydantic import BaseModel

from ley_khaa.llm.ollama import OllamaLLM
from ley_khaa.llm.router import Stage, model_for
from ley_khaa.vision.contract import VisionExtraction


class Answer(BaseModel):
    verdict: str
    score: float


class _Recorder:
    """Stands in for ollama.Client. Records the kwargs of every chat() call."""

    def __init__(self, content='{"verdict": "yes", "score": 0.5}'):
        self.calls: list[dict] = []
        self._content = content

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return type(
            "R", (), {"message": type("M", (), {"content": self._content})()}
        )()


def _llm(rec, model="qwen2.5"):
    return OllamaLLM(model=model, client=rec)


def test_the_output_schema_is_sent_as_the_format():
    """This is what makes one implementation cover every stage: the caller's
    pydantic model becomes the JSON schema Ollama is constrained to."""
    rec = _Recorder()
    _llm(rec).parse(
        choice=model_for(Stage.RELEVANCE_FILTER), system="s", user="u", output_format=Answer
    )
    assert rec.calls[0]["format"] == Answer.model_json_schema()


def test_the_response_is_validated_into_the_requested_type():
    rec = _Recorder()
    out = _llm(rec).parse(
        choice=model_for(Stage.RELEVANCE_FILTER), system="s", user="u", output_format=Answer
    )
    assert isinstance(out, Answer)
    assert out.verdict == "yes"
    assert out.score == 0.5


def test_the_routers_token_budget_is_honoured():
    """The router's max_tokens still means something locally: synthesis needs
    room for a whole program."""
    rec = _Recorder()
    choice = model_for(Stage.SYNTHESIS)
    _llm(rec).parse(choice=choice, system="s", user="u", output_format=Answer)
    assert rec.calls[0]["options"]["num_predict"] == choice.max_tokens
    assert choice.max_tokens == 16000


def test_the_routers_claude_model_id_is_ignored():
    """The local model comes from config; a Claude id must never be sent to a
    local daemon that has never heard of it."""
    rec = _Recorder()
    choice = model_for(Stage.INTERPRETER)
    _llm(rec, model="llama3.1").parse(
        choice=choice, system="s", user="u", output_format=Answer
    )
    assert rec.calls[0]["model"] == "llama3.1"
    assert "claude" not in rec.calls[0]["model"]


def test_the_system_and_user_prompts_are_separate_messages():
    rec = _Recorder()
    _llm(rec).parse(
        choice=model_for(Stage.INTERPRETER), system="be terse", user="the ask", output_format=Answer
    )
    assert rec.calls[0]["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "the ask"},
    ]


def test_the_name_records_the_actual_local_model():
    """`ollama` alone does not identify what produced a script."""
    assert _llm(_Recorder(), model="qwen2.5").name == "ollama:qwen2.5"
    assert _llm(_Recorder(), model="llama3.1").name == "ollama:llama3.1"


def test_an_empty_response_is_a_clear_error_not_a_confusing_one():
    """ChatResponse.message.content is Optional[str]. Passing None into
    model_validate_json raises an opaque TypeError; the same None-return shape
    was a Critical in phase 7."""
    rec = _Recorder(content=None)
    with pytest.raises(ValueError, match="empty response"):
        _llm(rec).parse(
            choice=model_for(Stage.RELEVANCE_FILTER), system="s", user="u", output_format=Answer
        )


def test_an_image_is_carried_not_read():
    """Ollama is text-only in v1 (spec §11). This returns the same shape as the
    heuristic stand-in, which phase 7's VisionExtractor already stores as a
    carried-not-read record — so no new degradation path is needed."""
    rec = _Recorder()
    out = _llm(rec).extract_image(
        choice=model_for(Stage.VISION_EXTRACTION),
        system="s",
        user="chart.png",
        image=b"\x89PNG_bytes",
        media_type="image/png",
        output_format=VisionExtraction,
    )
    assert out.content == ""
    assert out.kind == "text"
    assert "chart.png" in out.summary
    assert rec.calls == []  # no model call at all
```

- [ ] **Step 2: Run them and watch them FAIL**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_ollama_client.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'ley_khaa.llm.ollama'`.

- [ ] **Step 3: Write the implementation**

Create `backend/ley_khaa/llm/ollama.py`:

```python
from typing import Any, TypeVar

from pydantic import BaseModel

from .router import ModelChoice

T = TypeVar("T", bound=BaseModel)


class OllamaLLM:
    """A local model behind the same seam as Claude (phase 8 design §3.1).

    Generic where HeuristicLLM is hand-written: Ollama takes a JSON schema as
    its `format`, and every output type in this codebase is a pydantic model,
    so one `parse` covers every stage that exists and every one added later.
    """

    def __init__(self, model: str, host: str = "", client: Any | None = None) -> None:
        if client is None:
            import ollama

            client = ollama.Client(host=host or None)
        self._client = client
        self.model = model
        # An instance attribute, unlike the class attributes on AnthropicLLM
        # and HeuristicLLM: the manifest must name the model that actually did
        # the work, and "ollama" alone does not identify what produced a script.
        self.name = f"ollama:{model}"

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        # choice.model is deliberately unused: it names a Claude model, and a
        # local daemon has never heard of it. choice.max_tokens still applies —
        # synthesis needs room for a whole program.
        response = self._client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format=output_format.model_json_schema(),
            options={"num_predict": choice.max_tokens},
        )
        content = response.message.content
        if not content:
            # ChatResponse.message.content is Optional[str]. Feeding None to
            # model_validate_json raises an opaque TypeError far from the cause.
            raise ValueError(f"{self.name} returned an empty response")
        return output_format.model_validate_json(content)

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
        """Text-only, as spec §11 states. `image` is deliberately unread.

        Phase 7's VisionExtractor turns this empty extraction into a stored
        carried-not-read record and names the image in the manifest, so §7's
        "image steps error out gracefully rather than guess" is satisfied here
        with no new error type and no change to the extractor.
        """
        return output_format(
            kind="text",
            content="",
            summary=(
                f"{user or 'an image'} was attached but not read: "
                f"{self.name} is a text-only backend (set ANTHROPIC_API_KEY for vision)."
            ),
        )
```

- [ ] **Step 4: Run the tests and watch them PASS**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_ollama_client.py -q
```

Expected: 8 passed.

- [ ] **Step 5: Mutate each guard, and record what you actually observe**

Run each, note the real result, revert before the next:

1. Send `model=choice.model` instead of `self.model` →
   `test_the_routers_claude_model_id_is_ignored` must fail.
2. Drop the `if not content:` guard →
   `test_an_empty_response_is_a_clear_error_not_a_confusing_one` must fail.
3. Make `extract_image` call `self._client.chat(...)` before returning →
   `test_an_image_is_carried_not_read` must fail on `rec.calls == []`.
4. Hardcode `options={"num_predict": 4096}` →
   `test_the_routers_token_budget_is_honoured` must fail.

**If any mutation does NOT produce the predicted failure, say so plainly in your report** — a
prediction that does not hold is a finding about coverage, not something to paper over. Find an
alternate mutation that DOES discriminate, or state that the guard is unproven.

- [ ] **Step 6: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/llm/ollama.py backend/tests/test_ollama_client.py
git commit -m "feat(llm): add a local Ollama client behind the same seam"
```

---

## Task 3: Selection, the startup probe, and the loud fallback

**Files:**
- Modify: `backend/ley_khaa/llm/factory.py`
- Test: `backend/tests/test_ollama_selection.py` (create)

**Interfaces:**
- Consumes: `OllamaLLM` (Task 2), `settings.ollama_model` / `settings.ollama_host` (Task 1).
- Produces: `build_llm("ollama")` returning either `OllamaLLM` or, on any probe failure, `HeuristicLLM`.

**The probe distinguishes two failures on purpose.** "Daemon unreachable" and "model not pulled" have
different fixes, and a user who gets `run: ollama pull qwen2.5` is unstuck in seconds.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_ollama_selection.py`:

```python
import logging

import ollama
import pytest

from ley_khaa.llm import factory
from ley_khaa.llm.heuristic import HeuristicLLM
from ley_khaa.llm.ollama import OllamaLLM


class _Listing:
    def __init__(self, *names):
        self.models = [type("M", (), {"model": n})() for n in names]


class _Daemon:
    """Stands in for ollama.Client at probe time."""

    def __init__(self, listing=None, raises=None):
        self._listing = listing
        self._raises = raises
        self.list_calls = 0

    def list(self):
        self.list_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._listing


@pytest.fixture(autouse=True)
def _reset_warning():
    factory._warned_about_fallback = False
    yield
    factory._warned_about_fallback = False


def test_a_reachable_daemon_with_the_model_pulled_gives_an_ollama_client(monkeypatch):
    monkeypatch.setattr(factory, "_ollama_client", lambda host: _Daemon(_Listing("qwen2.5")))
    llm = factory.build_llm("ollama")
    assert isinstance(llm, OllamaLLM)
    assert llm.name == "ollama:qwen2.5"


def test_an_unreachable_daemon_falls_back_to_the_heuristic_loudly(monkeypatch, caplog):
    """A dead daemon raises builtins.ConnectionError — NOT an ollama.* type."""
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(raises=ConnectionError("refused"))
    )
    with caplog.at_level(logging.WARNING):
        llm = factory.build_llm("ollama")
    assert isinstance(llm, HeuristicLLM)
    assert llm.name == "heuristic"
    assert "not reachable" in caplog.text
    assert "11434" in caplog.text or "host" in caplog.text.lower()


def test_a_model_that_is_not_pulled_says_how_to_pull_it(monkeypatch, caplog):
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(_Listing("llama3.1", "mistral"))
    )
    with caplog.at_level(logging.WARNING):
        llm = factory.build_llm("ollama")
    assert isinstance(llm, HeuristicLLM)
    assert "ollama pull qwen2.5" in caplog.text


def test_an_ollama_response_error_also_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(
        factory,
        "_ollama_client",
        lambda host: _Daemon(raises=ollama.ResponseError("boom")),
    )
    with caplog.at_level(logging.WARNING):
        assert isinstance(factory.build_llm("ollama"), HeuristicLLM)


def test_the_fallback_warning_is_said_once_not_every_sweep(monkeypatch, caplog):
    """build_llm runs per request and per background sweep."""
    monkeypatch.setattr(
        factory, "_ollama_client", lambda host: _Daemon(raises=ConnectionError("refused"))
    )
    with caplog.at_level(logging.WARNING):
        factory.build_llm("ollama")
        factory.build_llm("ollama")
        factory.build_llm("ollama")
    assert caplog.text.count("not reachable") == 1


def test_the_other_backends_are_untouched(monkeypatch):
    assert isinstance(factory.build_llm("heuristic"), HeuristicLLM)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(factory.build_llm("anthropic"), HeuristicLLM)
```

- [ ] **Step 2: Run them and watch them FAIL**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_ollama_selection.py -q
```

Expected: FAIL — `AttributeError: module 'ley_khaa.llm.factory' has no attribute '_ollama_client'`.

- [ ] **Step 3: Write the implementation**

In `backend/ley_khaa/llm/factory.py`, add the import and the seam near the top:

```python
from ..config import settings
from .ollama import OllamaLLM
```

```python
def _ollama_client(host: str):
    """The one place the real client is constructed, so tests can replace it
    without ever opening a socket."""
    import ollama

    return ollama.Client(host=host or None)
```

Then, inside `build_llm`, before the `ANTHROPIC_API_KEY` check:

```python
    if backend == "ollama":
        return _build_ollama()
```

And add:

```python
def _build_ollama() -> LLMClient:
    """Probe once, at startup, and degrade loudly (phase 8 design §3.4).

    Two failures with different fixes are reported differently: a daemon that
    is not running, and a model that was never pulled.
    """
    model, host = settings.ollama_model, settings.ollama_host
    try:
        listing = _ollama_client(host).list()
        pulled = {m.model for m in listing.models}
    except (ConnectionError, ollama.RequestError, ollama.ResponseError) as exc:
        # A dead daemon raises builtins.ConnectionError, NOT an ollama.* type —
        # catching only the ollama ones means this never fires and the app dies
        # at startup instead of degrading.
        _fall_back(
            f"LEY_KHAA_LLM=ollama but the Ollama daemon is not reachable at {host} "
            f"({type(exc).__name__}) — falling back to HeuristicLLM, the offline regex "
            "stand-in. Start Ollama, or set LEY_KHAA_OLLAMA_HOST."
        )
        return HeuristicLLM()

    if not any(name == model or name.startswith(f"{model}:") for name in pulled):
        _fall_back(
            f"LEY_KHAA_LLM=ollama and the daemon is reachable at {host}, but the model "
            f"{model!r} is not pulled — falling back to HeuristicLLM, the offline regex "
            f"stand-in. Fix with: ollama pull {model}"
        )
        return HeuristicLLM()

    return OllamaLLM(model=model, host=host)


def _fall_back(message: str) -> None:
    """Say it once. build_llm runs per request and per background sweep."""
    global _warned_about_fallback
    if not _warned_about_fallback:
        _warned_about_fallback = True
        logger.warning(message)
```

Add `import ollama` at module top (it is a pinned dependency as of Task 1).

Note the pulled-model check accepts a bare name or a tagged one (`qwen2.5` matches `qwen2.5:latest`),
because that is how Ollama actually reports pulled models.

- [ ] **Step 4: Run the tests and watch them PASS**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_ollama_selection.py -q
```

Expected: 6 passed.

- [ ] **Step 5: Mutate, and record what you actually observe**

1. Narrow the except clause to `except (ollama.RequestError, ollama.ResponseError)` →
   `test_an_unreachable_daemon_falls_back_to_the_heuristic_loudly` must fail with an escaping
   `ConnectionError`. **This is the most important mutation in the phase** — it proves the
   catch-clause defect is pinned.
2. Drop the pulled-model check →
   `test_a_model_that_is_not_pulled_says_how_to_pull_it` must fail.
3. Remove the `_warned_about_fallback` guard →
   `test_the_fallback_warning_is_said_once_not_every_sweep` must fail with a count of 3.

- [ ] **Step 6: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/llm/factory.py backend/tests/test_ollama_selection.py
git commit -m "feat(llm): select ollama at startup and degrade loudly"
```

---

## Task 4: Docker, documentation, and the honest limits

**Files:**
- Modify: `docker-compose.yml`, `README.md`, `docs/GETTING_STARTED.md`, `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-08-28-phase-5-backlog.md`

**Interfaces:** none. Ships no code.

**The rule that scopes it: fix statements that are FALSE, and state the limits plainly.**

- [ ] **Step 1: Wire compose**

In `docker-compose.yml`, on the backend service, add the three env passthroughs beside the existing
ones and the host mapping:

```yaml
      # Ollama runs on the HOST, not in this container, so localhost inside the
      # container is the wrong machine. host.docker.internal is how a container
      # reaches the host; without it the probe fails and the backend silently
      # falls back to the regex stand-in.
      LEY_KHAA_OLLAMA_MODEL: ${LEY_KHAA_OLLAMA_MODEL:-}
      LEY_KHAA_OLLAMA_HOST: ${LEY_KHAA_OLLAMA_HOST:-http://host.docker.internal:11434}
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Match the surrounding style exactly — read the existing `LEY_KHAA_*` entries first.

- [ ] **Step 2: Correct what this phase falsified**

Run the search and fix every hit:

```bash
grep -rn "Ollama" README.md docs/GETTING_STARTED.md | head -20
```

- `docs/GETTING_STARTED.md` §9 "What is not built yet" lists **Ollama offline fallback. Not started.**
  That is now FALSE. Remove that bullet. §9 then has no items left — replace the list with a sentence
  saying every §11 item has shipped, keeping the section so the document's numbering is stable.
- Check the sentence "This is the remaining item in the v1 definition of done (§11 of the design
  spec)" that follows it, and rewrite it to match.

- [ ] **Step 3: Document the backend**

In `README.md`, after the `### Images` section, add `### Running without an API key` covering:
the three settings and their defaults; that the local model is used for every stage and the router's
tier is ignored; that the manifest names `ollama:<model>` so a bundle never credits Claude for local
work; that vision is text-only so an image is carried-not-read; and that an unreachable daemon or an
unpulled model degrades to the regex stand-in with a warning naming the cause.

Mirror it briefly in `docs/GETTING_STARTED.md` where the other run modes are described, including the
compose caveat from Step 1.

- [ ] **Step 4: CHANGELOG**

Under `## [Unreleased]`, add a `## [0.9.0] — 2026-09-02` entry in the shape of the `## [0.8.0]` entry
directly below it: `### Added` for the backend, `### Known limits` for text-only vision, no runtime
step-down, and output quality depending on the local model.

- [ ] **Step 5: File the backlog item**

**Read the backlog file first and use the next free number.** As of this writing the file ends at
`## 21.`, so the new item is `## 22.` — but verify rather than trusting this line. This repo has
shipped duplicate backlog numbers twice.

Add: **22. No runtime step-down between backends.** The backend is chosen once at startup, so a
Claude call that fails is not retried on Ollama and vice versa (design spec §7 asks for this). The
blocker is `LLMClient.name`: a per-call fallback makes the producer a property of the call, not the
client, so the manifest's "who actually did the work" contract needs rework first.

- [ ] **Step 6: Verify every claim, then commit**

Re-read each sentence you wrote and ask whether it is literally true of the code that now exists.

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
cd .. && git add -A && git commit -m "docs: document the ollama backend and its honest limits"
```

---

## Final verification, before the whole-branch review

- [ ] `cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q` → **0 failures, 0 skipped, 0 warnings.**
- [ ] `TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -m docker -q` → 9 passed, 0 skipped.
- [ ] `TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_migrations.py -q` → drift guard green.
- [ ] `cd frontend && npm test && npm run typecheck` → green, tsc silent.
- [ ] **Prove no test can reach a daemon**: `git grep -n "11434" -- backend/tests` returns only assertions about warning text, never a client construction.
- [ ] **Prove the Anthropic path is untouched**: `git diff main -- backend/ley_khaa/llm/client.py backend/ley_khaa/llm/heuristic.py` is empty.
- [ ] **Whole-branch review on Opus.** Five phases running, it has found what per-task reviews structurally cannot — including a defect that made an image permanently unreadable. Expect it to find something.

---

## Self-review of this plan

**Spec coverage.** §2 decision 1 (static only) → Task 3 plus the Task 4 backlog item. Decision 2 (one
model, tier ignored) → Task 2 Step 1 tests 3 and 4. Decision 3 (loud fallback) → Task 3. Decision 4
(text-only vision) → Task 2's `extract_image`. Decision 5 (`name`) → Task 2. §3.1 → Task 2. §3.2 →
Task 2. §3.3 → Task 2. §3.4 → Task 3. §3.5 (docker) → Task 4 Step 1. §4 config → Task 1. §5 testing
→ every task's mutation step. §6 DoD → the final verification list. §7 known limits → Task 4.
§8 out of scope → nothing implements it, and the backlog item records why.

**Four API facts were verified against the installed `ollama` 0.6.2 before this plan was written**,
because reference code written from memory is where the last two phases' defects came from. The two
that would have caused real bugs: a dead daemon raises `builtins.ConnectionError`, not an `ollama.*`
type, so a natural-looking `except ollama.RequestError` would never fire; and
`ChatResponse.message.content` is `Optional[str]`, the same `None`-return shape that was a Critical
in phase 7.

**Type consistency.** `OllamaLLM(model=..., host=..., client=...)` is constructed with those names in
Tasks 2 and 3. `name` is `f"ollama:{model}"` in both. `_ollama_client(host)` is defined in Task 3 and
monkeypatched by that task's tests under the same name. `parse` and `extract_image` keep the exact
signatures the protocol declares.

**Placeholder scan:** every step carries its actual content — no "add error handling", no "similar to
Task N", no test named without its body. Task 4's prose steps name the specific file, section and
claim to change rather than saying "update the docs".
