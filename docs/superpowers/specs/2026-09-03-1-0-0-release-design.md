# Phase 10 — 1.0.0 release: design

**Status:** approved 2026-09-03.
**Predecessor:** `2026-09-02-phase-9-release-hardening-design.md` (v0.10.0, merged as `5d177d5`).
**Authority:** the project spec `2026-08-18-ley-khaa-design.md`; §11 is the definition of done this
phase exists to satisfy.

## 1. What this phase is

Every §11 feature line is already shipped. Verified line by line against the code on 2026-09-03, not
from memory: crystallizer, vision intake with a frozen checkpoint, the autonomy dial, project routing
with concurrent queues, the amendment detector, synthesis in the sandbox, the registry fast path, the
reproducible Output Bundle with a byte-identity test, the Model Router with its Ollama fallback, task
memory, real Slack and Discord adapters, five end-to-end test files, and full public-repo hygiene.

So this phase adds no features. It does four things:

1. Closes the three backlog items that are **gaps in the quality gates themselves** — 25, 26, 28.
2. Establishes the §11 first line — fresh clone → `docker compose up` → dashboard live — **by hand
   for the record and as a CI job so it cannot rot again.**
3. Says what 1.0.0 commits to, and states the known limits honestly.
4. Tags `v1.0.0` and cuts its GitHub release.

### 1.1 Hard constraint: no new features

Anything that is not one of the three gate fixes, the verification, or documentation is out of scope
**by construction** — including tempting adjacent cleanups in files this phase already touches. The
fourteen other open backlog items are documented, not opportunistically fixed. A release phase that
grows features is how a release slips.

### 1.2 Why these three items and not the other fourteen

Items 25, 26 and 28 share a property the rest do not: each is a hole in a mechanism whose whole job is
to tell the truth about the code.

- **25** — the suite is green only in pytest's default collection order. A suite whose greenness
  depends on filename sort order is not reporting what it claims to report.
- **26** — Alembic migrations have never run against Postgres, the database the project deploys on.
  A migration suite that only exercises the dev database is testing the wrong artifact.
- **28** — the `--database` lane guard has no test. It is the thing that stops a CI lane silently
  degrading into a duplicate of the other lane; nothing stops *it* degrading.

A 1.0.0 that ships with documented feature limits is honest. A 1.0.0 whose gates can lie is not. That
is the whole selection rule.

## 2. What 1.0.0 commits to

Two claims, stated in the README.

**Claim one: §11 is met.** Each line is true and demonstrable, and the README points at what
demonstrates it — a test name, a CI job, or a recorded transcript. No line is asserted from memory.

**Claim two: an explicit stable/unstable contract.** SemVer is already declared
(`CONTRIBUTING.md`), but nothing says what is being versioned. Across `1.x`:

**Stable — changes to these are breaking and require `2.0.0`:**
- **every HTTP endpoint the application serves, and its response shape.** Enumerated rather than
  gestured at, so this cannot quietly become a partial list: `/health`, `/messages`,
  `/conversations/{id}/messages`, `/tasks` and its `/{id}` sub-resources (`answer`, `approve`,
  `reject`, `mode`, `spec`, `promote`, and the `bundle` family), `/projects` and
  `/projects/{name}/tasks`,
  `/registry` and its `/{name}` sub-resources, `/candidates` with `sweep`/`fold`/`separate`,
  `/triage`, `/dead-letters`, and `/simulate/{...}`. `/health` is load-bearing beyond the dashboard —
  compose's own health check and the §6 smoke job both depend on it;
- the `LEY_KHAA_*` environment variables and their meanings;
- the Output Bundle layout: `task-workspaces/task-<id>/` with its deliverable, `generator/`,
  `inputs/` and `manifest.json`, and the manifest's field names.

**Unstable — these may change in any `1.x` release:**
- internal module boundaries and function signatures (this is an application, not a library);
- the registry and task-memory database schemas, which Alembic migrates for you;
- prompt text and the synthesized-script format;
- the dashboard's component structure.

This is deliberately narrow. The project is local-first and single-operator, and over-promising on
interfaces that are still young would make the next honest improvement a major version bump.

