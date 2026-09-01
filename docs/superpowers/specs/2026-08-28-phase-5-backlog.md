# Phase 5 backlog — carried forward from Phase 4

Written 2026-08-28, at the close of Phase 4 (PR #5). Every item here was found during Phase 4's
task reviews or its whole-branch review, triaged as non-blocking, and deliberately deferred.

Nothing in this file is a bug report against work that failed review. These are known, located,
and costed — the point of writing them down is that a deferred finding nobody records is a silent
discard.

Ordering is by what I would do first, not by severity.

---

## 1. Memory does not learn paraphrases the way the registry learns aliases

**The asymmetry.** Spec §3.3 gives the registry a learning rule: a stage-2 (model) match that then
passes appends the new phrasing to `operation_aliases` on the **proven row**. One workflow, many
aliases. Memory does the opposite — `_remember` (`orchestrator/driver.py`) fingerprints the
*current* task's text and `MemoryRepository.record` inserts a **new row**, so one remembered
request becomes N rows, one per phrasing, each with its own `times_seen`. §3.5 never states this
difference, while both matchers' docstrings advertise "the same two stages and same contract".

**What is actually broken, stated precisely.** The §9 definition-of-done line — *"a request phrased
differently is matched by the model once, then matched free by fingerprint afterwards"* — is still
satisfied: phrasing B forks its own row, so the next B is an exact-fingerprint hit and costs
nothing. Familiarity is also correct on the exact-fingerprint path, which is every offline path and
both tested paths. Two real costs remain:

- **`times_seen` under-reports.** A request served ten times across ten wordings reports "seen 1×"
  ten times over. The dashboard badge is wrong and the dial's confidence bonus stays at its floor
  for exactly the user whose history should earn it.
- **Row fan-out feeds prompt growth.** Every distinct phrasing is another row rendered into the
  stage-2 prompt, which is what makes item 2 below load-bearing rather than a safety net.

**Shape of the fix.** Mirror the registry: give `MemoryRow` a fingerprint-alias list, or add a
`memory_fingerprints` table pointing at one `memory_id`. `_remember` recognises that the current
task arrived via `remembered_from_task_id`, adds the new fingerprint as an alias, and `_touch`es
the source row instead of inserting. `record()`'s insert path stays for genuinely new requests.
This is a schema change plus a migration, which is why it was not done under merge pressure.

**Do this before adding any doc line claiming memory learns paraphrases.** Today it does not.

## 2. The recall candidate cap is currently load-bearing, not a safety net

`RECALL_CANDIDATE_LIMIT = 50` (`persistence/memory_repository.py`) was added in Phase 4's final fix
wave, because an unbounded prompt eventually exceeds Haiku's context, `MemoryMatcher.recall`'s
blanket `except Exception` swallows the error, and memory turns itself off permanently while still
looking healthy.

The cap is correct and should stay. But while item 1 is outstanding, memory grows per *phrasing*
rather than per *distinct remembered request*, so the cap is what stops the failure rather than a
backstop against an unlikely one. Fixing item 1 makes 50 a generous bound on real requests.

**Related, and worth doing with it:** `recall`'s blanket `except Exception` is right as a policy (a
cache that fails must cost only the work it was trying to save) but it means a *systemic* failure
and a routine miss are indistinguishable in the logs. Consider distinguishing "the model call
failed" from "the model found no match" so a persistent breakage is visible as one.

## 3. No management surface for task memory

The registry has list / unquarantine / delete and a dashboard page. Task memory has none. A bad
remembered spec — or, until item 1 lands, a fan-out of ten rows all holding the same bad spec — has
no human off-switch short of SQL.

This is asymmetric with the project's own stated principle that cached behaviour should be
inspectable and revocable. A memory list endpoint plus a delete, and a panel mirroring the Registry
page, is the obvious shape.

## 4. Settle the memory-vs-registry scoping asymmetry deliberately — CLOSED (`cfe58c4`)

Memory is scoped by `TaskRow.project`; the registry is global. No document claims otherwise, so
this is not a false statement — but it is undesigned, arrived at by default rather than by
decision.

