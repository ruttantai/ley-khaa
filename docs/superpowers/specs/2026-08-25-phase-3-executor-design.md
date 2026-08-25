# Phase 3 (v0.4.0) — Synthesis-First Executor, Sandbox, and the Reproducible Output Bundle

**Status:** approved 2026-08-25
**Implements:** §5.10 (synthesis-first executor + validator), §5.11 (reproducible Output Bundle),
§5.12 (document generation) of `2026-08-18-ley-khaa-design.md`.
**Explicitly deferred:** §5.6 (workflow registry fast path and promotion) → Phase 4 (v0.5.0).

## 1. Goal

Phase 2 left `TaskDriver._execute` and `_validate` as stubs that walk a task from `executing` to
`done` without doing any work. This phase replaces them with a real executor: a task's validated
`TaskSpec` is turned into inputs, a synthesized Python script, a sandboxed run, a validated
deliverable, and a persisted, re-runnable Output Bundle.

After this phase, `docker compose up` on a fresh clone with **no `ANTHROPIC_API_KEY`** produces a
real `.xlsx` file on disk, reachable from the dashboard, with the code that generated it beside it.

## 2. Decisions

Settled during the 2026-08-25 brainstorming session. Not open for re-litigation during execution.

1. **Scope is synthesis + sandbox + validator + bundle + Excel.** The §5.6 registry is deferred to
   Phase 4 (v0.5.0). The registry is a *cache of promoted, proven workflows*; it has nothing to cache until
   synthesis works and has produced scripts worth promoting. Designing its matcher against two seed
   examples rather than real promoted scripts would be premature.
2. **Inputs resolve from attachments first, then a synthetic catalog.** Message `Attachment`s are
   already plumbed end-to-end and carry literal CSV for `kind=table`. A Faker-seeded catalog of
   synthetic securities datasets covers spec input names that no attachment provides — which is what
   the golden `messy_universe_check` conversation ("compare the Bloomberg universe against FactSet")
   needs in order to run untouched.
3. **The sandbox is a seam with two implementations.** `DockerSandbox` is the default and is what
   §5.10 promises. `SubprocessSandbox` takes over when no daemon is reachable, so the SQLite +
   uvicorn dev loop keeps working. This mirrors the existing `LLMClient` / `HeuristicLLM` precedent.
   The manifest records which one actually ran, so a bundle never overstates its own isolation.
4. **The offline path produces a real deliverable.** `HeuristicLLM` returns genuine Python source for
   the two demo shapes. It runs in the real sandbox against real resolved inputs and writes a real
   spreadsheet. The README states plainly that offline synthesis is canned rather than generated.
   These canned scripts are the honest precursor to the Phase 4 registry.
5. **One repair attempt, then escalate.** A crash or a failed validation feeds the traceback back to
   the model for exactly one re-synthesis. This mirrors the interpreter's existing
   retry-once-then-escalate pattern. Every attempt is kept in the bundle.
6. **The state machine does not change.** The repair loop lives inside `_execute`, so no new edges
   and no execute/validate ping-pong.
7. **Document generation is not a subsystem.** The synthesized script writes the `.xlsx` itself using
   `openpyxl` inside the sandbox. §5.12 is therefore a line in the synthesis prompt plus two
   libraries in the sandbox image — and it is what keeps `generator/` genuinely re-runnable: re-running
   it reproduces the spreadsheet, not merely the underlying data.

## 3. Architecture

`TaskDriver._execute` delegates to a single `ExecutionRunner.run(task, spec)`:

```
resolve inputs → build workspace → synthesize → sandbox run → validate
                                        ↑______ repair once ______|
```

### 3.1 New package: `backend/ley_khaa/executor/`