## 3. Item 25 — settings that re-read their environment

### 3.1 The defect, and why the backlog's own proposed fix does not work

**Two** test files call `importlib.reload(ley_khaa.config)` — `tests/test_ollama_config.py` and
`tests/test_vision_config.py` (eight call sites across four tests). Reloading **rebinds
`ley_khaa.config.settings` to a new object**. Any module that did `from ..config import settings`
before the reload keeps the old one. `test_api.py` imports `settings` inside the test body and gets
the new one, raises `crystallizer_debounce_seconds` to hold a candidate ready-but-unpromoted — while
`api/app.py`'s `build_orchestrator` reads the *old* object and builds `ReadinessGate(0)`. The
candidate promotes at ingest and the assertion fails. Reproduced down to three files; not a timing
effect.

The backlog's stated fix — "build a `Settings()` from a patched environment rather than reloading" —
**cannot work as written, and this was found by reading the constructor.** `Settings` is a
`@dataclass(frozen=True)` whose fields use `os.getenv(...)` as *default values evaluated at
class-definition time* (`config.py:39-120`). A fresh `Settings()` re-uses the defaults baked in at
import and never re-reads the environment. That is precisely why the test reaches for `reload` in the
first place. The item's "shape of the fix" was written from the symptom, not the constructor.

### 3.2 The fix

Introduce `_env_str` / `_env_int` / `_env_bool` helpers in the shape of the existing `_tolerant_int`,
and move every field to `field(default_factory=...)`.

Three consequences:

1. `Settings()` genuinely re-reads the environment, so a test constructs an instance under
   `monkeypatch.setenv` and never mutates a global. **Both** reload sites drop `importlib.reload`
   entirely — making the defaults lazy does NOT fix a `reload`, which rebinds the module attribute
   regardless of how the defaults are computed, so removing every call site is load-bearing rather
   than tidy-up.
2. **The project's stated falsy-safe rule becomes true.** The rule — "settings are read
   `os.getenv(NAME) or default`, never `os.getenv(NAME, default)`, because compose passes
   `${VAR:-}` which SETS the variable to empty" — is currently violated by **19 of 27 fields**.
3. Production behaviour is unchanged: the module-level `settings = Settings()` still evaluates
   exactly once, at import.

### 3.3 The falsy-safe hazard, stated precisely

This is **not** a live bug today, and the spec says so rather than overselling it. Every variable
`docker-compose.yml` passes as `${VAR:-}` was checked against its field: each is either already
falsy-safe (`ollama_model`, `image_hosts`, `image_max_bytes` use `or`), or harmless because its
default is `""` anyway (the Slack and Discord settings), or correct by luck — `vision_enabled` is
`os.getenv("LEY_KHAA_VISION", "on") != "off"`, and `"" != "off"` is `True`, which is the intended
default.

It is a live *hazard*. Adding one line such as `LEY_KHAA_DEBOUNCE_SECONDS: ${...:-}` to compose makes
`int("")` raise `ValueError` during import, before logging is configured — the operator gets a
traceback and no service. Since the fix touches all 27 fields anyway, closing the hazard costs
almost nothing, and it makes a stated rule true instead of aspirational.

### 3.4 Risk

This is the phase's only production-code change. The failure mode is loud (a wrong default surfaces
as a wrong value everywhere it is read) and the regression net is 1038 tests across two databases
(the baseline at the time of writing; 1066 by the end of the phase).
`mypy` must stay clean: `field(default_factory=...)` under annotations is well-typed, and the frozen
dataclass is unaffected.

## 4. Item 26 — migrations against Postgres

`tests/test_migrations.py` builds SQLite URLs directly in five places
(`f"sqlite:///{tmp_path / '…'}"`), independent of the conftest lane switch, so migrations run on
SQLite **even on the Postgres lane**.

**Fix.** A `migration_url` fixture yielding a `tmp_path` SQLite file on the SQLite lane, and on the
Postgres lane a dedicated throwaway schema per test (`ley_khaa_mig_<uuid>`, `search_path` set,
dropped afterwards). Migrations *create* tables, so they need a genuinely empty namespace and cannot
share the `ley_khaa_test` schema the main suite truncates. The five hardcoded URLs read from the
fixture.

