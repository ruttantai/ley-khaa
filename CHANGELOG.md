# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com); versioning is [SemVer](https://semver.org).

## [Unreleased]

## [0.10.0] — 2026-09-02

Release hardening. No new features: ten defects fixed — nine carried in the Phase 5 backlog, one
found while writing the spec — two quality gates added, and every statement this phase found to be
false corrected. The branch's own whole-branch review then found four more defects, all fixed here
before the release: a head task that could starve its project's queue, `LEY_KHAA_MAX_PROJECTS=0`
silently wedging the dispatcher for ever, a dead letter suppressed past the failure it described,
and a mistyped retention cap that stopped the service at import.

### Added
- **A backend typechecker, enforced by CI (§4.1).** "Typecheck clean" is a definition-of-done
  line in three of the five phase specs since Phase 4 (v0.5.0, 2026-08-27) — Phases 4, 6 and 7 name
  it, Phases 5 and 8 do not — and until now only the *frontend* had
  a typechecker to satisfy it — `npm run typecheck` landed the same day; `backend/` had none
  configured at all. `mypy==2.3.1` at **default**
  settings — not `--strict`, which is mostly annotation churn and is filed as a post-1.0.0 ratchet —
  runs over `ley_khaa` and `docker-entrypoint.py` and fails the build on any error. Proven to gate
  rather than warn: a deliberate wrong return type made the lane exit 1 while the full suite stayed
  green, which is the whole point — the lane catches what the tests cannot see.
- **A Postgres lane in CI (§4.2, backlog item 9).** The suite has always run on SQLite while
  `docker compose up` deploys Postgres 16, so the database the project actually ships on was tested
  by nothing. Setting `DATABASE_URL` now runs the *whole* suite against Postgres; CI runs both lanes
  over one setup, with `if: !cancelled()` on the second so a red build says whether a failure is
  Postgres-only. Isolation on that lane is a dedicated `ley_khaa_test` schema plus a per-test
  `TRUNCATE`, which keeps commits real and keeps the concurrency tests' two threads genuinely
  independent.
- `pytest --database=sqlite|postgres` asserts which database a run actually used and refuses to
  continue otherwise. It selects nothing and supplies no URL: it exists because deleting CI's `env:`
  block would otherwise re-run SQLite, print the same pass count a second time, and go green having
  never touched Postgres — and a check keyed on `DATABASE_URL` cannot catch that, since the
  malformation removes the variable the check reads.
- `dead_letters` retention (backlog item 18): `LEY_KHAA_DEAD_LETTER_MAX_ROWS`, default 1000, keeps
  the newest rows. A permanently bad token previously wrote one row a minute for as long as the
  process ran.
- `tasks.last_notified_question` (migration `0009`) and `image_extractions.url_sha256` (migration
  `0010`), backing the two fixes below.

### Fixed
- **A second, different clarifying question is delivered** (item 17). The re-notification guard was a
  compare-and-swap on the task's *state*, so a task asked a new question without leaving
  `NEEDS_CLARIFICATION` lost it silently — the run had failed, a human was blocked, and the channel
  said nothing. The CAS now covers the question text as well, NULL-safely, so a new question re-arms
  the guard while a re-drive with the same one still does not.
- **A poisoned task now notifies when it fails** (item 16). `_fail_poison` was the one path to FAILED
  that had no notifier at all. Wiring it exposed a live ordering bug: the workers-mode dispatcher is
  built once at startup and freezes whatever notifier it was given, so it had to be built *after* the
  real `ChannelNotifier` is installed — that ordering is now pinned by a test.
- **A project drains its whole backlog per tick, and a slow project no longer paces the others**
  (item 11). Review of the first fix found two head-of-line-blocking cases it did not remove: a
  poison-failed or race-lost head task ended the drain as if the lane were empty, and the
  per-project semaphore was held for a whole drain rather than per task. The whole-branch review
  then found a third, in the drain's own termination guard: a head task the driver left unadvanced
  ended the lane too, abandoning everything queued behind it — permanently, since a cleanly released
  task's `lease_attempts` never grows and the poison cap that might have evicted it can never fire.
  The guard now excludes already-attempted ids from the *query* instead, so the drain steps past
  such a head. The guarantee is therefore **one attempt per task per tick**, not "until nothing
  runnable is left": a task that does not advance is still runnable when the tick ends, but it no
  longer takes its project's queue with it.
