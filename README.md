# ley-khaa (เลขา)

> Your AI secretary — turns the way people actually talk into finished, validated work.

**ley-khaa** is a personal automation layer for developers. It sits *in front of* coding agents
(Claude Code, Codex) and does the toil you currently do by hand: reading a messy, multi-message
conversation, working out what was actually asked, and prompt-engineering it into a well-structured,
executable task.

> ⚠️ All datasets and communications in this project are **synthetic**. It connects to no real
> employer data, credentials, or infrastructure.

## The two problems it solves

1. **Task Crystallizer** — real requests arrive scattered across a noisy thread with fuzzy start and
   end. A cheap relevance filter plus an LLM candidate-state engine assembles them into one coherent,
   validated task.
2. **Adjustable Autonomy Dial** — the system reads confidence + risk and recommends
   *Suggest* / *Co-pilot* / *Auto*, in plain English. The human always keeps the override.

Two design principles run through everything: the executor is **synthesis-first** (it writes and
sandbox-runs a Python script for the open-ended case, with a workflow registry acting as a learned
cache of proven fast paths), and every run emits a **reproducible Output Bundle** — deliverable,
generator code, exact inputs, seeded manifest — so any result can be audited and re-run.

## Status

**v0.5.0 — the workflow registry and task memory.** Two learned caches now sit in front of the
model. A proven bundle can be **promoted** from the dashboard into a permanent, frozen workflow;
the next matching request replays that code instead of synthesizing, judged by the same validator.
**Task memory** remembers the spec that satisfied a request and skips the interpreter on a repeat.
Both match on a free, deterministic fingerprint first and fall back to one cheap, confidence-gated
model call only on a miss — so the same request asked twice now costs no model call at all. See
[The two caches](#the-two-caches) below.

| Phase | Tag | Scope | State |
|-------|-----|-------|-------|
| 0 | `v0.1.0` | Walking skeleton: state machine, task API, dashboard, CI | ✅ shipped |
| 1 | `v0.2.0` | Intake + **Task Crystallizer** | ✅ shipped |
| 2 | `v0.3.0` | Interpreter + **Autonomy engine** + human-in-the-loop | ✅ shipped |
| 3 | `v0.4.0` | Synthesis-first executor, validator, Output Bundle | ✅ shipped |
| 4 | `v0.5.0` | **Workflow registry** + **task memory** | ✅ shipped |
| — | `v1.0.0` | Definition of done (spec §11) | 🎯 target |

Phases 2–4 are an indicative grouping of the spec's components, not yet broken down into written plans.

Design spec: [`docs/superpowers/specs/2026-08-18-ley-khaa-design.md`](docs/superpowers/specs/2026-08-18-ley-khaa-design.md).
Phase plans: [`docs/superpowers/plans/`](docs/superpowers/plans/).

## Run

```bash
docker compose up
```

> Upgrading from 0.2.0? This release adds Alembic. A database created by 0.2.0 has the tables but
> no `alembic_version`, so the app stamps it at the baseline automatically on first start and then
> applies the new columns. No manual drop is needed — and this is the last release that will ever
> ask. A fresh clone needs nothing.

- Dashboard: http://localhost:5173
- API: http://localhost:8000
  - `GET /health`
  - `GET /tasks`, `GET /tasks/{id}`
  - `POST /messages` — ingest one message through the full pipeline
  - `GET /candidates` — task candidates and their state
  - `GET /conversations/{id}/messages`
  - `POST /simulate/{name}` — replay a synthetic conversation fixture
  - `POST /candidates/sweep` — re-check ready candidates against the debounce gate
  - `POST /tasks/{id}/approve` — run an approved task through to completion
  - `POST /tasks/{id}/reject` — fail a parked task with a reason
  - `POST /tasks/{id}/mode` — override the autonomy mode (or clear the override)
  - `PATCH /tasks/{id}/spec` — edit the interpreted spec inline; re-scores the recommendation
  - `POST /tasks/{id}/answer` — answer a clarifying question; re-enters as a real message
  - `POST /tasks/{id}/promote` — freeze a passed bundle's winning script into a workflow
  - `GET /registry` — every cached workflow: origin, aliases, hash, run counts, quarantine state
  - `POST /registry/{name}/unquarantine` — clear a workflow blocked by a failed cached run
  - `DELETE /registry/{name}` — remove a workflow from the registry

Brings up Postgres + backend + frontend and seeds the demo by replaying a synthetic
conversation through the real intake pipeline. Verified from a fresh clone on Docker 29
(Colima on Apple Silicon).

### Which model actually runs

Stage A and Stage B call Claude through one seam. With no `ANTHROPIC_API_KEY` in the
environment the backend falls back to **`HeuristicLLM`** — a deterministic, regex-based
stand-in, not a model. It keeps a fresh clone and CI runnable without credentials, and it
is intentionally dumb: keyword matching, no language understanding, no reasoning about
which messages belong to the same request. It logs a warning at startup saying so. Every
result you see in the demo without a key comes from that stub.

For the real path, export a key before starting; Compose passes it through:

```bash
export ANTHROPIC_API_KEY=sk-...
docker compose up
```

To force the stand-in even with a key set, run with `LEY_KHAA_LLM=heuristic`.

**Offline synthesis is canned, not generated.** With no `ANTHROPIC_API_KEY` the executor still
produces a real, runnable script and a real deliverable — but that script is looked up by keyword
from two hand-written templates (`set_difference`, `summary_stats`), not written for the request.
Anything else gets a script that describes its inputs. The bundle's `manifest.json` records which
model produced the generator, so a bundle never overstates where its code came from.

### Where synthesized code runs

Docker, by default: `--network none`, read-only rootfs, non-root, 512 MB, 1 CPU, 64 pids, killed on
a wall-clock timeout, and only the running task's own bundle mounted — not the other tasks'.
The image (`backend/sandbox/Dockerfile`) carries the standard library, `openpyxl` and
`python-docx`, and nothing else. That is a subset of what the backend has, so the real sandbox
cannot run something the fallback couldn't; the reverse is not true, since the fallback runs the
backend's own interpreter and can therefore import `anthropic`, `fastapi` and the rest.

The container runs as the backend's own uid, so the backend must not be root. Under compose,
`backend/docker-entrypoint.py` drops to an unprivileged account before uvicorn starts; if the
backend is root anyway, the run fails rather than quietly producing a bundle that claims an
isolation it did not have. To be precise about what "unprivileged" buys you: that account is
deliberately joined to the group owning the Docker socket, because it has to be able to launch
sandbox containers at all. Membership of that group is root-equivalent *against the host daemon*.
So the drop protects the container's own filesystem and makes the sandbox's `--user` non-root; it
is not a claim that a compromised backend cannot reach Docker.

Scoping the mount to a single task uses the `volume-subpath` mount option, which needs
**Docker Engine 26.1 or newer** on the host. On anything older the run fails with
`SandboxUnavailable` and the task is marked failed — it never silently widens back to mounting
every task's bundle.

With no daemon reachable, `SubprocessSandbox` takes over so the Docker-free dev loop keeps working.
It caps CPU and memory and scrubs the environment — your `ANTHROPIC_API_KEY` is not visible to
synthesized code — but it **cannot remove network access**. It warns once per process, and the
bundle's manifest records `"sandbox": "subprocess"`. Set `LEY_KHAA_SANDBOX=docker` to refuse the
fallback entirely.

### The two caches

Two learned fast paths sit in front of the model, each short-circuiting one expensive step:

- **Task memory** remembers the `TaskSpec` that satisfied a request, keyed on a fingerprint of the
  request's own text (stopwords stripped, tokens sorted, so word order and politeness don't split a
  repeat). A recognised repeat skips the interpreter entirely.
