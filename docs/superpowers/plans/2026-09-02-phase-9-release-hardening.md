# Phase 9 — Release hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the backlog defects that would embarrass a 1.0.0, and install the two quality gates the project has claimed since v0.1.0 but never enforced.

**Architecture:** No new subsystems. Ten defect fixes across the dispatcher, the notifier's state guard, the dead-letter table, the vision cache's key space, the queue drain, three small API/persistence items and the dashboard; plus mypy at default settings and a Postgres lane, both enforced in CI.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy + Alembic, Pydantic v2, mypy, pytest, React/Vite, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-02-phase-9-release-hardening-design.md` — read it first; it is the binding authority and this plan argues from it.

## Global Constraints

- **This phase branches from a `main` that already contains Phase 8 (v0.9.0).** It touches `llm/client.py`, closes backlog item 19 which builds on Phase 7's vision cache, and typechecks `llm/ollama.py`. Do not start it on a `main` without Phase 8 merged.
- **Python is the worktree-local venv**: `../.venv/bin/python` from `backend/`. The repo-root `.venv` is installed editable against the MAIN checkout — using it silently tests the wrong code.
- **Backend tests**: `cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q`. `mkdir -p "$HOME/tmp"` first — this Mac runs Docker via Colima, which mounts only `$HOME`; without `TMPDIR` the 9 `[docker]` params fail misleadingly.
- **Frontend**: `cd frontend && npm test && npm run typecheck`.
- **The bar is 0 failures, 0 skipped, 0 warnings.** A skip is a retired assertion, not a pass.
- **No test may reach the network.**
- **Every fix gets a test that FAILS without it, verified by mutation.** "A guard that only appears pinned" has recurred in every recent phase — a test that passes both before and after a mutation has pinned nothing. Report the observed result, not the predicted one.
- **Every new non-null string column needs `server_default=text("''")`**, not `server_default=""` — Alembic's SQLite comparator cannot strip quotes from a zero-length default literal and the drift guard false-positives.
- **Settings are read falsy-safe** — `os.getenv(NAME) or default`, never `os.getenv(NAME, default)`. Compose passes `${VAR:-}`, which SETS the variable to `""`.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/ley_khaa/llm/client.py` | Task 1 — the unguarded `None` return |
| `backend/ley_khaa/orchestrator/dispatcher.py` | Tasks 2, 5 — poison notification, backlog drain |
| `backend/ley_khaa/persistence/repository.py` | Task 3 — the second-question guard |
| `backend/ley_khaa/persistence/dead_letter_repository.py` | Task 4 — retention |
| `backend/ley_khaa/vision/extractor.py`, `persistence/image_extraction_repository.py` | Task 6 — the URL key space |
| `backend/ley_khaa/api/schemas.py`, `api/app.py`, `persistence/workflow_repository.py` | Task 7 — the three small items |
| `frontend/src/App.tsx`, `Registry.tsx`, `TaskDetail.tsx`, `BundlePanel.tsx` | Task 8 — the stale list |
| `backend/pyproject.toml`, `.github/workflows/ci.yml` | Tasks 9, 10 — mypy and Postgres |
| `docs/superpowers/specs/2026-08-28-phase-5-backlog.md`, `CHANGELOG.md`, `README.md` | Task 11 — closure and docs |

**Ordering that matters:** Task 1 before Task 9 — the `None` guard is one of mypy's 14 `return-value`
errors, so fixing the defect first means one fewer error to triage. Task 9 (mypy) comes after every
code task except 10 and 11, so it typechecks the final code rather than being invalidated by later
edits.

---

## VERIFIED AGAINST THE REAL CODE — do not re-derive these

I checked each before writing the plan. Line numbers are from `main` before Phase 8.