- **An unfetchable image is recorded once, not dead-lettered on every drive** (item 19), via a second
  key space hashed from the source URL — there are no image bytes to key a refusal on when the fetch
  itself failed. That suppression is now scoped to a source that is *still* unfetchable, as its
  contract always claimed: the negative row is retired the moment the same source does fetch, so a
  genuinely new failure of a URL that worked in between is dead-lettered rather than silently
  swallowed.
- **`LEY_KHAA_MAX_PROJECTS=0` no longer wedges the dispatcher.** `asyncio.Semaphore(0)` can never be
  acquired, so every project's lane blocked for ever, `tick()` never returned, and `run_forever`'s
  own error handling cannot see a hang — the dispatcher stopped draining everything, silently, with
  no log line. Clamped to at least 1, the same posture `dead_letter_max_rows` already had.
- **A mistyped `LEY_KHAA_DEAD_LETTER_MAX_ROWS` no longer stops the service.** It raised `ValueError`
  during module import, before logging exists — the opposite posture to the clamp two lines away,
  which exists precisely so a misconfigured retention cap cannot take the service down. It now falls
  back to the default and warns.
- `AnthropicLLM.parse` and `extract_image` raise instead of returning an unguarded `None` when the
  model stops on `max_tokens`, and a truncated interpreter response is now labelled by its real cause
  rather than as "interpreter unavailable", which sent operators hunting for a network problem that
  did not exist.
- `ProjectIn.name` is validated against the same `NAME_PATTERN` as its sibling `PromoteIn` (item 13);
  `GET /projects/{name}/queue` is renamed `/projects/{name}/tasks`, which is what it always returned
  (item 15); `workflow_repository.py`'s redundant post-UPDATE read and the false comment justifying it
  are gone (item 10).
- The dashboard's Registry list refreshes after a promotion made from a `BundlePanel` on the same
  page (item 8), and Promote's Cancel no longer leaves stale input behind.
- **One live-reachable crash and three latent defects found by adopting mypy.** The live one: the
  Discord notifier reached for `get_partial_message` on a forum channel — a forum post is a thread,
  so its conversation id carries the forum as the parent, and the moment that thread left the
  client's cache the notification raised a bare `AttributeError` the dead-letter record could not
  explain. The other three are latent — signatures declaring what the code cannot deliver, exposed by
  the types rather than by a failure — and are fixed at cause rather than annotated away:
  `ReadinessGate.should_emit` declared a non-Optional `last_message_at` its callers can pass `None`
  for, `_route_reply` handed a nullable id to `Session.get`, and `TaskDriver`'s re-reads returned
  `TaskRow | None` from methods declared `-> TaskRow`. Counting all four as bugs found would
  overstate it; counting none of them would understate what the gate is for.

### Changed
- Test coverage for the two gaps §4.3 names (items 7 and 12): migration downgrades — `downgrade base`
  leaves no table behind, and a head → *each* revision → head round trip is diffed against the models
  (nine of the ten downgrades redden it when deleted; `0006`'s JSON↔JSONB `alter_column` stays
  uncovered — *corrected in 1.0.0: the reason given here, that SQLite renders the two types
  identically, is true but is not the mechanism. The round trip compares only the schema after the
  re-upgrade, and `0006.upgrade()` sets `jsonb` either way, so a no-op downgrade leaves no residue
  on **any** database. Re-filed as backlog item 40*) — `fingerprint_candidates`'
  empty-operation guard,
  `confidence == CONFIDENCE_FLOOR` exactly as a match, `_remember`'s own empty-fingerprint guard, and
  `HeuristicLLM`'s offline `ProjectChoice` rule — which nothing tested, because `ProjectRouter`'s
  blanket `except` turned a missing rule into a default differing only in a string nothing asserted
  on. Every one was verified by deleting the behaviour and watching the test fail.
- `backend/pyproject.toml` declares version `0.10.0`; it had said `0.6.0` since v0.7.0 shipped.