Settle it when §5.4 project routing lands, because that is the change that makes scoping mean
something. Note that, at the time of writing (the close of Phase 4), every task is created with
`project="default"` — hardcoded at `orchestrator/orchestrator.py`, the only `TaskRepository.create`
call site — so there is exactly one project and memory is shared across every client. Phase 4's
CHANGELOG and matcher comment said this plainly as of that same point; they previously claimed
client isolation the code did not provide.

**When project routing lands, re-read that claim first.** The moment tasks stop all being
`"default"`, the remembered spec's `recipient` field becomes a real cross-client concern: a spec is
reused wholesale, delivery target included.

**Closed by `cfe58c4`** (`feat(orchestrator): route each task into a project at promotion`).
`Orchestrator._promote` now routes every candidate through `ProjectRouter` instead of hardcoding
`project="default"`; memory's `TaskRow.project` scoping is a real boundary wherever a routing
binding exists. It is scoped per client, not a blanket guarantee — see the README's
[Projects and queues](../../../README.md#projects-and-queues) section for what still shares
`default`. The `recipient`-reuse concern flagged above is unaddressed and stands as its own risk,
not reopened by this closure.

## 5. Concurrency: workflow counters are read-modify-write — CLOSED (`327fc18`, hardened `8b2b320`)

`WorkflowRepository.record_success` / `record_failure` (`persistence/workflow_repository.py`) read,
modify and write `runs_ok` / `runs_failed` / `operation_aliases` with no locking. FastAPI's
sync-endpoint threadpool plus the periodic sweeper is real concurrency today, so two concurrent
cached runs of the same workflow can lose a counter increment or a learned alias.

Costs are small — counters are cosmetic, and a lost alias costs one extra Haiku call — which is why
this was deferred. The fix is an atomic `UPDATE ... SET runs_ok = runs_ok + 1`, and the alias append
wants the same treatment.

**Closed by `327fc18`** (`fix(persistence): make workflow counters atomic and claim before
writing`): `record_success`/`record_failure` switched to an atomic `UPDATE` for `runs_ok`/
`runs_failed`; the alias list, which can't be incremented, became a compare-and-swap on the value
read (`WHERE operation_aliases == current`), retried once — a lost retry costs one extra Haiku call,
never a corrupted list. **Hardened by `8b2b320`**: the CAS predicate raised `UndefinedFunction` on
Postgres (`json` has no equality operator; SQLite's untyped comparison had masked this), so
`operation_aliases` moved to `JSON().with_variant(JSONB(), "postgresql")` in migration
`0006_alias_jsonb.py` — see backlog item 9 below for why this class of bug had no automated coverage
until it reached review.

## 6. Ordering: two writes land before the state claim that authorises them — CLOSED (`327fc18`)

`save_memory_hit` (`orchestrator/driver.py`) commits before the `CLASSIFIED → INTERPRETED` claim, so
a task that loses that race permanently carries `remembered_from_task_id` / `familiarity` for a path
it did not take. It mirrors `save_spec`'s pre-existing ordering, so it is not a regression — and
`_remember` gets this right by calling only after its claim wins, which is the model to follow.

Worth fixing both together, since they are the same shape and the correct pattern already exists
two functions away.

**Closed by `327fc18`**: `save_memory_hit` in `_interpret` now moves behind the won claim, following
`_remember`'s model. `_after_spec` (`orchestrator/driver.py`) was also reordered to claim
`CLASSIFIED → {INTERPRETED, NEEDS_CLARIFICATION}` before `save_spec` — the same shape, not
originally named in this item, closed alongside it for the same reason. That reordering trades the
old spec-without-state inversion for a narrower state-without-spec window between the claim commit
and the `save_spec` commit; its docstring states the window and why it's narrow in practice
(workers mode leases the row, `advance_stalled` is inline-only, the next sweep self-heals). Closing
it for good needs the claim and the writes in one transaction — out of scope here, and not filed as
a fresh backlog item since the docstring already carries it.

## 7. Test-coverage gaps carried forward

None of these are suspected bugs; each was verified correct by reading or direct execution during
review. They are places where a regression would not turn anything red.

- **No downgrade test for any migration** (`0001`–`0006`). Verified by reading only: LIFO column
  drops, native SQLite `DROP COLUMN`; `0006`'s `alter_column` to/from JSONB likewise unexercised.
- `fingerprint_candidates`' empty-operation early return (`registry/fingerprint.py`).
- Case-insensitive suffix matching in `bind()` (`registry/binder.py`).
- `confidence == CONFIDENCE_FLOOR` exactly, as a match — only floor − 0.01 is pinned, as a miss.
- `_remember`'s own empty-fingerprint guard: dropping it is caught by no test, because
  `MemoryRepository.record`'s guard absorbs the call. The composite invariant is pinned; that one
  guard is not.

## 8. Frontend polish

- The Registry list does not refresh after a promotion made from a `BundlePanel` on the same page.
  The most user-visible item here.
- `PromoteControl`'s Cancel clears `error` but not `name` / `description`, so reopening shows stale
  input.
- `Registry`'s `load` is redefined every render (no `useCallback`).
- `BundlePanel.test.tsx` and `TaskDetail.test.tsx` mix `test(...)` and `it(...)`. Of the 7
  frontend test files that exist now (`Projects.test.tsx` and `Triage.test.tsx` added this
  phase), the rest use `test(...)` consistently.

## 9. No Postgres lane in CI, and the whole suite is SQLite-only

`.github/workflows/ci.yml` has no `services` block and no `DATABASE_URL` — the backend job installs
the package and runs `pytest` with whatever `DATABASE_URL` default the code falls back to.
`backend/tests/conftest.py` builds every test's schema with `Base.metadata.create_all()` against
SQLite (`sqlite://` in-memory for the shared `session` fixture, a file-backed `sqlite:///...` for
`session_factory`). So every dialect-dependent defect is invisible to all 632 tests *and* to CI —
while `docker compose up`, the demo path, runs Postgres 16 (see `docker-compose.yml`). This is
exactly what let this phase's `operation_aliases` bug (backlog item 5's hardening, `8b2b320`) reach
review: `WHERE operation_aliases == current` on a plain `json` column raises `UndefinedFunction` on
Postgres and works fine on SQLite, and nothing that runs automatically exercises Postgres.

Two further things this same gap hides, both confirmed by hand for this entry:

- **The migration drift guard (`test_migrations_match_the_models`, `test_migrations.py`) also runs
  on SQLite only.** It upgrades a throwaway `sqlite:///` database to head and diffs it against
  `Base.metadata` with `compare_metadata`. The `with_variant(JSONB(), "postgresql")` half of
  `operation_aliases`'s ORM declaration (`persistence/orm.py`) is a Postgres-only branch this guard
  never dialect-switches into, so it has no automated coverage anywhere — a future edit that broke
  the Postgres variant specifically (wrong type, a typo in the dialect string) would pass every test
  and CI both.
- **`compare_metadata` against a live Postgres shows a pre-existing diff**, independent of this
  phase's changes: `[('remove_constraint', UniqueConstraint(Column('name', ..., table=<workflows>)))]`
  — an autogenerate artifact of `unique=True` together with `index=True` on `WorkflowRow.name`
  (`persistence/orm.py`), not a real schema gap (the unique index it wants removed is the same
  constraint SQLAlchemy's own comparator is comparing against). Confirmed present running
  `compare_metadata` at revision `0005_routing_queues` against a real `postgres:16` container, i.e.
  before `0006_alias_jsonb` existed — so it predates this phase and is not introduced by it, but it
  is also invisible to the SQLite-only guard that would normally catch drift like this.

**Shape of the fix.** Add a `postgres:16` service to the CI backend job (`services:` + a
`DATABASE_URL` pointed at it, mirroring `docker-compose.yml`'s `db` service) and run the suite a
second time against it, or at minimum run `test_migrations_match_the_models` and the
counter/alias-CAS tests against it. That also gives the pre-existing `UniqueConstraint(workflows.name)`
autogenerate diff a place to get fixed deliberately (an explicit named constraint in the migration,
matching what the comparator expects) instead of continuing to pass silently everywhere it is
currently checked.

## 10. A false comment and a redundant read in `workflow_repository.py`

`WorkflowRepository.record_success` (`persistence/workflow_repository.py`, the comment block just
before its final `cached = self.get(name)`) justifies an extra `SELECT` plus `session.expire()` with:
"Bulk UPDATE bypasses the unit of work: it does not touch the identity map, so the caller's own
in-session copy of this row (if one was already loaded) still shows the pre-update values until
something expires it." That is not true of SQLAlchemy 2.0's ORM-enabled bulk update: `session.execute
(update(WorkflowRow)...)` defaults to `synchronize_session="auto"`, which does synchronize matching
objects already in the identity map (evaluating the criteria in Python where it can, falling back to
a fetch strategy otherwise) — the premise the comment is defending against does not hold as stated.
`record_failure` carries the same redundant read with no comment at all.

Removing the `cached = self.get(name); if cached is not None: self.session.expire(cached)` block from
both methods (and returning `self._row(name)` directly, as they already do at the end) leaves all 632
tests, including the identity-map-focused tests this phase added for the alias CAS, passing
unchanged — the extra `SELECT` was defending against a case that either doesn't happen or is already
handled by `synchronize_session`. It is dead defensive code justified by a false premise, costing one
extra `SELECT` per `record_success`/`record_failure` call. Verifying that removal is safe beyond "the
existing tests don't notice" — i.e. actually pinning what the identity map looks like after the bulk
UPDATE, with and without the expire — is itself worth a test before the removal ships, since the
comment being wrong about SQLAlchemy's default behavior is exactly the kind of thing "no test failed"
alone doesn't prove.

**Shape of the fix.** Delete the false comment and the `cached =` / `expire()` lines from both
methods; add a small test that loads a `WorkflowRow`, calls `record_success`/`record_failure` on it
through the same session, and asserts the in-session object's `runs_ok`/`runs_failed` already reflect
the write with no explicit `refresh()` — pinning the identity-map-sync behavior the (now-removed)
comment misdescribed, rather than leaving it to be rediscovered by reading the SQLAlchemy source
again.

## 11. A project drains one task per tick, and one slow project paces every other

Spec §3.3 describes the worker loop as: *"On return it releases the lease and loops."* It does not.
`Dispatcher._work_one` (`orchestrator/dispatcher.py`) claims one task, drives it, releases the lease
and **returns** — no loop — and `tick()` re-derives the runnable projects from scratch on the next
pass. So a project drains exactly one task per tick, and the next tick is a whole
`LEY_KHAA_SWEEP_SECONDS` (default 15s) away.

`tick()` also `gather`s every project it started and waits for all of them before returning, so the
slowest project sets the pace for the rest: measured with the interval pinned to 0 to isolate the
per-tick cost, four instant tasks queued behind a project whose own tasks take 1.5s each took
**6.10s** to drain (5 ticks) instead of finishing at once. With the real 15s interval that same
backlog is a minute of wall clock for four no-ops.

**Deliberately not fixed in the Phase 5 fix wave.** Nothing here is incorrect — the FIFO ordering,
the lease, the poison cap and the concurrency guarantee all hold — it is a throughput ceiling, and
changing the dispatcher's shape under merge pressure is exactly the sort of edit that turns a
correct-but-slow queue into a fast-but-racy one. The README's projects/queues section now states
the pacing so "each project drains through its own worker" is not read as unbounded throughput.

**Shape of the fix.** Make `_work_one` loop until `_claim_next` returns None, so a project drains
its whole backlog under one tick, and let each project's lane finish independently of the others
(`tick()` returning once every lane is *started*, with the gather moved into a per-project task, or
a long-lived per-project worker task instead of a tick-driven one). Both need the lease heartbeat
and the max-concurrency semaphore re-checked against the new lifetime, and a test that a fast
project is no longer paced by a slow one — which is why it is its own piece of work.

## 12. Nothing tests `HeuristicLLM`'s `ProjectChoice` rule, because a blanket `except` launders it

Deleting the `if output_format is ProjectChoice:` branch from `HeuristicLLM.parse`
(`llm/heuristic.py`) leaves all tests green. `parse` then falls through to its final
`raise NotImplementedError`, and `ProjectRouter.route`'s `except Exception`
(`projects/router.py`) catches it and returns the same `_fallback` the deleted rule produced —
only the `reason` string differs, and nothing asserts on that.

So spec §3.5's stated justification for the rule — *"`HeuristicLLM` gains a deterministic `_route`
so CI and `docker compose up` stay green with no `ANTHROPIC_API_KEY`"* — is verified by no test at
all. This is the **fourth** instance of the same pattern in this codebase (Phase 4's registry
stand-in test asserting *through* a blanket `except Exception`; `MemoryMatcher.recall`; the
detector's own `except` in `orchestrator/amendment.py`), which is what makes it worth a numbered
item rather than a footnote: a blanket `except` around a stage that has an offline stand-in makes
the stand-in untestable by observation of the result alone.

**Shape of the fix.** Assert on the thing the two paths do NOT share. Either pin
`decision.reason` / `decision.stage` for the offline route (so the laundered `NotImplementedError`
produces a different, failing reason), or — better, and reusable for the other three — assert that
`HeuristicLLM.parse` returns a `ProjectChoice` directly, with no router in the way, the way
`test_heuristic_llm.py` already does for the formats it does cover.

## 13. `ProjectIn.name` accepts anything at all

`POST /projects` validates the description (a project with no description is unroutable by stage 2,
so the route refuses it) but not the name. `""`, `"  "`, `"../etc"`, `"Default Project!"` and a
300-character name all return **201**. Its sibling `PromoteIn` validates against a shared
`NAME_PATTERN` (`api/schemas.py`), so the pattern and the precedent both already exist.

Two concrete costs:

- An empty name becomes a primary key that `GET /projects//queue` cannot address — the row exists
  and is unreachable through the API that lists it.
- A whitespace-only name with a real description is offered to stage-2 routing as a project the
  model can name, so the router can legitimately route work into a project nobody can see.

**Shape of the fix.** Apply `NAME_PATTERN` (or a slug-shaped variant) to `ProjectIn.name` and add
the length bound, matching `PromoteIn`. Note this is validation only, not a schema change — and
existing rows created before it, if any, are not retroactively invalid.

## 14. Spec §8's "the whole suite green on both dispatch modes" is unachievable by construction — CLOSED (`34b144e`)

`backend/tests/conftest.py` sets `os.environ["LEY_KHAA_DISPATCH"] = "inline"` **unconditionally**
(not `setdefault`), before any application module is imported, so the suite cannot be run in
`workers` mode at all: `LEY_KHAA_DISPATCH=workers pytest` runs inline. §3.4's weaker wording —
workers mode covered on its own terms, by `test_dispatcher.py`, `test_concurrency.py` and
`test_dispatch_modes.py` — is what actually shipped, and is the honest description.

This is a documentation correction, not a code change: making the pin a `setdefault` would let a
`workers` run happen, but the existing suite asserts throughout on tasks that have already run, so
that run would fail for reasons that are about the fixtures, not about the code. §8 should be
corrected to match §3.4.

**Closed by `34b144e`:** §8 now reads "green with `inline` pinned and workers mode covered on its own
terms", and carries a note recording what the line used to claim and why it was false. The
`setdefault` change is deliberately still NOT made — the reasoning above stands.

## 15. `project_queue` and `queue_depth` disagree by design, and nothing says so

Two numbers are shown to a human under the word "queue" and they count different things:

- `GET /projects/{name}/queue` (`api/app.py::project_queue`) returns **every** task in the project,
  `DONE` and `FAILED` included — it is a project's task list, not its queue.
- `ProjectOut.queue_depth` is `TaskRepository.runnable_count`, which excludes terminal and
  human-waiting states **and** the task currently under a live lease (which is reported separately
  as `in_flight`).

Both are individually correct and each is the right number for its own caller. Neither the field
name, the route name, nor the README says they differ, so a dashboard showing "queue_depth: 1"
beside a queue listing of nine rows looks like a bug.

**Shape of the fix.** Cheapest: document it — one sentence in the README's projects/queues section
and a docstring on `project_queue`. Better: rename the route to `/projects/{name}/tasks` (with
`/queue` kept as an alias) or give it a `?runnable=true` filter so the two words mean one thing.

---

## The process lesson worth carrying into Phase 5

Phase 4 produced **eight separate findings of tests that passed for the wrong reason**: a fixture
already in sorted order, so a `sorted()` regression could not fail it; an assertion comparing
`recommend(spec, familiarity=0)` against `recommend(spec)`, which are the same call because 0 is the
default; a test claiming to exercise a race that took the ordinary path; an offline stand-in test
asserting *through* a blanket `except Exception` that swallowed the very `NotImplementedError` it
existed to catch; a negative frontend assertion whose query never matched anything; and a
behavioural fix shipped with no test at all.

The practice that catches them is mechanical, and it should be demanded in every dispatch and
verified in every review: **delete the behaviour the assertion guards, watch the test fail for the
right reason, restore it.** Self-reported "I mutation-tested it" was wrong more than once this
phase, so the claim needs checking, not accepting.

A second one, from the same phase: **a plan's reasoning can be wrong, not just its code.** Phase 4's
plan justified a safety property with "the bonus (0.15) is smaller than one missing field's penalty
(0.2)" — which does not follow, because that is a relative relation and the AUTO threshold is
absolute. Executing it showed a `certainty=1.0` spec with a known gap reaching AUTO. Pre-scanning
each task brief against the real code before dispatching caught that and four more brief defects.

## 15. A poisoned task fails without telling anyone

**What is broken.** `Dispatcher._fail_poison` moves a task to FAILED when it has outlived
`max_lease_attempts` workers. It does that with a bare `TaskRepository`, and the notifier lives on
`TaskDriver` — so this is the one path to FAILED that does not notify. Everything else does
(`advance()`'s single exit point, and `reject()` explicitly). §8's "`done` and `failed` notify" is
therefore true of every path a human is likely to hit and false of exactly this one, which is
recorded in 0.7.0's known limits rather than left to be discovered.

**Shape of the fix.** Inject an `announce: Callable[[Session, str], None]` into `Dispatcher`, bound
in `api/app.py` to something that builds the orchestrator for that session and calls
`driver._announce(repo.get(task_id))`. That keeps the dispatcher ignorant of the driver, the
notifier and FastAPI, which is why it is not simply given a `TaskDriver`.

**Why it was deferred.** Phase 6 widened the review surface more than any phase so far — first
outside world, first credentials — and this adds a constructor parameter to the one component whose
concurrency correctness the whole queue rests on.

## 16. A second clarifying question is never delivered

**What is broken.** `TaskDriver._announce` guards re-notification with
`mark_notified(row.id, row.state)` — a compare-and-swap on the STATE string. There are three
writers of `open_question` and three transitions into `NEEDS_CLARIFICATION`
(`_MAX_CLARIFICATION_ROUNDS` is 3), so a task that arrives at `NEEDS_CLARIFICATION` a second time
with no notifying state in between is silently skipped. The harmful variant: `_gate` goes
`INTERPRETED → EXECUTING` directly when the effective mode is `auto`, so `AWAITING_APPROVAL` never
intervenes, and `_validate`'s failing branch re-parks the task with a DIFFERENT question. The run
failed, a human is blocked, and the channel is silent.

Spec §3.6 sanctions the mechanism verbatim ("notify only when the state differs from what was last
announced"), so code and spec agree — what was wrong was §8's and the README's unqualified claim
that the question comes back in the thread. Those are now qualified rather than left overstated.

**Shape of the fix.** Key the CAS on the announced *content*, not the state: store a hash of the
message in `last_notified_state`, or add a `last_notified_question` column. A NEW question then
re-arms the guard while a re-drive in the same state still does not.

**Why it was deferred.** It is a schema change to the exact column whose compare-and-swap stops a
re-entrant `advance()` spamming the channel, landed at the end of the phase that introduced it.

## 17. `dead_letters` has no retention

**What is broken.** `MAX_PAYLOAD_CHARS` bounds row SIZE, not row COUNT, and nothing prunes. A
permanently bad token makes `AdapterSupervisor._supervise` crash-loop at its 60s backoff cap,
writing one `connection` row every minute for as long as the process runs.

**Shape of the fix.** Coalesce repeated identical connection failures (a `count` plus `last_seen`
on one row), which is better than time-based pruning here — the point of the table is that a drop
stays visible, and a retention window would quietly delete evidence.

**Why it was deferred.** Not a correctness bug, and the dashboard makes the crash-loop obvious long
before row count matters.

**18 is reserved.** PR #9 renumbers this file's duplicate `## 15.` (the two sections above with that
number) to 15/16/17/18; that renumbering is not repeated here to avoid colliding with it. The three
items below are numbered 19–21 on the assumption PR #9 has merged.

## 19. An unfetchable or undecodable image is retried on every drive, and dead-lettered every time

**What is broken.** `VisionExtractor.extract` (`vision/extractor.py`) keys its cache on
`sha256(image_bytes)`. When `_bytes_for` raises — the fetch was refused, the host is not
allowlisted, the body exceeded the size cap, or a base64 payload failed to decode — there are no
image bytes to hash, so the extractor returns an `_unread()` record built directly as an
`ImageExtractionRow`, never passed through `ImageExtractionRepository.record`. Nothing is written.
The next drive of the same task reaches the same URL, fails the same way, and
`_record_drop` writes a second `dead_letters` row for the identical failure — a third drive writes
a third.

**Root cause.** The cache key IS the image's own bytes. A source that never produced bytes has
nothing to key a "do not try again" record on, so there is nowhere to store the refusal.

**Shape of the fix.** A second key space for unfetchable sources — e.g. a row keyed on the
attachment's URL (or its own hash) rather than the image digest, with its own frozen-refusal
semantics. Deferred rather than done under phase pressure: it is a second cache with its own
invalidation question, not a one-line change to the existing one.

## 20. A same-backend model failure stays frozen under that image's digest forever

**What is broken.** When `self.llm.extract_image(...)` raises — a transient 503, a rate limit, a
timeout — the extractor DOES store the failure, credited to the backend that produced it
(`model=getattr(self.llm, "name", "")`, e.g. `"anthropic"`). The cache-hit check in `extract()` then
reads:

```python
if cached.content or (cached.model and cached.model == current_model):
    return cached
```

A stored failure with `model == current_model` short-circuits forever. There is no distinction
between "this backend will never manage to read this image" and "this backend hit one bad
response".

**This is deliberate, not an oversight.** The same check is what makes every *configuration*
change re-extract for free — offline to online, a disabled vision path turned on, one backend
switched for another all show up as `cached.model != current_model` (or `cached.model == ""`) and
retry automatically, with no operator action. Widening that same check to also retry a same-backend
transient error would require distinguishing "different config" from "same config, unlucky call",
which the single `model` column cannot express.

**Shape of the fix.** A retry-count (or a `last_failed_at`) column, with a small bounded-retry
policy on top of the existing model-mismatch rule. A schema change, deferred to the user's design
call rather than decided under this phase's pressure.

## 21. Vision will not work on the offline Ollama path (0.9.0)

**What is broken, stated before anyone discovers it the hard way.** The Ollama fallback planned for
0.9.0 (§11, "not started") is scoped as a **text** offline model. `LLMClient.extract_image` has
three implementations today — `AnthropicLLM`, `HeuristicLLM`, and the test double `FakeLLM` — and an
Ollama-backed `LLMClient` due in 0.9.0 will need a fourth, satisfied by a model that can actually see
an image. A text-only local model cannot; it can only produce the same carried-not-read shape
`HeuristicLLM.extract_image` already produces today (name the image, read nothing).

**Shape of the fix.** A vision-capable local model (e.g. a multimodal Ollama tag) is the roadmap
item; nothing about this phase's `VisionExtraction` contract, the fetcher, or the cache needs to
change for it — only a fourth `LLMClient` implementation that actually looks at the bytes.

**Why it is filed now rather than left implicit.** 0.9.0 is scoped as "offline fallback", and
"offline" reads as "the same features, no network" unless stated otherwise. It is not, for images,
and saying so now costs one paragraph against discovering it after the fact.