- **The workflow registry** remembers proven generator code. A matched request runs that frozen
  source in the same sandbox, judged by the same validator, and skips synthesis entirely.

Chained, the same request asked twice costs **no model call at all** the second time: memory recalls
the spec, the registry replays the code, and both matchers try a free, deterministic fingerprint
match first. Only a miss there costs one cheap, confidence-gated model call — and that call is
**fingerprint-only offline**: with no `ANTHROPIC_API_KEY`, `HeuristicLLM` always answers "no match"
for both, so a repeat still hits with the same wording but a paraphrase does not. Both caches degrade
the same way — a miss just costs the normal path (interpret again, synthesize again) — never a wrong
answer served with false confidence.

A cached run is validated exactly like a synthesized one: same sandbox, same validator, same
escalation on failure. A workflow that fails is quarantined (blocked from future matches until a
human clears it) and the run falls through to synthesis with a full attempt budget, so a bad cache
entry costs only the work it was trying to save.

A workflow only enters the registry two ways: two hand-written seeds shipped in the repo, or
**promotion** — a human, from the dashboard, turning a bundle that just passed validation into a
permanent capability. Promotion is a pure byte-for-byte copy of the winning script, never a
rewrite; the stored `source_sha256` and a pointer back to the task that produced it are what make
that copy auditable.

On a fresh `docker compose up`, the registry ships with two seeds — `set_difference` (two csv
inputs, xlsx output) and `summary_stats` (one csv input, csv output) — installed before the seeded
demo conversation is replayed. That demo asks to compare Bloomberg against FactSet as Excel, which
is exactly `set_difference`'s shape, so **the seeded demo now takes the registry fast path** instead
of synthesizing. Anything else still synthesizes — the registry is a cache for known shapes, not the
only path.

Memory never remembers input *files* — only input *names*. A remembered spec's `inputs` are re-resolved
against the task that reused it, against its own attachments and catalog, every time; last week's
spec cannot quietly reuse last week's file.

### Local dev (no Docker)

The backend reads `DATABASE_URL`, so it runs on SQLite with no Postgres:

```bash
# backend
cd backend && pip install -e ".[dev]"
DATABASE_URL="sqlite:///./leykhaa.db" uvicorn ley_khaa.api.app:app --port 8000

# frontend (separate shell)
cd frontend && npm install && npm run dev
```

## Develop

```bash
cd backend  && python -m pytest -q   # 528 tests
cd frontend && npm test              # 37 tests (vitest)
cd frontend && npm run typecheck     # `npm run build` is transpile-only; this is the real check
```

The sandbox contract tests run against a real container, so build the image once
(`docker build -t ley-khaa-sandbox backend/sandbox`) or every `[docker]` parameter skips.

**On Docker Desktop alternatives (Colima, Rancher, Lima):** the VM usually mounts only `$HOME`,
while pytest's `tmp_path` lives under `/private/var/folders/...` on macOS. A bundle created there
is invisible inside the VM, so the docker-parametrized tests fail with a misleading
`No such file or directory` naming a path that plainly exists on the host. Point `TMPDIR`
somewhere under `$HOME` for the run:

```bash
TMPDIR="$HOME/.leykhaa-tmp" python -m pytest -q
```

## Stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy · Postgres (SQLite for dev/test) · custom
synchronous state-machine orchestrator (with a background sweeper for the debounce gate) ·
tiered model router (Claude Haiku ↔ Opus, with a deterministic `HeuristicLLM` stand-in when no
API key is set) · React · Vite · Tailwind · Docker Compose.

## Conventions

[SemVer](https://semver.org) tags per milestone · [Conventional Commits](https://www.conventionalcommits.org) ·
TDD throughout · `main` stays green and runnable at every tag.

## License

MIT — see [LICENSE](LICENSE).