### Known limits
- **The suite's green depends on pytest's default collection order.** In reverse file order one test
  fails, identically on both database lanes: `test_ollama_config.py` reloads `ley_khaa.config`, which
  rebinds `settings` out from under modules that already imported it. The shipped code is not
  involved. Filed as backlog item 25 with a three-file reproducer; it must be fixed before anything
  introduces test-order randomisation or `pytest-xdist`.
  ***Closed in 1.0.0** (`68b348c`, `f84d313`). Also corrected there: this named one reloading file,
  and there were two — `test_vision_config.py` reloaded at eight further sites, and the fix needed
  both of them as well as the production change.*
- **Alembic migrations are still exercised on SQLite only** (backlog item 26). The Postgres lane
  builds its schema with `create_all`, deliberately, so that a failure there is unambiguously a
  dialect difference rather than a migration bug — which leaves the drift guard, the downgrade tests
  and the `JSONB` variant with no automatic Postgres coverage.
  ***Closed in 1.0.0** (`8975f84`): `test_migrations.py` now runs on whichever database the lane
  names, in a throwaway schema of its own.*
- **mypy runs at default settings, and `ignore_missing_imports` is global** for the sake of one
  untyped dependency (openpyxl), so a future untyped dependency would be exempted silently (item 24).
  CI's mypy dependencies are also unpinned floors, so a stub release can redden the gate on a commit
  that changed nothing (item 23).
- Items 1, 2, 3, 20, 21 and 22 of the Phase 5 backlog remain open by decision, each with its reason
  recorded in that file: memory still does not learn paraphrases, task memory still has no management
  surface, vision is still text-only on the Ollama path, and there is still no runtime step-down
  between backends.

## [0.9.0] — 2026-09-02

### Added
- **Ollama offline fallback (§11).** `OllamaLLM`, a third implementation of `LLMClient` alongside
  `AnthropicLLM` and `HeuristicLLM`, talks to a local Ollama daemon so a clone with no
  `ANTHROPIC_API_KEY` gets a real model instead of the regex stand-in. One implementation covers
  every stage: Ollama takes a JSON schema as its `format` parameter, and every stage's output is
  already a Pydantic model that can produce one.
- `LEY_KHAA_LLM=ollama` selects the backend; `LEY_KHAA_OLLAMA_MODEL` (default `qwen2.5`) names the
  local model used for every stage, and `LEY_KHAA_OLLAMA_HOST` (default `http://localhost:11434`)
  says where the daemon lives.
- The manifest names the real producer — `ollama:<model>`, e.g. `ollama:qwen2.5` — never a Claude
  id, so a bundle never credits Claude for work a local model did.
- `build_llm` probes the daemon once, at startup: is it reachable, and is the configured model
  pulled. Either failure degrades to `HeuristicLLM` with a one-time WARNING naming the specific
  cause, so `docker compose up` keeps demoing even with Ollama down or unconfigured.
- `docker-compose.yml` maps `host.docker.internal` to the host (`extra_hosts`) and defaults
  `LEY_KHAA_OLLAMA_HOST` to it under compose, since Ollama runs on the host, not in a container.

### Known limits
- **No runtime step-down.** The backend is chosen once, at startup. A Claude call that fails is not
  retried on Ollama, and vice versa (backlog item 22). Design spec §7 asks for this; it is out of
  scope here because a per-call fallback would make the producer a property of the call rather than
  the client, which the manifest's attribution does not currently support.
- **Vision stays text-only on the Ollama path.** `OllamaLLM.extract_image` returns the same
  carried-not-read shape `HeuristicLLM.extract_image` already produces — an image is recorded and
  named in the manifest, but nothing reads it (backlog item 21).
- **Output quality depends entirely on the local model.** A small quantised model produces weaker
  specs and scripts than Opus, and the system does not detect or warn about that beyond naming the
  model in the manifest.

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
- `LEY_KHAA_VISION` (default `on`) turns the whole path off; `LEY_KHAA_IMAGE_HOSTS` and
  `LEY_KHAA_IMAGE_MAX_BYTES` (default 5 MB) bound what gets fetched.

