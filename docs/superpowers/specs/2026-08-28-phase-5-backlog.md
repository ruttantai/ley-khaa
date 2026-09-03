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

**Deliberately deferred past 1.0.0** (Phase 9 design spec §7). This is a **feature** — a
`memory_fingerprints` table or a fingerprint-alias list on `MemoryRow`, plus a migration — not a
defect: the §9 definition-of-done line it might appear to threaten is still satisfied, as this entry
already establishes. The registry learns aliases and memory does not; that is a capability gap, and
shipping 1.0.0 with it open and written down is honest, while rushing a schema change into the
release-hardening phase is how the last three phases produced their defects. **Nothing has been added
claiming memory learns paraphrases**; the README and CHANGELOG still say what it actually does.

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

**Deliberately deferred past 1.0.0** (Phase 9 design spec §7). The cap is correct and stays;
what is deferred is *changing* it, because the right value is an empirical question — how many rows a
real project accumulates, against Haiku's context — and there is no measurement to answer it. Picking
a new number without one would be a guess wearing a fix's clothes. Note the coupling: while item 1 is
open the cap is load-bearing rather than a backstop, so these two defer together and item 1 is what
makes 50 generous.

## 3. No management surface for task memory

The registry has list / unquarantine / delete and a dashboard page. Task memory has none. A bad
remembered spec — or, until item 1 lands, a fan-out of ten rows all holding the same bad spec — has
no human off-switch short of SQL.

This is asymmetric with the project's own stated principle that cached behaviour should be
inspectable and revocable. A memory list endpoint plus a delete, and a panel mirroring the Registry
page, is the obvious shape.

**Deliberately deferred past 1.0.0** (Phase 9 design spec §7). A **feature**: a memory list
endpoint, a delete, and a dashboard panel — new API surface and new UI, not a fix to something that is
wrong. The asymmetry with the registry is real and stays recorded; a release-hardening phase is the
wrong place to add a surface, and 1.0.0's known limits name it rather than hiding it.

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

## 7. Test-coverage gaps carried forward — CLOSED (`21025b6`)

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

**Closed by `21025b6`** (`test(registry,memory,migrations): close backlog item 7's
carried-forward gaps`). Four of the five gaps were still real and now have mutation-verified tests:

- **Migration downgrades.** `test_migrations.py` steps every revision down to `base` and asserts no
  table is left behind, and separately round-trips head → *each* revision → head, re-running
  `compare_metadata` at every stopping point. Parametrised over every stopping point because a single
  one only discriminates revisions whose leftovers survive down to it — stopping only at
  `0001_baseline` catches a no-op `0009` downgrade but **not** a no-op `0010`, since `0008`'s
  downgrade drops `image_extractions` and takes the leftover column with it. Mutation sweep over all
  ten downgrades, each replaced by `pass` and the whole file run: **nine of ten turn it red**
  (`0001` 2 failed, `0002` 1, `0003` 2, `0004` 5, `0005` 6, `0007` 8, `0008` 9, `0009` 8, `0010` 2).

  **`0006_alias_jsonb` is the tenth, and it stays unexercised — 14 passed with its downgrade
  deleted.** Its `alter_column` moves between `sa.JSON()` and `JSON().with_variant(JSONB(),
  "postgresql")`, which render identically on SQLite, so no SQLite test can discriminate it. This
  entry originally listed it as a gap and it is *still* a gap; what closed is the rest. It is
  covered by item 26, not here — stating that is the point, since a guard that appears to cover a
  type change it cannot see is worse than one that admits the gap.
- **`fingerprint_candidates`' empty-operation early return.** Without it, a spec naming no operation
  matched any workflow whose aliases hold no alphanumerics.
- **`confidence == CONFIDENCE_FLOOR` exactly, as a match**, for the registry and memory matchers
  both. Only floor − 0.01 was pinned, so `<` and `<=` were indistinguishable.
- **`_remember`'s own empty-fingerprint guard.** A spy repository asserts `record` is never called
  at all — the composite invariant test passes with the driver's guard deleted, because
  `MemoryRepository.record`'s own guard absorbs the call, which is exactly what this entry said.

The fifth — **case-insensitive suffix matching in `bind()` — had closed itself** before this phase.
`test_suffix_matching_is_case_insensitive_declared_uppercase` and `_declared_mixed` cover both
`.lower()` calls; confirmed by mutation rather than by reading. All of `test_migrations.py` runs on
SQLite only — see item 26 below, which is where `0006`'s type change would finally get a lane.

## 8. Frontend polish — CLOSED (`bf9b27f`)