- `dispatcher.py:141` `_fail_poison` exists, and `grep -c "notif" dispatcher.py` returns **0** — the dispatcher has no notifier at all.
- `dispatcher.py:79-90` `_work_one(project) -> str | None` claims **one** task and returns. Its caller at `:66-77` gathers those into `driven: list[str]`. **Changing the drain changes this return type and its caller.**
- `repository.py:334-335` is the `last_notified_state` guard: `TaskRow.last_notified_state.is_(None)` / `!= state`.
- `dead_letter_repository.py:29` `MAX_PAYLOAD_CHARS = 4_000` bounds row SIZE. There is no prune, no retention, no count bound.
- `schemas.py:149-152` `ProjectIn.name: str` is unconstrained. `schemas.py:140-145` `PromoteIn.name: str = Field(pattern=NAME_PATTERN)` is the pattern to match, with `NAME_PATTERN` imported so there is one source of truth.
- `workflow_repository.py:127-129` and `:139-141` each do `cached = self.get(name)` then `self.session.expire(cached)` — the redundant read, twice.
- `app.py:338` `queue_depth=queued` and `app.py:372` `def project_queue(...)`.
- `Registry.tsx:14` `const load = () => ...` and `:26-28` `useEffect(() => { load(); }, [])` — loads once, with no external trigger. `App.tsx:78` renders `<Registry />` as a SIBLING of the task list; `TaskDetail.tsx:157` renders `<BundlePanel taskId={task.id} />`. A promotion in the panel cannot reach the registry.
- `.github/workflows/ci.yml` has two jobs, `backend-tests` and `frontend-tests`, neither with a `services:` block. The backend job builds the sandbox image before pytest.

---

## Task 1: The unguarded `None` return — spec §3.1

**Files:**
- Modify: `backend/ley_khaa/llm/client.py`
- Test: `backend/tests/test_llm_client_guard.py` (create)

**Interfaces:** no signature change. `AnthropicLLM.parse` gains a guard on its return path.

**Why this is first.** It is the same defect Phase 7 fixed one method over in `extract_image`, on the
older and far more used path — the relevance filter, crystallizer, interpreter, synthesis and both
caches all call `parse`. It is also one of mypy's `return-value` errors, so fixing it here means Task
9 has one fewer to triage.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_llm_client_guard.py`:

```python
import pytest
from pydantic import BaseModel

from ley_khaa.llm.client import AnthropicLLM
from ley_khaa.llm.router import Stage, model_for


class Answer(BaseModel):
    verdict: str


class _NoneReturning:
    """Reproduces a response that stopped on max_tokens: the SDK returns a
    message whose parsed_output is None rather than raising."""

    def __init__(self):
        self.messages = self

    def parse(self, **kwargs):
        return type("R", (), {"parsed_output": None})()


def test_a_none_parse_result_raises_naming_the_cause():
    llm = AnthropicLLM(client=_NoneReturning())
    with pytest.raises(ValueError, match="no parsed output"):
        llm.parse(
            choice=model_for(Stage.INTERPRETER),
            system="s",
            user="u",
            output_format=Answer,
        )
```

- [ ] **Step 2: Run it and watch it FAIL**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_llm_client_guard.py -q
```

Expected: FAIL — `DID NOT RAISE ValueError`. The method currently returns `None` to its caller.

- [ ] **Step 3: Add the guard**

In `AnthropicLLM.parse`, replace the bare `return response.parsed_output` with:

```python
        parsed = response.parsed_output
        if parsed is None:
            # A response that stops on max_tokens parses to None. Returning it
            # hands the caller a None it does not expect — the crystallizer and
            # interpreter both dereference the result immediately — and the
            # traceback then names THEIR line, not this one. Phase 7 fixed the
            # same shape in extract_image; this is the older, busier path.
            raise ValueError(
                f"{choice.model} returned no parsed output "
                f"(stop_reason may be max_tokens for {output_format.__name__})"
            )
        return parsed
```

- [ ] **Step 4: Run it and watch it PASS**

Expected: 1 passed.

- [ ] **Step 5: Mutate, and record what you actually observe**

Delete the `if parsed is None:` block. Expected: the test FAILS with `DID NOT RAISE`.
Revert. **Report the observed result.**

- [ ] **Step 6: Full suite, then commit**

```bash
cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q
git add backend/ley_khaa/llm/client.py backend/tests/test_llm_client_guard.py
git commit -m "fix(llm): raise on an empty parse result instead of returning None"
```

---

## Task 2: A poisoned task tells someone — item 16, spec §3.2