### Changed
- A pasted CSV now beats a screenshot of the same table when their filenames collide (the resolver
  binds textual attachments first, then drops any vision-extracted attachment whose stem collides
  with one already bound) — real bytes win over a model's reading of a picture.

### Known limits
- With no `ANTHROPIC_API_KEY` an image is carried, not read: it is recorded, credited to the
  offline `HeuristicLLM` stand-in, and its name still reaches the prompt, but the task proceeds on
  text alone. `docker compose up` still demos end to end.
- **No re-extraction of a successful read.** If vision misreads a table the frozen checkpoint stays
  wrong until its row is deleted — freezing on the image's hash is what makes a re-run
  reproducible.
- An unfetchable or undecodable image has no bytes to key a cache row on, so the failure is retried
  on every drive and dead-lettered again each time (backlog item 19).
- A same-backend model failure (a transient 503, a rate limit) is stored and stays frozen under
  that image's digest — the cache re-extracts on a configuration change (a different or newly
  enabled backend) but not on a retry of the same one (backlog item 20).
- Images are not stored, only their extraction, so an image whose URL has expired cannot be
  re-read. A re-drive past that point asks a human rather than silently computing on the synthetic
  demo catalog instead, but only when the image's own filename shares a token with the input it
  could be satisfying (`holdings.png` for an input named "holdings") — a generic or auto-generated
  name (`image.png`, a macOS `Screenshot ....png`) is not recognized as any particular input, and
  the run proceeds on catalog data with the manifest recording the unread image explicitly rather
  than staying silent about it.
- The offline fallback planned for 0.9.0 is text-only, so **vision will not work on the offline
  path** (backlog item 21). A local vision-capable model is roadmap.
- Not live-tested against a real Slack or Discord image — proven offline and against recorded
  transports, the same call made for the channel adapters in 0.7.0.

## [0.7.0] — 2026-08-31

### Added
- Real Slack and Discord channel adapters (§5.1), ingesting **and** notifying. Each adapter is split
  in two: a pure `translate.py` (platform event → the raw dict `IntakeGateway.accept()` already
  takes) holding the allowlist, the self-message filter, thread derivation and the dedupe key, and a
  thin `client.py` holding the socket and no decisions. Both platforms dial out — Slack Socket Mode,
  Discord Gateway — so there is no public URL, no tunnel and no inbound port, and adapters run as
  supervised asyncio tasks in the FastAPI lifespan beside the Phase 5 dispatcher. `docker compose up`
  is still one command.
- An explicit **channel allowlist** (`LEY_KHAA_SLACK_CHANNELS`, `LEY_KHAA_DISCORD_CHANNELS`),
  enforced before anything is persisted. Empty means ingest nothing, never everything. Startup logs
  exactly which channels are live.
- **Notification** as a `Notifier` seam injected into `TaskDriver` — the same shape as `LLMClient`
  and `SandboxRunner`, with `NullNotifier` as the default, so every existing test and every
  token-free run is unchanged. Exactly four states speak: `needs_clarification` (the question),
  `awaiting_approval` (the effective mode and its reason), `done` (the bundle path) and `failed`
  (the reason). `tasks.last_notified_state` plus a compare-and-swap (`TaskRepository.mark_notified`)
  is what stops a re-entrant `advance()` repeating a question every pass.
- **A reply in the thread answers the question** (§3.7). The rule lives in `Orchestrator.ingest`,
  not in an adapter — deciding what a message means is business logic — so a Slack thread reply and
  a dashboard answer take the identical path and are identical rows in storage.
- **Dead letters** (§3.8): a `dead_letters` table, `GET /dead-letters`, and a dashboard panel that
  is loud when there is something and absent when there is not. Written on a failed translation, a
  failed notification and an adapter crash. Payloads are redacted at the write — Slack's own Socket
  Mode envelope carries a `token` field.
- `Simulator` now satisfies `ChannelAdapter`, so the protocol has three implementations rather than
  being shaped around Slack and bolted onto the others.

### Changed
- `AdapterSupervisor` restarts a crashed adapter with capped exponential backoff, dead-letters the
  crash, and never lets it reach the API or the dispatcher. Cancellation is shutdown, not a crash.
