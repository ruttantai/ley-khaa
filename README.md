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

**v0.6.0 — project routing, per-project queues, and amendment detection.** Every task now lands in
a **project**, decided by a two-stage router at promotion time; each project drains through its own
concurrent, per-project worker instead of one shared lane; and a follow-up message that modifies a
task already under way is detected and either folded in automatically or parked for a human to
decide. See [Projects and queues](#projects-and-queues) below.

**v0.5.0 — the workflow registry and task memory.** Two learned caches sit in front of the
model. A proven bundle can be **promoted** from the dashboard into a permanent, frozen workflow;
the next matching request replays that code instead of synthesizing, judged by the same validator.
**Task memory** remembers the spec that satisfied a request and skips the interpreter on a repeat.
Both match on a free, deterministic fingerprint first and fall back to one cheap, confidence-gated
model call only on a miss — so the same request asked twice is interpreted and executed with no
model call at all. That covers interpretation, matching and execution; it does not cover intake —
relevance filtering and crystallization run on every message regardless, caches or not. See
[The two caches](#the-two-caches) below.

| Phase | Tag | Scope | State |
|-------|-----|-------|-------|
| 0 | `v0.1.0` | Walking skeleton: state machine, task API, dashboard, CI | ✅ shipped |
| 1 | `v0.2.0` | Intake + **Task Crystallizer** | ✅ shipped |
| 2 | `v0.3.0` | Interpreter + **Autonomy engine** + human-in-the-loop | ✅ shipped |
| 3 | `v0.4.0` | Synthesis-first executor, validator, Output Bundle | ✅ shipped |
| 4 | `v0.5.0` | **Workflow registry** + **task memory** | ✅ shipped |
| 5 | `v0.6.0` | **Project routing**, per-project queues, **amendment detection** | ✅ shipped |
| — | `v1.0.0` | Definition of done (spec §11) | 🎯 target |

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
- API: http://localhost:8000 (this list is the full route table — see `backend/ley_khaa/api/app.py`
  if it ever drifts)
  - `GET /health`
  - `GET /tasks`, `GET /tasks/{id}`
  - `POST /messages` — ingest one message through the full pipeline
  - `GET /candidates` — task candidates and their state
  - `GET /projects` — every active project, its queue depth, and the task (if any) currently leased
  - `POST /projects` — create a project; needs a non-empty description, since that's what stage-2
    routing reasons over — an empty one would be unroutable by construction
  - `GET /projects/{name}/queue` — every task in one project
  - `GET /triage` — candidates parked as a possible amendment to an active task, awaiting a human
    decision (see [Amendments](#amendments))
  - `POST /candidates/{id}/fold` — fold a parked candidate into the task it amends
  - `POST /candidates/{id}/separate` — promote a parked candidate as its own task instead
  - `GET /conversations/{id}/messages`
  - `POST /simulate/{name}` — replay a synthetic conversation fixture
  - `POST /candidates/sweep` — re-check ready candidates against the debounce gate
  - `POST /tasks/{id}/approve` — run an approved task through to completion
  - `POST /tasks/{id}/reject` — fail a parked task with a reason
  - `POST /tasks/{id}/mode` — override the autonomy mode (or clear the override)
  - `PATCH /tasks/{id}/spec` — edit the interpreted spec inline; re-scores the recommendation
  - `POST /tasks/{id}/answer` — answer a clarifying question; re-enters as a real message
  - `GET /tasks/{id}/bundle` — how the deliverable was produced: sandbox, model, per-attempt
    verdicts and sha256
  - `GET /tasks/{id}/bundle/file?path=` — one file out of the bundle, path-traversal guarded
  - `GET /tasks/{id}/bundle/deliverable` — the deliverable file itself
  - `GET /tasks/{id}/bundle/download` — the whole bundle, zipped
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

Chained, the same request asked twice is **interpreted and executed with no model call at all** the
second time: memory recalls the spec, the registry replays the code, and both matchers try a free,
deterministic fingerprint match first. Only a miss there costs one cheap, confidence-gated model
call — and that call is **fingerprint-only offline**: with no `ANTHROPIC_API_KEY`, `HeuristicLLM`
always answers "no match" for both, so a repeat still hits with the same wording but a paraphrase
does not. Both caches degrade the same way — a miss just costs the normal path (interpret again,
synthesize again) — never a wrong answer served with false confidence.

That "no model call" claim is scoped to interpretation, matching and execution — the two caches sit
downstream of intake. `Orchestrator.ingest` calls the relevance filter and the crystallizer on every
message unconditionally, cache hit or not, so a user asking the same thing twice in the actual chat
still spends those two calls on the second ask; only the interpret-and-execute step behind them
becomes free.

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
is exactly `set_difference`'s shape. The demo task itself lands parked at `awaiting_approval` and
runs nothing at startup — but when it is approved, that run takes the registry fast path instead of
synthesizing. Anything else still synthesizes — the registry is a cache for known shapes, not the
only path.

Memory never remembers input *files* — only input *names*. A remembered spec's `inputs` are re-resolved
against the task that reused it, against its own attachments and catalog, every time; last week's
spec cannot quietly reuse last week's file.

### Projects and queues

A **project** (`GET /projects`, `ProjectRow`) is a named, described client or workstream — `name`,
`display_name`, `description`, `active`. It exists so one client's work queues, gets driven, and (see
[The two caches](#the-two-caches)) is remembered separately from another's. Every clone starts with
exactly one project, `default`, seeded idempotently at startup — a fresh clone, or a task that
routing can't confidently place, always has somewhere to land.

**Routing decides a task's project at promotion**, the moment a candidate becomes a task — not
before, and never again afterwards: nothing re-routes an existing task, and moving one between
projects has no API. `ProjectRouter` is the same two-stage shape as the workflow registry and task
memory matchers:

1. **A free lookup first.** If this message's source/client/conversation already has a binding —
   written by a previous stage-2 match, see below — that project wins for free, no model call.
2. **One confidence-gated model call on a miss.** The model is shown the request and every active
   project that has a description (a project with no description, like `default`, is unroutable by
   stage 2 by construction — reachable only via an explicit binding) and names one, or null. Below
   confidence 0.8, or on any model or transport failure, the task goes to `default` rather than
   guessing — a null routing decision costs a human sorting one task; a wrong one puts a client's
   request in another client's queue.

**A confident stage-2 match writes a binding** for that conversation, so every later message in it
routes free from then on — the same learning rule the registry and memory matchers follow, applied
to routing instead of matching.

Routing assigns a project **per client**, so memory's project scoping (see
[The two caches](#the-two-caches)) becomes a real boundary between clients wherever a binding
exists. It is not blanket client isolation: anything that never routes — an unrouted source, a miss,
a project with no description — still shares `default`, and so does its memory.

**Each project drains through its own worker, concurrently with every other project's**, proven by
`backend/tests/test_concurrency.py`'s barrier test: two projects' workers are made to block until
both have arrived, which only passes if both are genuinely running at once. Within one project,
tasks are driven strictly FIFO, one at a time. Queue reordering by urgency is not built: urgency
lives in the `TaskSpec`, which is only known after a task has already been interpreted and
dequeued.

Two dispatch modes, `LEY_KHAA_DISPATCH=inline|workers` (default `workers`, what `docker compose up`
runs):

- **`workers`** (default) — a background async `Dispatcher` gives every project with runnable work
  its own worker, up to `LEY_KHAA_MAX_PROJECTS` (default 4) at once. Each task is driven only under
  a **lease** (`LEY_KHAA_LEASE_TTL`, default 120s, heartbeated every `LEY_KHAA_LEASE_HEARTBEAT`,
  default 30s), so a worker that dies mid-flight is recoverable: once its lease expires, another
  worker can reclaim the row rather than the task staying stuck. `LEY_KHAA_MAX_LEASE_ATTEMPTS`
  (default 3) caps how many times a task can be reclaimed before it is failed visibly as poison,
  rather than being retried forever at cost.
- **`inline`** — every task is driven synchronously on the calling thread, no lease taken. This is a
  real supported mode, not a test shim: it is what the whole test suite pins, and it is the right
  choice for a single-operator run where nothing else is contending for a task.

### Amendments

A follow-up message can modify a task that is already under way instead of starting a new one —
"actually, make that a PDF" while the original request is still running. `AmendmentDetector` runs
the same two-stage shape again: a free check for whether the project has any active task at all
(almost always the fast "no" for a brand-new request), then one confidence-gated model call naming
which active task, if any, the new request modifies.

A detected amendment isn't folded in blindly — `recommend_fold` (the autonomy dial's fold decision)
asks for all of: the target task in `AUTO` mode, not already in a state that has committed resources
(`EXECUTING`/`VALIDATING`), no outstanding missing fields on the target's spec, and detector
confidence above 0.9 (higher than the 0.8 detection floor, since folding is destructive — a request
folded in is no longer separate). Failing any of those parks the candidate for a human, visible on
the dashboard's **Triage** tray (`GET /triage`) and resolved with `POST /candidates/{id}/fold` or
`POST /candidates/{id}/separate`.

**Amendment detection is within a project only** — a follow-up that lands in a different project is
always a new task, never matched against another project's work.

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
cd backend  && python -m pytest -q   # 632 tests
cd frontend && npm test              # 48 tests (vitest)
cd frontend && npm run typecheck     # `npm run build` is transpile-only; this is the real check
```

The sandbox contract tests run against a real container, so build the image once
(`docker build -t ley-khaa-sandbox backend/sandbox`) or every `[docker]` parameter skips.

**On Docker Desktop alternatives (Colima, Rancher, Lima):** the VM usually mounts only `$HOME`,
while pytest's `tmp_path` lives under `/private/var/folders/...` on macOS. A bundle created there
is invisible inside the VM, so the docker-parametrized tests fail with a misleading
`No such file or directory` naming a path that plainly exists on the host. Point `TMPDIR`
somewhere under `$HOME` for the run:

Docker-parametrized tests need a temp directory Colima can see:
`mkdir -p "$HOME/tmp" && TMPDIR="$HOME/tmp" python -m pytest -q`. The directory must exist first —
otherwise pytest silently falls back to `/private/tmp` and the sandbox tests fail with a misleading
"can't open file".

## Stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy · Postgres (SQLite for dev/test) · custom
synchronous state-machine orchestrator (with a background sweeper for the debounce gate and an
async, lease-based, per-project dispatcher for the task queue) · tiered model router (Claude Haiku
↔ Opus, with a deterministic `HeuristicLLM` stand-in when no API key is set) · React · Vite ·
Tailwind · Docker Compose.

## Conventions

[SemVer](https://semver.org) tags per milestone · [Conventional Commits](https://www.conventionalcommits.org) ·
TDD throughout · `main` stays green and runnable at every tag.

## License

MIT — see [LICENSE](LICENSE).