**Files:**
- Modify: `backend/ley_khaa/orchestrator/dispatcher.py`
- Modify: `backend/ley_khaa/api/app.py` (the dispatcher's construction site)
- Test: `backend/tests/test_dispatcher_notifies.py` (create)

**Interfaces:**
- Produces: `Dispatcher(..., notifier=None)` — a new optional keyword argument, defaulting to `None` so every existing construction keeps working.

**The gap:** `grep -c "notif" dispatcher.py` returns 0. `_fail_poison` (`:141`) is the one path to
`FAILED` that tells nobody — a task dies and the human learns about it only by looking.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_dispatcher_notifies.py`. Follow the construction pattern in the existing
`tests/test_dispatcher.py` — read it first and mirror how it builds a `Dispatcher` and drives a task
past `max_lease_attempts`. The test must assert that a task failed by `_fail_poison` produces a
notification naming the task, and must fail if the notifier call is removed.

- [ ] **Step 2: Run it and watch it FAIL**

Expected: FAIL — either `TypeError: unexpected keyword argument 'notifier'` or an empty notification
list, depending on how you write it.

- [ ] **Step 3: Wire the notifier**

Add `notifier: Notifier | None = None` to `Dispatcher.__init__`, store it, and call it from
`_fail_poison` after the row is marked FAILED. Match how `TaskDriver` announces terminal states —
read `orchestrator/driver.py` for the existing shape and message style rather than inventing one.
A `None` notifier must be a no-op, not a crash.

Then pass the live notifier at the dispatcher's construction site in `api/app.py`, the same way
`build_orchestrator` passes `current_notifier()`.

- [ ] **Step 4: Run it and watch it PASS**

- [ ] **Step 5: Mutate, and record what you actually observe**

1. Remove the notifier call from `_fail_poison` → your new test must fail.
2. Remove `notifier=...` from the `api/app.py` construction site → a test must fail.
   `backend/tests/test_notifier_wiring.py` is where that class of test already lives; read it and add
   yours there. **If no test fails, the wiring line is unpinned** — that exact defect class shipped four times in Phase 6 and four
   more in Phase 8. Add a test that pins it.

- [ ] **Step 6: Full suite, then commit**

```bash
git commit -m "fix(dispatcher): notify when a poisoned task is failed"
```

---

## Task 3: A second clarifying question is delivered — item 17, spec §3.3

**Files:**
- Modify: `backend/ley_khaa/persistence/repository.py` (the `last_notified_state` guard at `:334-335`)
- Modify: `backend/ley_khaa/persistence/orm.py` and a new migration, IF you take the column route
- Test: `backend/tests/test_second_question_notified.py` (create)

**Interfaces:**
- If you add a column: `TaskRow.last_notified_question`, nullable, with the `server_default=text("''")` rule from Global Constraints.

**The gap:** notification is keyed on a *state change*. A second question asked without the task
leaving `needs_clarification` in between produces no notification — the human sees it only in the
dashboard, and spec §8's claim that each human-facing state is announced is false for that case.

**Two routes, implementer's choice — say which you took and why in your report:**
(a) add `last_notified_question` and compare the question text, or
(b) hash the announced text into the existing `last_notified_state` value.
Route (a) is clearer to read; route (b) avoids a migration. Both are acceptable.

- [ ] **Step 1: Write the failing test**

Create the test: a task in `needs_clarification` is asked a SECOND, different question without an
intervening state change, and a notification is delivered for it. Read
`backend/tests/test_notification_policy.py` — it covers the announced states and is the file whose
property you are extending — and mirror its construction so you are testing the real guard, not a
reimplementation.

- [ ] **Step 2: Run it and watch it FAIL** — expected: no second notification.

- [ ] **Step 3: Implement your chosen route.** If it needs a migration, write it and keep the drift
      guard green (`pytest tests/test_migrations.py`).

- [ ] **Step 4: Run it and watch it PASS.**

- [ ] **Step 5: Mutate.** Revert the comparison to state-only → your test must fail. Also confirm the
      ORIGINAL property still holds: the *same* question repeated must NOT re-notify. Add that test
      if it does not exist — a fix that notifies on every drive is worse than the bug.

- [ ] **Step 6: Full suite (plus the drift guard if you migrated), then commit.**

---

## Task 4: `dead_letters` retention — item 18, spec §3.4

**Files:**
- Modify: `backend/ley_khaa/persistence/dead_letter_repository.py`
- Test: `backend/tests/test_dead_letter_retention.py` (create)

**Interfaces:**
- Produces: a bounded-growth policy on `record(...)`, or a `prune()` the writer calls.

**The gap:** `MAX_PAYLOAD_CHARS = 4_000` (`:29`) bounds row SIZE, not COUNT. A bad token writes one
row per minute at the 60-second backoff cap, forever.

**Count-based, not time-based** (spec §3.4): the table exists so a drop is never silent, so the
NEWEST rows are the ones that must survive. Bound the count and drop oldest-first. Put the cap in
`config.py` as a falsy-safe setting so an operator can raise it.

- [ ] **Step 1: Write the failing test** — write more than the cap, assert the count is bounded AND
      that the survivors are the newest, not the oldest.
- [ ] **Step 2: Run it and watch it FAIL.**
- [ ] **Step 3: Implement.** Prune inside the same transaction as the write, so a crash cannot leave
      the table unbounded.
- [ ] **Step 4: Run it and watch it PASS.**
- [ ] **Step 5: Mutate** — remove the prune → the count test fails; invert the ordering → the
      newest-survive test fails. Both must discriminate; report what you observed.
- [ ] **Step 6: Full suite, then commit.**

---

## Task 5: A project drains its backlog per tick — item 11, spec §3.6

**Files:**
- Modify: `backend/ley_khaa/orchestrator/dispatcher.py`
- Test: `backend/tests/test_dispatcher.py` (extend) or a new file

**Interfaces:**
- **`_work_one(project) -> str | None` becomes a drain returning a LIST of driven task ids.** Its
  caller at `dispatcher.py:66-77` gathers results into `driven: list[str]` and appends each non-None
  result — that loop must handle a list instead. **This is the one interface change in the phase;
  get it right or the sweep silently reports the wrong count.**

**The gap:** `_work_one` claims exactly one task and returns, so a project with a slow task holds up
every other project's queue for that tick.

- [ ] **Step 1: Write the failing test** — one project with three queued tasks drains all three in a
      single sweep, and two projects make progress independently when one is slow.
- [ ] **Step 2: Run it and watch it FAIL** — expected: only one task per project per sweep.
- [ ] **Step 3: Implement the drain.** Loop until `_claim_next` returns `None`. Keep the per-project
      semaphore (`settings.max_concurrent_projects`) — this changes what a lane does per tick, not
      the concurrency machinery. Update the caller's result handling for the new return type.
- [ ] **Step 4: Run it and watch it PASS.**
- [ ] **Step 5: Mutate** — restore the single-claim behaviour → the drain test must fail. Also check
      the failure path still holds: one project raising must not take the others' results with it
      (there is an existing test for this; confirm it still passes and still discriminates).
- [ ] **Step 6: Full suite, then commit.**

---

## Task 6: An unfetchable image is recorded once — item 19, spec §3.5

**Files:**
- Modify: `backend/ley_khaa/vision/extractor.py`, `backend/ley_khaa/persistence/image_extraction_repository.py`, `backend/ley_khaa/persistence/orm.py`
- Create: a migration
- Test: `backend/tests/test_vision_extractor.py` (extend)

**Interfaces:**
- Produces: a `url_sha256` key space alongside the existing `image_sha256`.

**The gap:** with no image bytes there is no `sha256(image_bytes)` to key a "do not retry" record on,
so an unfetchable image is retried and re-dead-lettered on EVERY drive. Phase 7 deferred this
deliberately; this is the durable fix it named.

**Read Phase 7's rules before changing the cache — they are load-bearing and must survive:**
- `.extract()` ALWAYS returns a row, never `None`, never raises.
- A non-empty `content` cache hit ALWAYS short-circuits — "written once and reused, a second drive
  makes no model call" is a definition-of-done line with tests pinning it.
- An empty-content hit re-extracts when the stored `model` differs from the current client's name or
  is empty — that is what makes a configuration change (offline→online) retry.

Your new key must record an unfetchable SOURCE without breaking any of those. Hash the URL rather
than storing it: raw Slack and Discord URLs are long and carry query tokens.

- [ ] **Step 1: Write the failing test** — the same unfetchable URL across two drives produces ONE
      dead letter, not two, and still returns a row both times.
- [ ] **Step 2: Run it and watch it FAIL.**
- [ ] **Step 3: Implement**, with a migration. Keep the drift guard green.
- [ ] **Step 4: Run it and watch it PASS.**
- [ ] **Step 5: Mutate** — remove the URL-keyed lookup → your test must fail. Then re-run Phase 7's
      OWN cache tests and confirm every one still passes, especially the one-model-call-per-image
      guarantee and the offline→online retry. **If any Phase 7 test needs changing, STOP and report
      it** — that is evidence your change altered a guaranteed behaviour, not a fixup to make.
- [ ] **Step 6: Full suite plus the drift guard, then commit.**

---

## Task 7: Three small items — 10, 13, 15, spec §3.7

**Files:**
- Modify: `backend/ley_khaa/persistence/workflow_repository.py` (item 10)
- Modify: `backend/ley_khaa/api/schemas.py` (item 13)
- Modify: `backend/ley_khaa/api/app.py` (item 15)
- Test: the existing files covering each

**Batched deliberately** — three small independent edits of the same kind, reviewed as one diff.

- [ ] **Item 10** — `workflow_repository.py:127-129` and `:139-141` each do `cached = self.get(name)`
      then `self.session.expire(cached)`. Delete the false comment and the redundant read from BOTH.
      Add a test that loads a `WorkflowRow`, calls `record_success`/`record_failure` on it, and would
      have caught the redundancy.
- [ ] **Item 13** — `schemas.py:149` `ProjectIn.name: str` is unconstrained. Give it
      `Field(pattern=NAME_PATTERN)` exactly as `PromoteIn` does at `:145`, importing the same
      `NAME_PATTERN` so there is one source of truth. Validation only, no schema change. Test that a
      malformed name is a 422.
- [ ] **Item 15** — `queue_depth` (`app.py:338`) and `project_queue` (`:372`) disagree by design and
      nothing says so. Make it explicit: a docstring at minimum. A rename to `/projects/{name}/tasks`
      is permitted if you judge it clearer, but **only if you update every caller including the
      frontend** — check `frontend/src/api.ts` before deciding, and say which you chose. Verified: `api.ts` carries a
      `queue_depth` FIELD (`:163`) but does not appear to call the queue ROUTE, so a rename may touch
      no frontend code at all — confirm that yourself rather than trusting this line.
- [ ] **Mutate each** — revert each fix in turn and confirm a named test fails. Report all three.
- [ ] **Full suite (and `npm test` if you renamed the route), then commit.**

---

## Task 8: The dashboard reflects a promotion — item 8, spec §3.8

**Files:**
- Modify: `frontend/src/App.tsx`, `Registry.tsx`, `TaskDetail.tsx`, `BundlePanel.tsx`
- Test: `frontend/src/Registry.test.tsx`, `BundlePanel.test.tsx`

**The gap, verified:** `Registry.tsx:26-28` does `useEffect(() => { load(); }, [])` — it loads once
with no external trigger. `App.tsx:78` renders `<Registry />` as a SIBLING of the task list, and
`TaskDetail.tsx:157` renders `<BundlePanel taskId={...} />` inside it. So a promotion made in the
panel cannot reach the registry, and the user sees a stale list after acting.

- [ ] **Step 1: Write the failing test** — promoting from a `BundlePanel` refreshes the `Registry`
      list on the same page.
- [ ] **Step 2: Run it and watch it FAIL.**
- [ ] **Step 3: Implement.** Lift a refresh signal into `App` and pass a callback down to
      `BundlePanel`, mirroring how `App.tsx:68` already passes `setTasks` down through the same tree.
      Do not add a state-management library for this.
- [ ] **Step 4: Run it and watch it PASS.**
- [ ] **Step 5: Two smaller real items in the same files** — `PromoteControl`'s Cancel clears `error`
      but not `name`/`description`, so reopening shows stale input; and `Registry`'s `load` is
      redefined every render (wrap in `useCallback`). Fix both, each with a test.
- [ ] **Step 6: Mutate** — remove the refresh signal → your test must fail.
- [ ] **Step 7: `npm test && npm run typecheck`, then commit.**

Cosmetic and optional: `BundlePanel.test.tsx` and `TaskDetail.test.tsx` mix `test(...)` and `it(...)`
where every other file uses `test(...)`. Harmonise while you are in the file, or leave it.

---

## Task 9: mypy at default settings, enforced in CI — item, spec §4.1

**Files:**
- Modify: `backend/pyproject.toml` (dependency + config), `.github/workflows/ci.yml`
- Modify: whichever source files carry errors

**Do this AFTER Tasks 1-8** so you typecheck the final code rather than being invalidated by later edits.

**The gap:** spec §7 has required "typecheck clean" since v0.1.0. CI runs pytest only, and the backend
has no typechecker configured at all. That line has been unverifiable for eight phases.

**Baseline measured on `main` before Phase 8: 71 errors in 20 files** — 29 `attr-defined`,
14 `return-value`, 13 `arg-type`, 6 `valid-type`, 5 `union-attr`, 4 `assignment`. Worst files:
`persistence/repository.py` (18), `llm/heuristic.py` (8), then orchestrator, driver and adapters at 5
each. Phase 8's files and Tasks 1-8 will shift this — **re-measure first, do not trust the number.**

- [ ] **Step 1: Add mypy and its config**

Add `mypy` at a pinned version to `[project.optional-dependencies].dev` in `backend/pyproject.toml`,
in the same exact-pin style as the runtime dependencies. Put the configuration in `pyproject.toml`
beside the existing `[tool.pytest.ini_options]`. Default settings — **not `--strict`** (spec
decision 2).

- [ ] **Step 2: Measure the real baseline**

```bash
cd backend && ../.venv/bin/python -m mypy ley_khaa --ignore-missing-imports
```

Record the actual count and distribution in your report.

- [ ] **Step 3: Fix the errors, file by file**

**Fix them, do not silence them.** A `# type: ignore` is acceptable only where the alternative is
worse, and the reason goes in a comment beside it. Work in small commits, one file or one error class
at a time, running the test suite as you go — a type fix that changes behaviour is a regression, and
the suite is what catches it.

**If a type error reveals a real bug** — as the `parse` `None` return did — treat it as a finding:
fix it, add a test that pins the behaviour, and say so in your report. That is the whole reason for
adopting this.

- [ ] **Step 4: Wire the CI lane**

Add a mypy step to the `backend-tests` job in `.github/workflows/ci.yml`, after the install step and
before or beside pytest. It must FAIL the build on an error, not warn.

- [ ] **Step 5: Verify the gate actually gates**

Introduce a deliberate type error, run the exact CI command locally, confirm it fails, then remove it.
**Report the observed output.** A CI lane that cannot fail is the same defect class as a test that
pins nothing.

- [ ] **Step 6: Full suite, then commit.**

---

## Task 10: A Postgres lane in CI — item 9, spec §4.2

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `backend/tests/conftest.py` if the suite needs a `DATABASE_URL` switch

**The gap:** the whole suite runs on SQLite while `docker compose up` deploys Postgres. Postgres-only
bugs have already bitten this project twice — a naive-vs-aware datetime comparison SQLite cannot
reproduce, and a `json`-column equality issue that shaped two table designs. **The suite has never run
against the database the project actually ships on.**

- [ ] **Step 1: Add the service**

Add a `postgres:16` service to the `backend-tests` job with a health check, mirroring
`docker-compose.yml`'s `db` service (verified: `postgres:16`, `POSTGRES_USER: ley`,
`POSTGRES_PASSWORD: ley`, `POSTGRES_DB: leykhaa`).