- The Registry list does not refresh after a promotion made from a `BundlePanel` on the same page.
  The most user-visible item here.
- `PromoteControl`'s Cancel clears `error` but not `name` / `description`, so reopening shows stale
  input.
- `Registry`'s `load` is redefined every render (no `useCallback`).
- `BundlePanel.test.tsx` and `TaskDetail.test.tsx` mix `test(...)` and `it(...)`. Of the 7
  frontend test files that exist now (`Projects.test.tsx` and `Triage.test.tsx` added this
  phase), the rest use `test(...)` consistently.

**Closed by `bf9b27f`** (`fix(dashboard): refresh the registry after a same-page promotion
(item 8)`), all four bullets. A refresh signal is lifted into `App` and threaded down through
`TaskDetail` → `BundlePanel` → `PromoteControl`, mirroring the existing `onChanged` callback, so a
promotion made from a `BundlePanel` reaches the Registry list; `Registry`'s `load` is memoised with
`useCallback`, since an unmemoised `load` in that effect's dependency array would refetch on every
render; `PromoteControl`'s Cancel now clears `name` and `description` as well as `error`; and
`BundlePanel.test.tsx` and `TaskDetail.test.tsx` were converted to `test(...)` throughout, so all
eight frontend test files agree.

## 9. No Postgres lane in CI, and the whole suite is SQLite-only — CLOSED (`353ee59`, `264249e`, hardened `f3738ed`)

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

**Closed by `353ee59`** (`test(conftest): run the whole suite on Postgres when DATABASE_URL
is set`) **and `264249e`** (`ci(backend): add the Postgres lane, and let mypy fail fast`). Both
fixtures now build on whatever `DATABASE_URL` names; with it unset the SQLite lane is unchanged and
still needs nothing installed. Isolation on the Postgres lane is a dedicated `ley_khaa_test` schema,
dropped and recreated once per session, plus `TRUNCATE … CASCADE` before each test — chosen over a
rolled-back per-test transaction, which cannot span the two independent connections `session_factory`
hands the concurrency tests. CI gains a `postgres:16` service mirroring `docker-compose.yml`'s `db`
and a second `pytest` step against it. `0a39ae8` fixed the one thing the new lane immediately found:
a leaked session in `test_task_leases.py` holding `ACCESS SHARE` and blocking the next test's
cleanup — invisible on SQLite.

**Hardened by `f3738ed`**: the lane was inferred from `DATABASE_URL` alone, so deleting or
misindenting CI's `env:` block would have re-run SQLite, printed the same pass count a second time
and gone green having never touched Postgres — this file's signature defect, inside the fix for it.
`--database=sqlite|postgres` states the expectation on the command line, where it survives anything
that happens to the step's environment, and refuses the run otherwise.

**Two things this entry named are NOT closed and are re-filed rather than quietly dropped:**

- The **migration drift guard still runs on SQLite only** — `test_migrations.py` builds its own
  `sqlite:///` URLs and is untouched by `DATABASE_URL`. Filed as item 26.
- The **pre-existing `('remove_constraint', UniqueConstraint(workflows.name))` autogenerate diff on
  Postgres is still there**, re-confirmed for this closure by upgrading a throwaway `postgres:16`
  database to head and running `compare_metadata` against it. (Also confirmed in the same run:
  `upgrade head` and `downgrade base` both succeed on Postgres.) It is carried by item 26, which is
  where it would get fixed deliberately.

## 10. A false comment and a redundant read in `workflow_repository.py` — CLOSED (`3aaa59f`)

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

**Closed by `3aaa59f`** (`fix(api,registry): three small hardening items — 10, 13, 15`).
The `cached = self.get(name)` / `session.expire(cached)` block and its false comment are gone from
both `record_success` and `record_failure`. The premise was checked by direct probe rather than by
"no test failed", as this entry asked: SQLAlchemy's default `synchronize_session` already keeps an
already-loaded identity-map row in sync as part of the bulk UPDATE, so both the read and the expire
were dead. A test now pins that sync invariant, so the behaviour the removed comment misdescribed is
asserted rather than left to be rediscovered by reading the SQLAlchemy source again.

## 11. A project drains one task per tick, and one slow project paces every other — CLOSED (`a45253d`, hardened `1b835ad`)

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

**Closed by `a45253d`** (`fix(dispatcher): drain a project's whole backlog per tick`).
`_work_one` now loops claim/drive/release until `_claim_next` returns None and returns every task id
it drove, and `tick()`'s collection loop extends rather than appends. Termination is guarded by a
per-tick "attempted" set: `release_lease` does not touch `lease_attempts`, so a task whose drive
keeps failing would otherwise be reclaimed forever and starve its own project's queue.