- Notification is fire-and-forget across the sync/async boundary: `TaskDriver.advance()` is
  synchronous and runs on a dispatcher worker thread, so `ChannelNotifier` hands its coroutine to
  the loop captured at lifespan start and does not wait — a wedged platform API cannot extend a
  task's execution time. Under `LEY_KHAA_DISPATCH=inline` a notification is dead-lettered rather
  than delivered.

### Dependencies
- `slack_sdk==3.44.0`, `discord.py==2.7.1`, backend image only. The sandbox image is untouched.

### Known limits
- Approve, reject and mode override are dashboard actions; there are no interactive buttons, so a
  phone-only workflow is not possible.
- Notification is best-effort with dead-lettering, not a durable outbox.
- A task abandoned past `max_lease_attempts` is failed by the dispatcher, which has no notifier —
  so that one failure path does not notify. Tracked as backlog item 16.
- Notification is keyed on a state CHANGE (§3.6), so a second question asked without the task
  leaving `needs_clarification` in between is not sent to the channel. Backlog item 17.
- `dead_letters` has no retention. A permanently bad token writes one `connection` row per minute
  at the supervisor's backoff cap. Backlog item 18.
- Dead-letter redaction catches Slack `xox*`/`xapp*` and `Bearer`, not Discord bot tokens, raw
  base64 or `sk_live_`-style keys.
- Attachments are carried, not understood; images from a channel are stored, not read.
- One workspace per platform, and threads only — no DMs.

## [0.6.0] — 2026-08-30

### Added
- Routing at promotion (§5.4): `Orchestrator._promote` now routes each candidate to a real
  project instead of hardcoding `"default"`. `ProjectRouter` (a `projects` keyword argument on
  `Orchestrator`, wired in `build_orchestrator`) checks for an existing binding first — free — and
  falls back to one model call, gated at confidence 0.8, only on a miss; a miss, an unroutable
  candidate (no messages), or no router at all still produces a task in `default` rather than
  dropping the request. `projects/seeds.py::ensure_default_project` installs the `default` project
  idempotently at startup, beside `ensure_seed_workflows`, so a fresh clone always has somewhere for
  the first task to land. Routing assigns a project **per client**: memory's existing
  `TaskRow.project` scoping (0.5.0) is now a real boundary between clients' remembered specs
  wherever a binding exists — it does not, on its own, isolate every client in general, since
  anything unrouted (no confident stage-2 match, no description on the candidate project) still
  shares `default` along with its memory.
- Per-project concurrent dispatch (§3.3, §5.4): a `Dispatcher` (`orchestrator/dispatcher.py`) gives
  every project with runnable work its own worker, up to `max_concurrent_projects` (default 4) at
  once, and drives one project's tasks strictly FIFO. Each task is driven only under a lease
  (`TaskRepository.claim_lease`/`heartbeat_lease`/`release_lease`, `tasks` gains
  `lease_owner`/`lease_expires_at`/`lease_attempts`), so a worker that dies mid-flight leaves the task
  recoverable once the lease expires rather than stuck in `EXECUTING`; a task reclaimed past
  `max_lease_attempts` (default 3) fails visibly instead of being retried forever.
  `LEY_KHAA_DISPATCH=inline|workers` (default `workers`) selects the mode — `inline` drives every
  task synchronously with no lease, which is what the whole test suite pins and a
  single-operator run wants, and is a supported mode, not a test shim.
  `test_concurrency.py::test_two_projects_genuinely_run_at_the_same_time` proves, with a barrier two
  workers must both reach to unblock, that tasks in different projects genuinely run concurrently —
  a claim only this kind of test can support, since a sleep-based one could pass by accident of
  timing.
- Amendment detection (§5.9): `AmendmentDetector` (`orchestrator/amendment.py`), the same two-stage
  shape as the registry and memory matchers, decides at promotion whether a new request modifies a
  task already active in its project rather than starting a new one. `recommend_fold`
  (`autonomy/engine.py`) — the dial's fold decision — folds automatically only when the target is in
  `AUTO` mode, not already `EXECUTING`/`VALIDATING`, has no missing spec fields, and detection
  confidence is ≥0.9 (above the 0.8 detection floor, since a fold is destructive); anything short of
  that parks the candidate in a new `AWAITING_TRIAGE` state for a human. `TaskRepository.fold_into`
  merges the amendment's messages into the target and sends it back to `CLASSIFIED` for
  re-interpretation over the enlarged message set, never stapling onto the old spec.