**What this was expected to close, and what actually happened — corrected after execution.**

The spec originally claimed this task would make `0006_alias_jsonb` discriminable and take migration
coverage from 9-of-10 to 10-of-10. **That claim was wrong, and executing the task disproved it.**

The reasoning behind it was: `0006`'s downgrade is an `alter_column` between `sa.JSON()` and
`JSON().with_variant(JSONB, "postgresql")`, those two render identically on SQLite, and therefore
SQLite rendering identity was the reason a `pass` downgrade left the file green. On Postgres the two
genuinely do differ — an `information_schema` probe over `head → 0005 → head` confirms the real
downgrade produces `json` at 0005 while the mutated one leaves `jsonb`, which is the first direct
evidence `0006`'s downgrade is correct on Postgres at all.

**But the round-trip test compares only the schema after the RE-UPGRADE, and `0006.upgrade()` sets
`jsonb` either way.** A no-op downgrade of a pure type change leaves no residue for a round trip to
trip over — **on any database**. SQLite's rendering identity was never the only reason, and removing
it changes nothing. Measured: `downgrade() -> pass` gives `14 passed` on Postgres *and* on SQLite.

**Migration coverage therefore remains 9-of-10, and the three `0006` admissions stay true.** They are
corrected only to state both reasons rather than one. Closing `0006` for real needs an assertion at
the *downgraded* point rather than after the round trip, which can only mean anything on Postgres —
that collides with the phase's 0-skipped bar and is a design decision, not an improvised test. It is
filed as backlog work, not attempted here.

**The task's real value turned out to be different: it converted a KNOWN defect from something
re-confirmed by hand into something an automated gate catches.** `0004_registry_memory` declared
`workflows.name` with column-level `unique=True` *and* a unique index, so a migrated Postgres database
carried a `workflows_name_key` constraint the models never declare.

**This drift was not discovered here, and saying so would be false.** Backlog item 9 raised it, item 26
carries it (`2026-08-28-phase-5-backlog.md:824`), and it was re-confirmed by hand at the close of
Phase 9 by upgrading a throwaway `postgres:16` database and running `compare_metadata`. What changed is
that `test_migrations_match_the_models` now fails on it automatically, on a straight `upgrade head`, on
every CI run — instead of depending on someone remembering to check by hand. Item 26's own "shape of
the fix" says this should be done "in the same change that gives it a lane to be checked on", so
fixing it here is inside the item's scope rather than an adjacent cleanup.

## 5. Item 28 — the lane guard's own test

The `--database=sqlite|postgres` guard raises `pytest.UsageError` from `pytest_configure`, so it
cannot be exercised by an ordinary test — by the time a test runs, the guard has already passed.

**Fix.** A subprocess test invoking `pytest` with a deliberately mismatched flag, asserting exit code
**4**, zero tests collected, and the specific message — in both directions (asking for Postgres on
the SQLite lane, and the inverse). That pins the one regression that matters: the guard silently
stops guarding, and both CI lanes quietly become the same lane.

## 6. The §11 first line: fresh clone → `docker compose up`

Last verified at v0.1.0, nine phases and a Postgres lane ago. Established two ways, because they
answer different questions.

**By hand, for the record.** A genuine `git clone` into a temporary directory, `docker compose up`,
and a walk of the golden path with a human looking at the dashboard. The observed transcript goes
into `docs/GETTING_STARTED.md`. CI cannot tell you the dashboard *looks* right.

**As a CI job, so it cannot rot.** A workflow job that builds the stack from the checkout, waits on
the compose health checks, asserts the backend answers, the dashboard serves, and the seeded demo
task reaches `done`, then tears down with `down -v`. This turns the DoD's first line from a claim
with a timestamp into a mechanism — which is the same argument as items 25, 26 and 28.