**Hardened by `1b835ad`**: review found the per-tick drain did not by itself remove head-of-line
blocking. `_claim_next` collapsed three distinct "return None" cases into one — nothing runnable, the
head task just poison-failed, and the head task lost a claim race — and only the first means the lane
is empty; the other two left everything behind the head undriven for the whole tick.
`next_runnable` gained `exclude_ids` so `_claim_next` can step past an unusable head. The
per-project semaphore also wrapped a whole drain, turning task-level blocking into project-level
blocking under contention; the slot is now taken and released once per claim-drive-release cycle.

**Hardened again by `4e6eccf`**: the whole-branch review found a THIRD head-of-line case, in the
termination guard `a45253d` introduced above. That guard was right that a task whose drive keeps
failing must not be reclaimed in a tight loop — but ending the whole lane was the wrong way to stop,
and the paragraph above describing it did not notice that it starved the queue in the process, which
is this very item's own symptom. Nor could a later tick or the attempt cap recover: `release_lease`
leaves `lease_attempts` untouched, so a cleanly released head is the oldest runnable row again on
every subsequent tick, for ever. `attempted` is now passed down to `_claim_next` as `exclude_ids` —
the third use of the mechanism `1b835ad` added — so the drain steps past an already-attempted head
in SQL instead of stopping at it. Termination is unchanged: each iteration still adds exactly one
NEW id to a set the query excludes. The guarantee this item closes is therefore stated as **one
attempt per task per tick**, not "until nothing runnable is left".

## 12. Nothing tests `HeuristicLLM`'s `ProjectChoice` rule, because a blanket `except` launders it — CLOSED (`d3d7f4f`)

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

**Closed by `d3d7f4f`** (`test(llm,projects): pin the offline ProjectChoice rule the router
launders`). Both halves of the shape-of-the-fix above, since they pin different things.
`test_heuristic_llm.py` asks `HeuristicLLM.parse` for a `ProjectChoice` **directly, with no router in
the way**, which is the reusable form — there is no `except` to launder a `NotImplementedError` into
a plausible default. `test_project_router.py` additionally pins `decision.reason` and
`decision.stage` for the offline route, because `reason` is the *only* field that differs between the
rule's answer and the laundered one (both are `project="default"`, `stage="default"`, confidence 0.0),
which is precisely why every other assertion in that file passed either way.

Mutation-verified over both whole files: with the `if output_format is ProjectChoice:` branch
deleted, the direct test raises `NotImplementedError` and the router test reads
`"routing failed" != "offline: no model routing"`. The three sibling blanket-`except` sites this
entry names (`MemoryMatcher.recall`, `orchestrator/amendment.py`, the Phase 4 registry stand-in) are
not touched here; the pattern to copy is now written down and exercised.

## 13. `ProjectIn.name` accepts anything at all — CLOSED (`3aaa59f`)

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

**Closed by `3aaa59f`** (`fix(api,registry): three small hardening items — 10, 13, 15`).
`ProjectIn.name` now carries the same `NAME_PATTERN` guard and length bound as its sibling
`PromoteIn`, so `""`, `"  "`, `"../etc"` and a 300-character name are 422s rather than rows that are
created and then unreachable. Validation only, as this entry noted — no schema change, and any row
created before it is not retroactively invalid.

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

## 15. `project_queue` and `queue_depth` disagree by design, and nothing says so — CLOSED (`3aaa59f`)

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

**Closed by `3aaa59f`** (`fix(api,registry): three small hardening items — 10, 13, 15`), by
the "better" option rather than the cheapest: `GET /projects/{name}/queue` is renamed
`GET /projects/{name}/tasks`, which is what it always returned, and both sides are documented. The
rename was checked against the frontend first — no dashboard code called the old route, only the
`queue_depth` field — so it touched one backend test caller and nothing else. `queue_depth` keeps its
name and its meaning; the two words no longer collide.

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

## 16. A poisoned task fails without telling anyone — CLOSED (`630de9a`, pinned `84c402c`)

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

**Closed by `630de9a`** (`fix(dispatcher): notify when a poisoned task is failed`).
`Dispatcher` takes an optional `notifier=`, defaulting to `NullNotifier` exactly as `TaskDriver`
does, so every existing construction is untouched; `_fail_poison` announces through it after the
FAILED claim, mirroring `_announce`'s claim-then-send shape (`mark_notified` guards a double
announce, and a failure to notify is logged, never propagated). The live notifier is wired at
`build_dispatcher()` in `api/app.py`.