- Triage API and dashboard: `GET /triage` lists parked amendment candidates. `POST
  /candidates/{id}/fold` resolves one, backed by the same `_fold` path the automatic route uses;
  `POST /candidates/{id}/separate` resolves the other way, promoting the candidate as its own task
  via `TaskRepository.create` — it never touches `_fold`. The dashboard's Triage tray surfaces
  these.
- Projects API and dashboard: `GET /projects` (name, display_name, description, `active`, queue
  depth, the task currently leased if any), `POST /projects` (refuses an empty description, since
  that is what stage-2 routing reasons over), `GET /projects/{name}/queue`. The dashboard's
  Projects view lists them.

### Changed
- `WorkflowRepository.record_success`/`record_failure` switched from a Python-side
  read-modify-write to an atomic `UPDATE` for `runs_ok`/`runs_failed` — real concurrency now that
  the dispatcher runs projects in parallel could otherwise lose an increment. The alias list, which
  can't be incremented in SQL, is a compare-and-swap on the value read
  (`WHERE operation_aliases == current`), retried once; a lost retry costs one extra Haiku call the
  next time that phrasing appears, never a corrupted list (closes backlog item 5). The CAS's
  equality predicate raised `UndefinedFunction` on Postgres, whose plain `json` type has no
  equality operator — invisible to the SQLite-only test suite — so `operation_aliases` moved to
  `JSON().with_variant(JSONB(), "postgresql")` (Alembic `0006_alias_jsonb`); SQLite and every other
  dialect keep plain `JSON`, unchanged.
- `TaskDriver._after_spec` now claims the `CLASSIFIED → {INTERPRETED, NEEDS_CLARIFICATION}`
  transition before writing the spec, and `save_memory_hit` (in `_interpret`) moves behind its own
  won claim, mirroring `_remember`'s pre-existing ordering (closes backlog item 6). A task that
  loses either race now carries no spec, or no memory attribution, for a path it never took, rather
  than the reverse inversion. This narrows, but does not close, a residual: the claim and the write
  are still two separate commits, so a reader can land in the gap between them — see
  `_after_spec`'s docstring for the reachability argument.
- `tasks` gains `lease_owner`, `lease_expires_at`, `lease_attempts`; `task_candidates` gains
  `amends_task_id`, `amendment_reason`, `amendment_confidence` (Alembic `0005_routing_queues`, which
  also creates the `projects` and `project_bindings` tables). `project` on `tasks` is not new — it
  has existed since Phase 0; this phase is what first writes anything other than `"default"` to it.
  The candidate `AWAITING_TRIAGE` state is a Python-level enum value on the existing `state` string
  column, not a schema change.

### Fixed
- `TaskRepository.fold_into`'s same-state (`CLASSIFIED → CLASSIFIED`) claim path had nothing making
  it mutually exclusive with a worker mid-interpretation, since it changes no state the way every
  other fold branch does — a fold could win against a task a worker already held, silently folding
  in a message the in-flight interpretation would never see while marking the candidate terminal.
  Fixed by requiring the task's lease to be free or expired too. A narrow window remains under
  `LEY_KHAA_DISPATCH=inline`, which takes no lease at all; `fold_into`'s docstring states it.
- `GET /projects` raised a `TypeError` on SQLite whenever a leased task existed: comparing a
  freshly-read naive `lease_expires_at` against an aware `now` in Python. The comparison moved into
  SQL (`TaskRepository.leased_task_id`).

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
  answer, scoped by `TaskRow.project` — as of Phase 5's routing (see 0.6.0), that project is
  assigned per client, so this scoping is a real separation between clients' memory wherever a
  routing binding exists, not merely structural. It is not blanket isolation: anything unrouted
  still shares `default`, and so does its memory.
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