| Module | Responsibility |
|---|---|
| `catalog.py` | Faker-seeded synthetic securities datasets, fixed seed, deterministic |
| `resolver.py` | `spec.inputs[]` names → `ResolvedInput`; attachment first, catalog second, else unresolved |
| `workspace.py` | Creates `task-<id>/{deliverable,generator,inputs}`; writes `manifest.json` |
| `synthesizer.py` | `(spec, input previews) → SynthesizedScript` via `LLMClient` and `Stage.SYNTHESIS` |
| `sandbox.py` | `SandboxRunner` protocol, `DockerSandbox`, `SubprocessSandbox`, selection logic |
| `validator.py` | Pure `validate(spec, workspace, result) → Verdict` |
| `runner.py` | `ExecutionRunner` — owns the loop above, writes the manifest, returns the outcome |

### 3.2 Interfaces

```python
@dataclass(frozen=True)
class ResolvedInput:
    name: str          # the spec input name this satisfies
    path: Path         # written into inputs/
    source: str        # "attachment" | "catalog"
    sha256: str

class SynthesizedScript(BaseModel):
    reasoning: str     # why the script is shaped this way; kept in the manifest
    source: str        # the Python module body

@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

class SandboxRunner(Protocol):
    name: str          # "docker" | "subprocess"
    def run(self, *, script: Path, workspace: Path, timeout_s: int) -> SandboxResult: ...

@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str                 # plain English, shown to the human
    checks: dict[str, bool]     # per-rule results, kept in the manifest
```

### 3.3 Model routing

`Stage.SYNTHESIS` is added to `llm/router.py`, routed to `OPUS` at both `routine` and `hard`
complexity, with a `max_tokens` of `16000` — synthesis emits far more tokens than any existing
stage. Opus supports adaptive thinking, so the existing `supports_thinking` flag handles it and no
call site needs to guess.

### 3.4 Persistence

Alembic revision `0003_executor` adds two columns to `tasks`:

- `workspace_path: str | None` — the bundle root, surfaced by the dashboard
- `execution_verdict: JSON | None` — the serialized `Verdict` that `_validate` acts on

`_execute` runs the attempt loop and persists the verdict; `_validate` reads it and transitions to
`DONE` or `NEEDS_CLARIFICATION`. This is a deliberate trade: `_validate` becomes a thin step that
records a decision made immediately before it, in exchange for leaving the state machine untouched
and making an execute/validate loop structurally impossible.

## 4. The sandbox

### 4.1 `DockerSandbox` (default)

Image `ley-khaa-sandbox`, built from `python:3.12-slim` with `pandas`, `numpy`, `openpyxl`, and
`python-docx` — nothing else. Every run:

- `--network none` — no outbound network, the §5.10 guarantee
- `--read-only` rootfs, with a tmpfs mounted at `/tmp`
- non-root user
- `--memory 512m`, `--cpus 1`, `--pids-limit 64`
- wall-clock timeout enforced by the caller, with `docker kill` on expiry
- `inputs/` mounted read-only; `deliverable/` the only writable mount

**Docker-out-of-docker.** Under compose the backend is itself containerized. It mounts
`/var/run/docker.sock` and spawns *sibling* containers on the host daemon, so a bind mount of the
backend's own container path would resolve to the wrong location on the host. `task-workspaces` is
therefore a **named volume mounted at the same path in both** the backend service and the sandbox
container, making the path valid on either side. Outside compose, `LEY_KHAA_WORKSPACE_ROOT` is an
ordinary host directory and the problem does not arise.

### 4.2 `SubprocessSandbox` (fallback)

Temp working directory, `RLIMIT_AS` and `RLIMIT_CPU`, `subprocess.run(timeout=…)`, and a **scrubbed
environment** so a synthesized script cannot read `ANTHROPIC_API_KEY` or any cloud credentials out
of the process environment. It cannot block network access on macOS. That is precisely why it emits
one loud warning per process (matching the LLM offline-fallback pattern) and stamps
`"sandbox": "subprocess"` into the manifest.

Selection: use Docker when a daemon responds; otherwise fall back. `LEY_KHAA_SANDBOX` may pin either
implementation explicitly.

## 5. The Output Bundle (§5.11)

```
task-workspaces/task-<id>/
├── deliverable/  missing_securities.xlsx
├── generator/    attempt_1.py  attempt_2.py  run.sh
├── inputs/       bloomberg_universe.csv  factset_universe.csv
└── manifest.json
```