- [ ] **Step 2: Run the suite against it**

Point `DATABASE_URL` at the service and run the suite. **Keep the SQLite lane** — it is the fast local
loop and the documented dev path. Either a matrix over both, or a second step; your judgment, say
which you chose.

- [ ] **Step 3: Fix what Postgres finds**

**Expect failures, and treat each as a finding rather than an obstacle.** This lane exists precisely
because the two databases differ. If a test fails only on Postgres, that is a real behaviour
difference: understand it before changing anything, and report it. **Do not make a test pass by
weakening it.**

- [ ] **Step 4: Verify the lane gates** — make a Postgres-only failure happen deliberately, confirm
      the job fails, remove it. Report what you observed.
- [ ] **Step 5: Commit.**

---

## Task 11: Close the coverage gaps and the backlog — items 7 and 12, spec §4.3

**Files:**
- Test: the files covering the item 7 gaps; `backend/tests/` for item 12
- Modify: `docs/superpowers/specs/2026-08-28-phase-5-backlog.md`, `CHANGELOG.md`, `README.md`, `docs/GETTING_STARTED.md`

- [ ] **Item 12** — nothing tests `HeuristicLLM`'s `ProjectChoice` rule, because a blanket `except`
      launders the `NotImplementedError` into a default. Assert on `decision.reason` /
      `decision.stage` so the offline route is pinned rather than laundered. **Verify the laundering
      is real first** — read the `except` and confirm what it swallows.