**Stated risk.** This job is the flakiest thing in the phase: image builds, health-check timing, and
a demo task that must actually complete. If it proves genuinely flaky under CI rather than merely
slow, the correct response is to ship it non-blocking with the reason recorded — **not** to loosen
its assertions until it passes. A gate that cries wolf and a gate that asserts nothing are both worse
than an honest non-blocking check. Flag it; do not quietly weaken it.

## 7. Documentation

- **README** — the phase table's `v1.0.0` row flips from 🎯 target to shipped; §11 restated as met,
  each line pointing at what demonstrates it; the §2 stable/unstable contract; the known-limits
  section below.
- **`docs/GETTING_STARTED.md`** — the observed fresh-clone transcript.
- **CHANGELOG** — a `## [1.0.0]` entry in the established shape.
- **Backlog** — 25, 26 and 28 closed with their commits, in the file's existing style, renumbering
  nothing. The remaining fourteen get a short preamble framing them as post-1.0 work.

### 7.1 Known limits, stated once and properly

What a reader should know before running it, written so they learn it from us rather than by hitting
it: the channel adapters were never exercised end to end against a live workspace — everything is
proven offline and against recorded transports, with the thin connection wrappers the only part
checked by hand against real Slack and Discord workspaces, once, and **no image from a real message
on either platform has ever been through the vision path**;
Ollama is text-only, so vision does not work on the offline path; memory does not learn paraphrases
the way the registry learns aliases, so `times_seen` under-reports across wordings; there is no
management surface for task memory; and `--strict` typing is a post-1.0 ratchet.

**Corrected while writing Task 6, and recorded rather than silently fixed** (this is the third
correction to this spec in this phase, all of the same class): the first item above originally read
"Slack vision is not live-tested — Discord's full loop is, Slack's is offline-and-recorded only."
**Nothing in the repository supports that split.** The README's [Images](../../../README.md#images)
section (`README.md:553` at the time of writing — cited by section as well, because a bare line
number is exactly what M-1 caught going stale) and `CHANGELOG.md`'s 0.8.0 entry
both say, of *both* platforms, "not live-tested against a real Slack or Discord image — proven
offline and against recorded transports, the same call made for the channel adapters in 0.7.0", and
the Phase 6 design spec (§7) says the thin connection wrappers are "the only part verified by hand,
once, against real workspaces" — again for both. Writing the split into the README would have been a
new false statement introduced by the section whose whole purpose is retiring them. This is the
second time this phase that a claim in a correcting document was itself wrong; the pattern is
reaching for the more specific-sounding story over the duller true one.

Also added, from Task 1's review and Task 2's finding: `DATABASE_URL=""` now falls back to the
credentialed localhost default rather than failing loudly (a consequence of §3.3's falsy-safe rule,
correct but surprising), and `0006_alias_jsonb`'s downgrade is discriminated by no test on **either**
database, for the reason §4 records.

## 8. Definition of done

- Every §11 line demonstrable **and demonstrated** — not asserted from memory.
- Items 25, 26 and 28 closed, each with a test that fails without its fix, verified by mutation over
  the whole test file, with the observed output reported.
- Migration coverage stays **9-of-10**, with the `0006` admissions corrected to state BOTH reasons
  (the round trip's shape, not only SQLite's type rendering) rather than being declared closed.
- The compose smoke job green in CI (or non-blocking with a recorded reason, per §6), and the
  by-hand fresh-clone transcript recorded.
- Both database lanes green, **0 failures, 0 skipped, 0 warnings**; `mypy` clean; frontend tests and
  `tsc` clean.
- **No false statement** in README, CHANGELOG, GETTING_STARTED, CONTRIBUTING, or this spec. The
  discipline that found fifteen of them in v0.10.0, applied once more before the tag.
- `v1.0.0` tagged on the merge commit; GitHub release cut from the CHANGELOG section.

## 9. Out of scope

Backlog items 1, 2, 3, 20, 21, 22, 23, 24, 27, 29, 30, 31, 32, 33 — documented, not fixed. Tauri
desktop packaging, `--strict` mypy, and a local vision model all remain post-1.0. Distribution stays
clone + `docker compose up`, which is the §3 v1 audience.