`manifest.json` records: `task_id`, `created_at`, lane (`synthesis`), the sandbox actually used, the
model chosen per stage, the seed, a snapshot of the `TaskSpec`, per-attempt exit codes / durations /
verdicts, the synthesizer's `reasoning`, and `sha256` for every input and every deliverable.

Failed attempts stay in `generator/`. A bundle that hides its first failure is not an audit trail.

### 5.1 API

- `GET /tasks/{id}/bundle` — manifest plus a file listing
- `GET /tasks/{id}/bundle/file?path=` — file contents for the generator code viewer, **guarded
  against path traversal**: the resolved path must stay inside the task's own workspace root
- `GET /tasks/{id}/bundle/download` — the whole bundle as a zip

### 5.2 Dashboard

`TaskDetail.tsx` gains a Bundle panel: a manifest summary (lane, sandbox, model, attempts), the
generator source, and a download link for the deliverable and the full bundle.

## 6. Error handling

The organizing question is *whose problem the failure is*.

| Failure | Handling |
|---|---|
| An input name resolves to nothing | `needs_clarification` **before any model call** — no tokens spent |
| Synthesis returns empty or unusable source | A failed attempt → repair once → escalate |
| Script crashes, times out, or is OOM-killed | Repair once with truncated traceback + stderr → escalate |
| Validation fails | Repair once → escalate |
| Sandbox infrastructure failure (daemon dies mid-run) | `FAILED`, **not** `needs_clarification` — a dead daemon is not a question a human can answer |
| Path traversal attempt on the bundle file API | `400` |

Escalation text put to the human stays plain English. The traceback lives in the bundle and the
dashboard, never in the question itself.

Validator rules: a deliverable exists; it is non-empty; its extension matches `spec.output_format`;
tabular output has at least one row; any columns named in the spec are present.

## 7. Testing

- **Sandbox contract test.** One parametrized suite that both `DockerSandbox` and
  `SubprocessSandbox` must pass — exit codes, stdout/stderr capture, timeout kill, deliverable
  written, environment scrubbed — so the fallback cannot silently drift from the real thing. Docker
  cases carry `@pytest.mark.docker`, which skips with no daemon and runs in CI.
- **`FakeSandbox`** for runner-level tests, keeping the suite's sub-second runtime.
- **Reproducibility test** — the claim the entire bundle rests on. Re-run `generator/` from
  `inputs/` and assert an identical deliverable. `.xlsx` is a zip that embeds timestamps, so raw
  byte-hashing is flaky: the test compares parsed cell values for spreadsheets and raw bytes for
  CSV/JSON, and the manifest records that distinction rather than implying a byte-identical claim it
  cannot support.
- **Offline end-to-end.** The golden conversation reaches `done` with a real `.xlsx` on disk under
  `LEY_KHAA_LLM=heuristic`, with no network and no API key.
- Table-driven tests for the validator and the resolver.
- `TaskDetail` bundle-panel tests with mocked fetch.

Existing global constraints continue to hold: tests never make network calls, `AnthropicLLM` is
never constructed under `backend/tests/`, and all data is synthetic.

## 8. Out of scope

- The §5.6 workflow registry, its matcher, and promotion (Phase 4, v0.5.0)
- Per-project async concurrency (Phase 4, v0.5.0)
- Amendment detection and real Slack/Discord adapters (Phase 4, v0.5.0)
- Multi-file repo generation, and media/image *output* generation (roadmap)
- Vision extraction of pasted images (the bundle reserves `inputs/` for the frozen checkpoint, but
  no vision stage is built in this phase)

## 9. Definition of done

- `docker compose up` on a fresh clone with no `ANTHROPIC_API_KEY` walks the golden conversation to
  `done` and writes a real `.xlsx` into a bundle.
- The dashboard shows the bundle, renders the generator source, and downloads the deliverable.
- The reproducibility test passes.
- Backend and frontend suites are green, with no new warnings.
- Released as tag `v0.4.0`.
