# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com); versioning is [SemVer](https://semver.org).

## [Unreleased]

## [0.5.0] — 2026-08-28

### Added
- Workflow registry (§3.3, §3.4, §5.6): a `workflows` table storing frozen source, its sha256, the
  operation aliases and input roles it matches, `origin` (`seed` | `promoted`), run counters, and a
  quarantine flag. `WorkflowRepository` hashes source on the way in and quarantines a workflow on its
  first cached-run failure.
- `RegistryMatcher`, a two-stage matcher: a free, deterministic fingerprint match (operation alias +
  format agreement + input count) first; one Haiku call only on a miss, gated at confidence 0.8, and
  its answer is treated as untrusted — the named workflow must exist, be active, and still bind
  before it counts as a match.
- `bind()`: positional binding of a workflow's declared roles to a run's resolved inputs by suffix.
  Every malformed or ambiguous case returns "no bind" rather than guessing — a wrong bind would run
  code proven for a different job and produce a plausible, wrong answer.
- Two hand-written seed workflows, installed idempotently at startup: `set_difference` (two csv
  inputs, xlsx output) and `summary_stats` (one csv input, csv output). A fresh clone can show the
  fast path before anyone has promoted anything, and the seeded demo conversation now matches
  `set_difference` and takes it.
- `ExecutionRunner`'s registry fast path (§3.4, §5.6): a matched request runs the workflow's frozen
  source in the same sandbox, judged by the same validator. The manifest records `lane: "registry"`,
  the workflow's name/hash/binding, and credits no model — none wrote the script. A failing cached
  run quarantines the workflow and falls through to synthesis with a full attempt budget.
- Promotion (§5.6): `POST /tasks/{id}/promote` turns a bundle's winning attempt into a workflow. A
  pure byte-for-byte copy of the script that actually passed — never a rewrite — with roles taken
  from the `params.json` binding that run used. Only a passed verdict can be promoted, and every
  bundle path (manifest, attempt, params) is read through the same containment check the bundle API
  uses, so generator-planted symlinks can't forge or exfiltrate their way into the registry.
- Registry API: `GET /registry`, `POST /registry/{name}/unquarantine`, `DELETE /registry/{name}`.
  The listing omits `source` by design — a workflow is identified by its hash, not by shipping
  model-written code into a page that renders it.
- Task memory (§3.5, §5.14): a `task_memory` table remembering the `TaskSpec` that satisfied a
  request, keyed on a fingerprint of the request's own text (stopwords stripped, tokens sorted, so
  word order and politeness don't split a repeat). Written only for a task that reached `done` with
  a passing verdict.
- `MemoryMatcher`, the same two-stage shape as the registry: an exact fingerprint hit costs no model
  call; a miss gets one cheap Haiku call at the same 0.8 confidence floor; null is always a legal
  answer, scoped by `TaskRow.project`, which is `"default"` for every task until §5.4 project
  routing lands — so today that scoping is structural, not yet a separation between clients.
- `TaskDriver`: a recognised repeat skips the interpreter entirely and reuses the remembered spec's
  shape — `source_message_ids` re-pointed at this task's own messages, and `inputs` re-resolved
  against this task's own attachments/catalog at execution time, never the remembered task's files.
- Autonomy: a familiarity bonus (+0.05 per remembered run, capped at +0.15) nudges confidence up for
  a repeat request. Withheld whenever the spec — or its candidate — still has a missing field, so
  repetition alone can never lift a spec with a known gap into Auto.
- Dashboard: a Registry page listing every cached workflow (origin, aliases, hash, run counts,
  quarantine state) with unquarantine and delete; a Promote control on a bundle that passed
  validation; a "remembered" badge on a task whose spec came from memory, naming the source task and
  how many times it's been seen; a bundle panel note when the deliverable replayed a registry
  workflow instead of being synthesized.
- `backend/tests/test_caches_end_to_end.py` (§9): the phase's headline claim as a test — the same
  request asked twice is interpreted and executed with zero model calls the second time and (for a
  csv deliverable) produces byte-identical output; a request no seed knows still synthesizes; and
  the seeded demo conversation itself takes the registry fast path once approved.