- [ ] **Item 7** — read the item's own list of carried-forward gaps and close them. If any is no
      longer true, say so rather than writing a test for a gap that closed itself.
- [ ] **Close the backlog items.** Mark **7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19** closed, each
      with the commit that closed it, in the file's established style. **Read the file's existing
      CLOSED entries and match them.** Do NOT renumber anything.
- [ ] **The six deferred items** — 1, 2, 3, 20, 21, 22 — each get a line saying they are deliberately
      deferred past 1.0.0 and why, per spec §7. They stay OPEN.
- [ ] **CHANGELOG** — a `## [0.10.0]` entry in the shape of `## [0.9.0]`.
- [ ] **README / GETTING_STARTED** — update any claim this phase falsified. Specifically: the phase
      table gains a `v0.10.0` row, and anything saying the suite is SQLite-only or that there is no
      typechecker is now false. `grep -rn "SQLite-only\|typecheck" README.md docs/` and fix each hit.
- [ ] **Full suite, `npm test`, `npm run typecheck`, then commit.**

---

## Final verification, before the whole-branch review

- [ ] `cd backend && TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -q` → **0 failures, 0 skipped, 0 warnings.**
- [ ] The same suite green against **Postgres**.
- [ ] `../.venv/bin/python -m mypy ley_khaa --ignore-missing-imports` → clean.
- [ ] `TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest -m docker -q` → 9 passed, 0 skipped.
- [ ] `TMPDIR="$HOME/tmp" ../.venv/bin/python -m pytest tests/test_migrations.py -q` → drift guard green.
- [ ] `cd frontend && npm test && npm run typecheck` → green, tsc silent.
- [ ] **Every backlog item this phase claims to close is actually closed in the file**, and the six deferred ones are still open with reasons.
- [ ] **Whole-branch review on Opus.** It is 6-for-6 at finding what per-task reviews structurally cannot — including, last phase, an audit artifact that denied its own producer. Expect it to find something.

