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

## 4. Settle the memory-vs-registry scoping asymmetry deliberately

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

## 5. Concurrency: workflow counters are read-modify-write

`WorkflowRepository.record_success` / `record_failure` (`persistence/workflow_repository.py`) read,
modify and write `runs_ok` / `runs_failed` / `operation_aliases` with no locking. FastAPI's
sync-endpoint threadpool plus the periodic sweeper is real concurrency today, so two concurrent
cached runs of the same workflow can lose a counter increment or a learned alias.

Costs are small — counters are cosmetic, and a lost alias costs one extra Haiku call — which is why
this was deferred. The fix is an atomic `UPDATE ... SET runs_ok = runs_ok + 1`, and the alias append
wants the same treatment.

## 6. Ordering: two writes land before the state claim that authorises them

`save_memory_hit` (`orchestrator/driver.py`) commits before the `CLASSIFIED → INTERPRETED` claim, so
a task that loses that race permanently carries `remembered_from_task_id` / `familiarity` for a path
it did not take. It mirrors `save_spec`'s pre-existing ordering, so it is not a regression — and
`_remember` gets this right by calling only after its claim wins, which is the model to follow.

Worth fixing both together, since they are the same shape and the correct pattern already exists
two functions away.

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