### Changed
- **`inputs/params.json` is now the only contract a generator script may use for its input and
  output paths — the one change affecting Phase 3 behaviour.** Every synthesized script (not only a
  cached one) now reads `{"inputs": {"<role>": "<filename>"}, "output": "...", "seed": <int>}` from
  that file instead of having filenames written into its prompt. A script that hardcoded a filename
  could not be re-run against different data, which made it unpromotable; every generator, seeded or
  synthesized, now has the same shape a promoted workflow needs. `params.json` is written into
  `inputs/`, so it travels with the bundle, is covered by the existing tamper check, and re-running
  `generator/run.sh` reproduces the same binding.
- `tasks` gains `remembered_from_task_id` and `familiarity` (Alembic `0004_registry_memory`, which
  also creates `workflows` and `task_memory`). `GET /tasks/{id}` now returns both fields.

## [0.4.0] — 2026-08-27

### Added
- Synthesis-first executor (§5.10): `TaskSpec` → resolved inputs → a synthesized Python script →
  a sandboxed run → a validated deliverable, all behind one `ExecutionRunner.run()`.
- Input resolution: message attachments first, then a Faker-seeded catalog of synthetic securities
  datasets. A name that matches neither becomes a clarification **before** any model call.
- Sandboxes: `DockerSandbox` (no network, read-only rootfs, non-root, capped, killed on timeout, and
  only the running task's own bundle mounted) and `SubprocessSandbox` (capped and
  environment-scrubbed, but *not* network-isolated). One contract test both must pass, and CI builds
  the sandbox image so the docker cases actually run there. The manifest records which one ran.
- `backend/docker-entrypoint.py`: under compose the backend drops to an unprivileged account before
  uvicorn starts. Each sandbox container inherits that uid, so "non-root" is true on the compose
  path and not only on a developer's machine; a root backend now fails the run rather than
  producing a bundle that overstates its isolation.
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
- A re-executed task (escalate → answer → re-run) clears `deliverable/` first and continues its
  generator numbering, so the verdict describes the run that just happened and the earlier round's
  attempts are still in the bundle. The manifest gains `earlier_attempts`.
- A symlink in `deliverable/` is never treated as a deliverable. Validation fails and says a link
  was left in place of an output file, instead of the manifest attesting bytes the run never wrote.
- The background sweeper no longer re-drives tasks in `EXECUTING`. Every other mid-flight step is
  idempotent; re-entering execution started a second synthesis-and-sandbox lane on the same bundle
  every 15 seconds.
- Sandbox stdout/stderr and `GET /tasks/{id}/bundle/download` are size-capped. A truncated capture
  says how much it dropped; an oversized bundle is a 413 pointing at the per-file routes.
- An attachment whose name carries no matchable tokens (`""`, `"---"`, `".csv"`) now matches no spec
  input at all. It used to match every one of them, taking the first and beating the catalog to it.
- `npm run typecheck` exists and runs in CI; `tsc --noEmit` was previously run nowhere.
- `TaskDriver._execute` and `_validate` are no longer stubs. `_execute` runs the lane and persists
  a verdict; `_validate` acts on it. The state machine is unchanged.
- The offline interpreter matches multi-word source names ("bloomberg universe") as one input
  rather than emitting an ambiguous bare "universe" that resolves to nothing.
- `tasks` gains `workspace_path` and `execution_verdict` (Alembic `0003_executor`).

## [0.3.0] — 2026-08-21

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
- Alembic migrations: a database created by 0.2.0's `create_all()` has the tables but
  no `alembic_version` row, so `run_migrations()` stamps it at the baseline
  (`0001_baseline`) automatically on first start, then upgrades to head normally — no
  manual drop, unlike the 0.2.0 upgrade. A test fails if the SQLAlchemy models and the
  migrations ever disagree.
- A second golden conversation with a deliberate gap, and end-to-end tests for both
  the dial and the clarification loop.

### Changed
- A promoted task no longer races to `done`. It is interpreted, scored, and then either
  parks at `awaiting_approval` or — when the effective mode is Auto — runs through.
- The background sweeper also re-drives stalled tasks, which is how an interpretation
  that hit a transport failure gets retried.
- `InvalidTransition` now surfaces as **409** rather than a 500; a malformed spec patch
  as **422**.
- The task state machine gained the three edges the clarification loop needs:
  `CLASSIFIED -> NEEDS_CLARIFICATION` (the interpreter escalates a gap),
  `NEEDS_CLARIFICATION -> CLASSIFIED` (an answered clarification is
  re-interpreted), and `AWAITING_APPROVAL -> INTERPRETED` (editing a parked
  spec re-enters scoring).