The shape differs from the `announce: Callable` this entry proposed, and the reason is worth
recording: unlike `build_orchestrator`, which re-reads `current_notifier()` on every call, the
dispatcher is built **once** at startup and holds what it was given for its whole life — so the
workers-mode startup had to move after the block that installs the real `ChannelNotifier`, or the
fix would have frozen on the startup `NullNotifier` and been silently dead in production.
**`84c402c` pins that ordering**, which nothing did: every existing test either ran inline or stubbed
adapters to an empty list, so the one combination that exercises the bug was never covered.

## 17. A second clarifying question is never delivered — CLOSED (`4f76a4b`)

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

**Closed by `4f76a4b`** (`fix(notify): deliver a second, different clarifying question`),
taking the "add a column" half of the shape-of-the-fix. `tasks.last_notified_question` joins
`last_notified_state` in `mark_notified`'s compare-and-swap, compared NULL-safely through
`coalesce()` so an unchanged (possibly NULL) question still suppresses a repeat — which is the
property the guard exists for. A new question re-arms it; a re-drive in the same state with the same
question still does not.

## 18. `dead_letters` has no retention — CLOSED (`094309b`, hardened `9fcfe23`)

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

**Closed by `094309b`** (`fix(dead-letters): bound dead_letters row COUNT`), by a count cap
rather than the coalescing this entry proposed — same goal, and it does not need repeated failures to
be recognisably identical. `DeadLetterRepository._prune()` runs inside `record()`'s own transaction
and keeps the newest `settings.dead_letter_max_rows` (`LEY_KHAA_DEAD_LETTER_MAX_ROWS`, default 1000),
using the same `created_at`/`id` tiebreak `list()` uses. Newest rather than oldest deliberately: a
reader investigating an incident needs what just happened.

**Hardened by `9fcfe23`**: `LEY_KHAA_DEAD_LETTER_MAX_ROWS=0` parses, and is the natural thing to try
when an operator means "disable retention" — with no clamp, `_prune()` deleted every row on every
write *including the one `record()` had just written*, crashing `session.refresh(row)` inside the
notifier's own exception handler. That turns a handled notification failure into an unhandled one, in
the table whose entire purpose is that a failure is never silent. `max(1, …)` is applied at the point
of use, so it covers a value set at runtime and not only one read from the environment.

## 19. An unfetchable or undecodable image is retried on every drive, and dead-lettered every time — CLOSED (`aa62c66`)

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

