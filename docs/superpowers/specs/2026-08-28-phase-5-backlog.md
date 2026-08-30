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
something. Note that today every task is created with `project="default"` — hardcoded at
`orchestrator/orchestrator.py`, the only `TaskRepository.create` call site — so there is exactly one
project and memory is shared across every client. Phase 4's CHANGELOG and matcher comment now say
this plainly; they previously claimed client isolation the code did not provide.

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

- **No downgrade test for any migration** (`0001`–`0004`). Verified by reading only: LIFO column
  drops, native SQLite `DROP COLUMN`.
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
- Both frontend test files now mix `test(...)` and `it(...)`.

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