### Fixed
- The demo path was tearing a single request in half: replaying the demo conversation
  produced two half-specified tasks instead of one — one candidate knew the data
  sources but not the output format, the other knew the format but not the sources.
  Cause: the simulator replayed messages one at a time, and because it backdates
  timestamps, each message looked like the last thing said long ago — so the
  readiness gate promoted the first candidate immediately, making it terminal, and
  the follow-up message could not join it. Fixed by replaying the whole conversation
  before letting the gate decide: `Orchestrator.ingest` gained a `promote` flag, and
  `Simulator.replay` now ingests every message with promotion skipped and sweeps once
  at the end. This was a latent Phase 1 bug that only became visible once tasks were
  really interpreted — before, both halves ran silently to `done` through the stub.

### Known limitations
- **Suggest and Co-pilot behave identically**: both park at the single approval gate.
  They diverge in 0.4.0, when the executor has mid-run checkpoints.
- **Execution is still a stub.** `executing → validating → done` does no real work.
- The offline `HeuristicLLM` reports a deliberately mediocre certainty (0.55, below the
  0.85 Auto threshold), so a no-API-key clone never reaches Auto on its own — keyword
  matching must not run tasks unattended.

## [0.2.0] — 2026-08-19

> **Upgrading from 0.1.0:** this release adds columns to the `messages` table and the project has
> no migration tooling yet. Drop your existing database first — `docker compose down -v` for the
> Postgres volume, or delete your local `leykhaa.db` for the SQLite dev path. A fresh clone is
> unaffected.
### Added
- Intake gateway: canonical multi-modal `Message` (text/table/image attachments), idempotent per external id.
- Task Crystallizer stage A — cheap per-message relevance and topic filter.
- Task Crystallizer stage B — stateful LLM candidate engine: candidates own only their own message ids, interleaved topics become separate candidates, readiness and missing-field tracking.
- Readiness gate debouncing emission until the conversation settles.
- Model Router (`model_for(stage, complexity)`) with a testable policy table and a thinking-capability flag.
- `HeuristicLLM` offline fallback so a fresh clone demos with no API key.
- Conversation simulator plus a golden messy synthetic conversation fixture.
- API: `GET /candidates`, `GET /conversations/{id}/messages`, `POST /simulate/{name}`; `POST /messages` now returns an intake ack.
- Dashboard panel showing candidates forming, with state and owned-message counts.
- `POST /candidates/sweep` re-evaluates ready candidates against the readiness gate, so a debounce can actually elapse (the gate is evaluated at message arrival, when no time has passed yet).
- Background sweeper on the FastAPI lifespan (`LEY_KHAA_SWEEP_SECONDS`, default 15) so the debounce gate has a trigger in live use, not only in the backdated demo.
- Stage A verdicts (`relevant`, `topic`, `confidence`) are persisted on the message and used to prune known chatter out of the stage B window.
- `anthropic` SDK dependency.

### Changed
- The orchestrator no longer turns every message into a task — only a `ready` candidate is promoted.
- Promotion claims a candidate with a conditional update before creating its task, so concurrent sweeps cannot double-create a task.
- Model message ids are validated against the conversation before they can reach a Task.
- README expanded for a public audience: problem framing, phase roadmap, both run paths
  (`docker compose up`, plus a no-Docker SQLite dev path), the full endpoint list, and an
  honest description of the offline `HeuristicLLM` stand-in.

### Fixed
- The offline stand-in keyed every candidate the same way, so a conversation could only ever produce one task; the key is now derived from the messages a candidate owns.
- `POST /messages` with empty or whitespace-only text returns 422 instead of 500.

### Verified
- `docker compose up` confirmed working from a fresh clone on Docker 29 / Colima (Apple Silicon):
  Postgres healthy, backend seeds the demo by replaying a synthetic conversation, frontend serves
  on :5173. This was the last unverified part of 0.1.0.

## [0.1.0] — 2026-08-18
### Added
- Foundation walking skeleton: FastAPI backend, task state machine, orchestrator (stub), Task API.
- React/Vite/Tailwind dashboard listing tasks.
- Docker Compose (Postgres + backend + frontend) with startup seed of a synthetic demo task.
- Repo hygiene: README, LICENSE, CONTRIBUTING, CHANGELOG, CI.