**The same missing row is also what made the catalog fallback silent (whole-branch review,
finding B1).** A frozen checkpoint IS the image's own bytes-derived digest, so once a channel CDN
URL expires (Discord's do so in about a day), `_bytes_for` can no longer even re-derive the key that
would have found the stored extraction — there is no url-keyed row to fall back on. Before B1's fix
that dead end let the input name fall through to `catalog.resolve_name(...)` and compute on the
synthetic demo dataset, with the manifest attesting a clean `source: "catalog"` and no hint an image
was ever involved. B1 closed the silence, but only for the case where the image's own filename
shares a token with the input it could be satisfying — that case now raises `UnresolvedInputs`
instead of reaching the catalog. A generically- or auto-named image (`image.png`, a macOS
`Screenshot ....png`) isn't recognized as any particular input, so the run still proceeds on catalog
data even after B1; the difference is that B2's manifest `images` block now names the unread image
explicitly, so that substitution is no longer silent even when it happens. The underlying limit
this item describes — a re-fetch is required to even ask "have I seen this one before" — is
unchanged and is still the thing worth fixing here.

**Shape of the fix.** A second key space for unfetchable sources — a row keyed on the attachment's
URL (hashed to a `url_sha256`, since raw Slack/Discord URLs can be long and carry query tokens)
rather than the image digest, with its own frozen-refusal semantics. That url→digest secondary key
is also the durable fix for B1's underlying limit: it is what would let a re-drive recognize an
already-frozen checkpoint even after the source URL has expired, rather than merely asking a human
instead of guessing (which is as far as this phase's fix goes). Deferred rather than done under
phase pressure: it is a second cache with its own invalidation question, not a one-line change to
the existing one.

**Closed by `aa62c66`** (`fix(vision): record an unfetchable image once, not on every
drive`), by exactly the second key space this entry proposed: `image_extractions.url_sha256`
(migration `0010_url_sha256`), hashed from the source string rather than storing raw Slack/Discord
URLs with their query tokens. A hit suppresses the duplicate dead letter; nothing here skips
retrying the real fetch, so a URL that becomes fetchable again recovers on the next drive for free.

Note what this does **not** close: the durable half of B1 named above. `url_sha256` records the
*refusal*, but a frozen *successful* extraction is still keyed on the image's own bytes, so an
expired CDN URL still cannot be resolved back to it. That remainder is **re-filed as item 32** rather
than left inside a closed entry — the README's Images section points there, not here.

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

**Deliberately deferred past 1.0.0** (Phase 9 design spec §7). Needs a retry-count (or
`last_failed_at`) column, and — the part that is not the engineer's to decide — a **policy**: how many
retries, over what window, before a same-backend failure is treated as permanent. As this entry
already says, the current check is deliberate and buys free re-extraction on every configuration
change; a schema change plus a policy call is the user's decision, not a hardening task.

## 21. Vision does not work on the offline Ollama path

**What is broken, stated plainly.** The Ollama fallback shipped in 0.9.0 as a **text** offline model.
`LLMClient.extract_image` has four implementations now — `AnthropicLLM`, `HeuristicLLM`, `OllamaLLM`,
and the test double `FakeLLM` — and `OllamaLLM.extract_image` is written, but it does not read the
image: it returns the same carried-not-read shape `HeuristicLLM.extract_image` already produces (name
the image, read nothing), because a text-only local model cannot see an image at all.

**Shape of the fix.** A vision-capable local model (e.g. a multimodal Ollama tag) is the roadmap
item; nothing about the `VisionExtraction` contract, the fetcher, or the cache needs to change for
it — only `OllamaLLM.extract_image` (or a further `LLMClient` implementation alongside it) actually
looking at the bytes instead of returning an empty extraction.

**Why it was filed before anyone hit it the hard way.** 0.9.0 was scoped as "offline fallback", and
"offline" reads as "the same features, no network" unless stated otherwise. It is not, for images,
and saying so cost one paragraph against discovering it after the fact.

**Deliberately deferred past 1.0.0** (Phase 9 design spec §7). Needs a **vision-capable
local model**, which is a roadmap item rather than a code fix: nothing about the `VisionExtraction`
contract, the fetcher, or the cache changes for it — only `OllamaLLM.extract_image` (or a further
`LLMClient` alongside it) actually looking at the bytes. It is stated in 0.9.0's known limits and
stays stated in 1.0.0's.

## 22. No runtime step-down between backends

**What is broken.** `LEY_KHAA_LLM` selects the backend once, at startup (`build_llm`). A Claude call
that fails mid-run is not retried on Ollama, and an Ollama call that fails is not retried on Claude —
design spec §7 asks for exactly this ("LLM call failure → retry, then fall back to local Ollama") and
0.9.0 does not implement it.

**Shape of the fix.** The blocker is `LLMClient.name`: the manifest's "who actually did the work"
contract currently treats the producer as a property of the *client* (one name per `LLMClient`
instance, fixed for its lifetime). A per-call fallback makes the producer a property of the *call*
instead — the same client could answer one request as `anthropic` and the next as `ollama:qwen2.5` —
so the manifest attribution needs rework before step-down can be added safely.

**Why it was deferred.** 0.9.0 scoped the Ollama backend as a static, startup-selected alternative
(decision 1 of the phase 8 design) specifically to avoid this rework under phase pressure. It is a
design change deserving its own phase, not a one-line addition to `build_llm`.

**Deliberately deferred past 1.0.0** (Phase 9 design spec §7). This entry says it deserves
its own phase and it does: a per-call fallback makes the producer a property of the *call* rather than
the client, which breaks the `LLMClient.name` contract the manifest's "who actually did the work"
attestation depends on. Reworking that attestation under a hardening phase would put the one guarantee
that makes a bundle auditable at risk to save a startup flag.


---

## Filed at the close of Phase 9 (v0.10.0)

Items 23–33 came out of Phase 9's task and whole-branch reviews. Every one is deliberately deferred:
none blocks 1.0.0, and each is recorded rather than discarded. Numbering continues from 22 — nothing
above is renumbered, for the reason PR #9 exists.

## 23. CI's mypy lane is not version-pinned the way the local one is

`mypy` itself is pinned (`mypy==2.3.1` in `[project.optional-dependencies].dev`), but the libraries
whose *stubs* determine what it sees are not: `anthropic>=0.70`, `fastapi>=0.111`,
`sqlalchemy>=2.0`, `pydantic>=2.7`, `alembic>=1.13` and `uvicorn[standard]>=0.30` are floors, and CI
runs `pip install -e ".[dev]"` fresh on every build. Every one of those ships `py.typed`, so a new
release of any of them can turn the gate red on a commit that changed nothing — and the failure
arrives attributed to whoever pushed next.

This is the ordinary cost of unpinned dependencies, sharpened by the gate: a test failure from a
dependency bump is usually a real behaviour change worth seeing, while a *type* failure is as often
a stub author tightening an annotation.

**Shape of the fix.** A lockfile (`pip-compile`/`uv lock`) for the CI environment, or upper bounds on
the four libraries mypy reads most. Deferred because pinning the whole dependency set is a
maintenance policy decision, not a one-line CI edit.

## 24. Global `ignore_missing_imports` exempts every future untyped dependency

`[tool.mypy] ignore_missing_imports = true` (`backend/pyproject.toml`) is set for exactly one import:
with it `false`, the whole codebase yields a single error, `ley_khaa/executor/validator.py:50`'s
`import openpyxl`. Every other third-party import here ships `py.typed`.

The cost is that the switch is global. The day a new untyped dependency is added, mypy will silently
treat it as `Any` — no error, no note, and the gate stays green while the new code is unchecked at
its boundary. That is the same shape as everything else in this file: something that looks healthy
and quietly does less than it says.

**Shape of the fix.** Either `types-openpyxl` in `[dev]`, or a scoped
`[[tool.mypy.overrides]] module = ["openpyxl.*"]` block with the flag set there and nowhere else.
Both are small; neither was done in Phase 9 because the correct one depends on whether the openpyxl
stubs are good enough to keep, which is a five-minute experiment nobody had budget for at the end of
the phase. The comment in `pyproject.toml` now states the true situation either way.

## 25. Green depends on test collection order

The suite passes in pytest's default (alphabetical) file order and fails in reverse file order —
**identically on both lanes**, so this is process-global state leaking between tests, not a database
difference:

```
pytest -q $(ls tests/test_*.py | sort -r)
FAILED tests/test_api.py::test_sweep_promotes_a_ready_candidate_once_the_conversation_goes_quiet
1 failed, 1020 passed        # same one failure on SQLite and on Postgres
```

**Root cause, reproduced down to three files.** `tests/test_ollama_config.py` calls
`importlib.reload(ley_khaa.config)`, which **rebinds `ley_khaa.config.settings` to a new object**.
Any module that did `from ..config import settings` *before* the reload keeps the old one;
`test_api.py` imports `settings` inside the test body and so gets the new one. That test raises
`crystallizer_debounce_seconds` to 5 to hold a candidate READY-but-not-promoted — on the new object,
while `api/app.py`'s `build_orchestrator` reads the old one and builds `ReadinessGate(0)`. The
candidate promotes at ingest and the assertion that no task was created fails.

The minimal reproducer, and the proof it is the reload rather than elapsed time:

```
pytest -q tests/test_registry_api.py tests/test_ollama_config.py tests/test_api.py   # 1 failed
pytest -q tests/test_ollama_config.py tests/test_registry_api.py tests/test_api.py   # 45 passed
```

Order matters because the leak needs `api/app.py` to have been imported *before* the reload. A
6-second sleeping test placed ahead of `test_api.py` does **not** reproduce it, so it is not a
timing effect.

**Shape of the fix.** `test_ollama_config.py` should build a `Settings()` from a patched environment
rather than reloading the module — the reload exists only to re-run the module-level `os.getenv`
defaults, which a constructor taking overrides would do without touching global state. A fixture
that restores `ley_khaa.config.settings` after the reload would also work and is smaller.

**Why deferred.** Nothing is wrong with the shipped code; a test's cleanup is incomplete. It is filed
rather than fixed because the honest fix is a small redesign of how `Settings` is tested, and Phase 9
closed with the ordering guaranteed by pytest's own default. Worth doing before anything introduces
test-order randomisation or `pytest-xdist`, both of which would turn this from latent into red.

## 26. Alembic migrations are still never run against Postgres

Phase 9 put the *suite* on Postgres, but `tests/test_migrations.py` builds its own `sqlite:///` URLs
and is untouched by `DATABASE_URL` — deliberately: `conftest.py` builds the Postgres schema with
`Base.metadata.create_all`, not Alembic, so that a failure on that lane is unambiguously a
Postgres/SQLite behaviour difference rather than a migration bug. The consequence is that the drift
guard, the pre-alembic stamping path, and Phase 9's new downgrade tests all run on SQLite only. The
`with_variant(JSONB(), "postgresql")` half of `operation_aliases` is still checked by nothing that
runs automatically — which is the exact gap that let item 5's bug reach review.

**`0006_alias_jsonb` is the concrete case, in both directions.** Its upgrade and downgrade are the
only `alter_column` in the tree, and both move between `sa.JSON()` and the JSONB variant — types that
render identically on SQLite. Phase 9's downgrade round trip reddens for nine of the ten revisions
when their `downgrade()` is replaced by `pass`; `0006` is the one that stays green (observed: `14
passed`). No SQLite test can discriminate it, so this entry is the only thing that would.

**This entry also carries the finding item 9 raised and could not close:** upgrading a throwaway
`postgres:16` database to head and running `compare_metadata` against `Base.metadata` still reports

```
[('remove_constraint', UniqueConstraint(Column('name', NullType(), table=<workflows>)))]
```

an autogenerate artifact of `unique=True` together with `index=True` on `WorkflowRow.name`, not a
real schema gap. Re-confirmed by hand at the close of Phase 9 (`upgrade head` and `downgrade base`
both succeed on Postgres, so the migrations themselves are sound there). Fixing it means declaring
the constraint explicitly in the migration so the comparator stops asking for its removal — best done
in the same change that gives it a lane to be checked on.

**Shape of the fix.** A dialect-parametrised `test_migrations.py` that runs the drift guard, the
downgrade tests and the round trip against whatever `DATABASE_URL` names, skipping to SQLite when it
is unset — the natural follow-on to Phase 9's Task 10.

## 27. `ley_khaa.db.engine` has no `search_path` on the Postgres lane

`ley_khaa/db.py:13` builds the application engine from the bare `settings.database_url` with no
`connect_args`, so it resolves unqualified table names in `public`. Every engine the fixtures build
sets `search_path` to `ley_khaa_test`, which is what makes "the tests confine themselves to a schema
of their own" true — but nothing *enforces* it. A future test that reaches `ley_khaa.db.engine` (or
`SessionLocal`) instead of a fixture is outside the boundary entirely.

**Currently safe, and worth saying why:** `public` is empty on a scratch server, so a stray reach
fails loudly with `UndefinedTable` rather than passing against the wrong tables. Against a developer's
own `docker compose` database it would find the real ones.

**Shape of the fix.** A tripwire rather than a redesign: an autouse fixture that fails any test whose
statements ran outside `ley_khaa_test`, or a `conftest` that points `ley_khaa.db.engine` at the test
schema for the duration of the run.

## 28. The `--database` guard is itself unpinned

`pytest_configure`'s lane assertion (`tests/conftest.py`) exists so a lane cannot silently re-run the
other one — but **no test drives its mismatch branch.** Both CI steps always match by construction, so
real CI never exercises the failure path either: a regression in the guard's own logic (an inverted
comparison, a `LANE` that stops being computed) would produce zero automated signal, and the first
sign would be a green build that never touched Postgres — precisely what the guard was added to
prevent.