---

## Self-review of this plan

**Spec coverage.** §3.1→T1, §3.2→T2, §3.3→T3, §3.4→T4, §3.5→T6, §3.6→T5, §3.7→T7, §3.8→T8,
§4.1→T9, §4.2→T10, §4.3→T11. §6 DoD → the final verification list. §7 deferred items → T11.

**All 18 open backlog items are accounted for:** 8, 10, 11, 13, 15, 16, 17, 18, 19 are group-A fixes
(Tasks 2-8); 7, 9, 12 are group B (Tasks 9-11); 1, 2, 3, 20, 21, 22 are deferred with reasons. Plus
`client.py`'s `None` guard, which is not a backlog item — it was found by mypy.

**Thirteen claims were verified against the real code before this plan was written**, because
reference code written from memory is where the last three phases' defects came from. The one that
would have caused a real bug: `_work_one` returns `str | None` and its caller appends non-None
results, so changing it to a drain without updating that loop would make the sweep silently report
the wrong driven count.

**Two tasks deliberately expect failures rather than treating them as obstacles.** Task 10's Postgres
lane exists because the databases differ, and Task 9's type errors may reveal real bugs — both say so
explicitly, because "make the new check pass" is the wrong instinct in both cases.

**Ordering is load-bearing in two places**, and both are stated in the File Structure section: Task 1
before Task 9 (the `None` guard is one of mypy's errors), and Task 9 after Tasks 1-8 (so it
typechecks final code).

**Placeholder scan.** Tasks 1 and 7 carry complete reference code. Tasks 2-6 and 8-11 name the exact
file, line and behaviour to change, and direct the implementer to read the existing neighbouring
pattern rather than inventing one — deliberate, because each touches a subsystem with established
conventions (notifier message style, migration shape, CI job structure) that a from-scratch snippet
would contradict. No step says "add error handling" or "similar to Task N".