**Shape of the fix.** A `pytester`-based test, or a subprocess `pytest --database=postgres` on the
SQLite lane asserting exit code 4 and the message. Deferred as small and self-referential; noted
because a guard nothing tests is the shape of the defect it guards against.

## 29. The two fixture families are two databases on SQLite but one on Postgres

`session` (in-memory, `StaticPool`) and `session_factory` (a `tmp_path` file) are **separate
databases** on SQLite: a row written through one is invisible to the other. On the Postgres lane both
are bound to the same `_pg_engine` and the same `ley_khaa_test` schema, so they see each other's
writes.

**Latent, not live.** An AST scan of every test function's parameters at the close of Phase 9 finds
**no test that requests both fixtures**, so nothing depends on either behaviour today. A future test
that took both would pass on one lane and fail on the other, for a reason that has nothing to do with
the code under test.

**Shape of the fix.** Cheapest and probably right: a fixture that fails fast if both are requested in
one test, with a comment saying why. Making them genuinely separate on Postgres means a second
schema and a second `create_all`, which costs more than the property is worth.

## 30. `TEST_SCHEMA` is a fixed name, dropped at session start

`_pg_engine` runs `DROP SCHEMA IF EXISTS ley_khaa_test CASCADE` once per session. Two runs against
one server collide: the second run's `DROP` deletes the first run's tables mid-suite.

**Not a concern today** — CI gives each build its own `postgres:16` service container, and
`pytest-xdist` is not installed, so there is one run per server. It becomes one the moment either
changes, and the failure would be a spray of unrelated `UndefinedTable` errors that reads like
anything but a name collision.

**Shape of the fix.** Suffix the schema name with something per-run (the pid, or `PYTEST_XDIST_WORKER`
when set) and drop it at session teardown as well as at start.

## 31. `adapters/discord/client.py:108` turns a would-be crash into a silent `None`

`on_ready` reads `self.bot_user_id = str(user.id) if user is not None else None`. The `None` guard was
added in Phase 9 because mypy proved `discord.Client.user` is Optional — correct as far as it goes,
but `None` there means the adapter is connected without knowing its own identity, and `bot_user_id`
is what stops the bot ingesting its own messages. A `None` flows on silently.

Sibling paths in the same file raise a named `AdapterError` describing what was actually found
(`fb69ac8`), which is the pattern this one does not follow.

**Shape of the fix.** Raise `AdapterError("Discord reported ready with no bot user")` instead of
storing `None`, so the supervisor dead-letters it under a name that says what happened. Deferred
because `on_ready` firing with `client.user` unset is not a state the library is documented to
produce, so the change is a hardening of an unreachable path — worth doing, not worth doing under
release pressure.

## 32. A frozen successful extraction is still unreachable once its URL expires

**The remainder of item 19, re-filed so a closed entry does not have to carry a live limit.**
`aa62c66` gave *refusals* a second key space (`image_extractions.url_sha256`), so an unfetchable
image is dead-lettered once instead of on every drive. A **successful** extraction is still keyed
only on `sha256(image_bytes)`.

The consequence is the one whole-branch review finding B1 named: channel CDN URLs expire (Discord's
in about a day), and once one does, `_bytes_for` can no longer even re-derive the digest that would
find the stored extraction. The work was done, the result is in the table, and nothing can reach it —
a re-drive asks a human instead, which is safe but is a re-ask for an answer the system already has.

**Shape of the fix.** Write the `url_sha256` row on the *success* path too, carrying the resulting
digest, so it becomes a url→digest index: a re-drive hashes the URL, finds the digest, and reads the
frozen extraction without needing the URL to still resolve. The column and its index already exist,
and `_store` currently calls `record()` with no `url_sha256` at all (so it defaults to `None`) — what
is missing is threading the source URL down to that success-path write, plus the lookup ahead of the
fetch. `ImageExtractionRepository.clear_unfetchable` already anticipates it: it refuses to delete a
source-keyed row that carries real content, so the negative-cache cleanup added in Phase 9's fix
wave cannot gut the index this item would build.

**Why deferred.** It is a second lookup ahead of the fetch with its own invalidation question — what
should happen when the same URL later serves different bytes — and that is a cache-semantics decision,
not a one-line addition to the path `aa62c66` already touched.

## 33. Two of the four confidence floors still cannot tell `<` from `<=`

**The remainder of item 7, re-filed so a closed entry does not have to carry a live gap** — the same
handling items 19 → 32 and 9 → 26 got.

Four call sites make the identical decision with the identical shape, `confidence < FLOOR`:

- `memory/matcher.py:72` (`CONFIDENCE_FLOOR`)
- `registry/matcher.py:67` (`CONFIDENCE_FLOOR`)
- `projects/router.py:80` (`ROUTING_CONFIDENCE_FLOOR`)
- `orchestrator/amendment.py:86` (`AMENDMENT_CONFIDENCE_FLOOR`)

Item 7 asked for the boundary case — a decision landing *exactly* on the floor, which must be
accepted — and `21025b6` added it for the two matchers
(`test_memory_matcher.py:71`, `test_registry_matcher.py:84`, both asserting on
`confidence=CONFIDENCE_FLOOR`). The router and the amendment detector still have only
below-the-floor and comfortably-above-the-floor tests, so at those two sites `<` and `<=` are
indistinguishable: flipping either one turns nothing red.

**Not a live defect.** All four are `<`, which is correct — a decision that exactly meets the bar
should be taken. This is a coverage gap, not a behaviour gap, and it was verified by reading during
the whole-branch review.

**Shape of the fix.** Two tests, mirroring the two that already exist: a `ProjectChoice` and an
amendment `TaskChoice` at exactly `0.8`, each asserted to be ACCEPTED. No production change.

**Why it was not done in Phase 9.** Task 11's brief was item 7 as written, and item 7's stated scope
was the two matchers. Widening a closure task's own scope mid-flight is how a fix wave stops being
reviewable, so the remainder is filed here instead — which is what the ledger intended and, until
now, forgot to do.
