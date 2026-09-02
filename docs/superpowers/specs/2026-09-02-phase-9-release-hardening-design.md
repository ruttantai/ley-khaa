# Phase 9 (v0.10.0) — Release hardening

Status: **approved** 2026-09-02.
Prior art: `2026-08-28-phase-5-backlog.md` (the item list this phase closes);
`2026-08-18-ley-khaa-design.md` §7, §11.

---

## 1. Goal

1.0.0 is the next release. A 1.0.0 carrying documented defects is a weak public artifact, so this
phase closes the ones that would embarrass it and installs the two quality gates the project has
been claiming but not enforcing.

It is **release hardening, not feature work.** Every item here is either a known defect with a known
fix, or a check that should have been running all along.

---

## 2. Locked decisions

1. **Scope is defects + infrastructure. Six backlog items are deliberately NOT in this phase** — see
   §7. They are features and design decisions, and folding them into a cleanup phase would smuggle
   several phases of work under one version number while forcing design calls that deserve their own
   consideration.
2. **mypy runs at DEFAULT settings, not `--strict`.** Measured on `main`: 71 errors in 20 files at
   default, 173 in 31 under strict. Default already catches this codebase's real bug class; strict is
   2.4× the work and most of the difference is annotation churn, not defect-finding. Strict adoption
   is filed as a post-1.0.0 item.
3. **Item 11 is fixed by draining each project's backlog per tick**, as the backlog entry proposes —
   not by a bounded batch, which would add a tuning constant nobody has a principled value for.
4. **This phase branches from a `main` that already contains Phase 8.** It closes backlog item 19,
   touches `llm/client.py`, and typechecks `llm/ollama.py` — all of which assume v0.9.0 has landed.

---

## 3. The defects (group A)

Ten fixes. Each names the item, what is actually wrong, and what "fixed" means.

### 3.1 `parse()` returns an unguarded `None` — `llm/client.py`

`AnthropicLLM.parse` ends `return response.parsed_output` with no `None` guard. This is the **same
defect Phase 7 fixed one method over** in `extract_image`, where a response stopping on `max_tokens`
yielded `None` and the caller crashed on attribute access. `parse` is the older and far more used
path — the relevance filter, crystallizer, interpreter, synthesis and both caches all go through it —
and nobody has audited what its callers do with a `None`.

Not currently in the backlog; found by mypy, which flags it as `return-value`. **Fixed** means the
`None` case raises something that names the cause, and a test pins it.

### 3.2 A poisoned task fails without telling anyone — item 16

`Dispatcher._fail_poison` is the one path to `FAILED` that does not notify. **Fixed** means it
notifies like every other terminal state, with a test that fails if the notifier is removed.

### 3.3 A second clarifying question is never delivered — item 17

Notification is keyed on a *state change*, so a second question asked without the task leaving
`needs_clarification` in between is never sent. The human sees it only in the dashboard. **Fixed**
means a new question is delivered even without an intervening state change — via
`last_notified_question` or by hashing the announced text, implementer's choice.

### 3.4 `dead_letters` has no retention — item 18

`MAX_PAYLOAD_CHARS` bounds row *size*, not row *count*. A bad token writes one row per minute at the
60-second backoff cap, forever. **Fixed** means bounded growth, count-based rather than time-based —
the table's purpose is that a drop is never silent, so the newest rows are the ones that must
survive.

### 3.5 An unfetchable image is re-dead-lettered on every drive — item 19

With no image bytes there is no `sha256(image_bytes)` to key a "do not retry" record on, so the
failure is retried and re-dead-lettered every drive. **Fixed** means a secondary key space — the
URL hashed to a `url_sha256` — so an unfetchable source is recorded once.

This is the durable fix Phase 7 deliberately deferred; it also closes that phase's silent-fallback
rationale, since a recorded failure is what distinguishes "never had an image" from "had one and
could not read it".

### 3.6 One slow project paces every other — item 11

Each project drains **one task per sweep tick**, so a project with a slow task holds up every other
project's queue. **Fixed** means a project works its whole backlog under one tick and each project's
lane finishes independently (decision 3). Per-project lanes already exist from Phase 5; this changes
what a lane does per tick, not the concurrency machinery.

### 3.7 Three small correctness-and-clarity items — 10, 13, 15

- **10:** a false comment plus a redundant read in `workflow_repository.py`. Delete both; add a test
  that would have caught the redundancy.
- **13:** `ProjectIn.name` accepts anything at all. Add the length bound, matching `PromoteIn`.
  Validation only, not a schema change.
- **15:** `project_queue` and `queue_depth` disagree by design and nothing says so. Make the
  disagreement explicit — a docstring at minimum, a rename if the implementer judges it clearer.

### 3.8 The dashboard shows a stale list after an action — item 8

Item 8 is filed as "frontend polish", but its first entry is a real user-visible defect: **the
Registry list does not refresh after a promotion made from a `BundlePanel` on the same page**, so the
user performs an action and the page appears not to have registered it. On a project whose dashboard
is the thing a reader actually clicks, that is not polish.

**Fixed** means the list reflects a promotion made on the same page, pinned by a test. Two smaller
real items ride along because they are in the same files: `PromoteControl`'s Cancel clears `error`
but not `name`/`description`, so reopening shows stale input; and `Registry`'s `load` is redefined
every render.

The item's fourth entry — `BundlePanel.test.tsx` and `TaskDetail.test.tsx` mixing `test(...)` and
`it(...)` where every other file uses `test(...)` — is cosmetic. Fix it while in the file or leave
it; it does not gate the item.

---

## 4. The quality gates (group B)

### 4.1 mypy, at default settings, enforced in CI — the "typecheck clean" line

Spec §7 has required "typecheck clean" since v0.1.0, but CI runs pytest only and the backend has
**no Python typechecker configured at all**. That definition-of-done line has been unverifiable for
eight phases.

**Fixed** means: mypy added to `[dev]` in `pyproject.toml` at a pinned version, its configuration in
`pyproject.toml` beside the pytest config, all errors resolved, and a CI lane that fails the build on
a new one.

**The errors are fixed, not silenced.** A `# type: ignore` is acceptable only where the alternative
is worse and the reason is written next to it. The distribution on `main` was 29 `attr-defined`,
14 `return-value`, 13 `arg-type`, 6 `valid-type`, 5 `union-attr`, 4 `assignment`; the worst files
were `persistence/repository.py` (18), `llm/heuristic.py` (8), then the orchestrator, driver and
adapters at 5 each. Expect Phase 8's files to add a few.

### 4.2 A Postgres lane in CI — item 9

The whole suite runs on SQLite, while `docker compose up` deploys Postgres. **Postgres-only bugs have
already bitten this project**: a naive-vs-aware datetime comparison that SQLite cannot reproduce, and
a `json`-column equality issue that shaped two table designs. The suite has never run against the
database the project actually ships on.

**Fixed** means a `postgres:16` service in the CI backend job with `DATABASE_URL` pointed at it,
mirroring compose's `db` service, and the suite run against it. The SQLite lane stays — it is the
fast local loop and the documented dev path.

### 4.3 Close the two coverage gaps — items 7 and 12

- **7:** the test-coverage gaps carried forward from Phase 5.
- **12:** nothing tests `HeuristicLLM`'s `ProjectChoice` rule, because a blanket `except` launders
  the `NotImplementedError` into a default. Assert on `decision.reason` / `decision.stage` so the
  offline route is pinned rather than laundered.

---

## 5. Testing

- Every fix gets a test that **fails without it**, verified by mutation. "A guard that only appears
  pinned" has recurred in every recent phase — a test that passes both before and after a mutation
  has pinned nothing.
- The bar stays 0 failures, 0 skipped, 0 warnings, on **both** database lanes.
- No test may reach the network.
- Items 3.4, 3.5 and 3.3 may need schema changes; each gets a migration and the drift guard stays
  green.

---

## 6. Definition of done

- All ten group-A defects fixed, each pinned by a mutation-verified test.
- `mypy ley_khaa` clean at default settings, enforced by a CI lane that fails on a new error.
- The suite passes against **Postgres** as well as SQLite, both in CI.
- Backlog items 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19 marked closed with their commit.
- The six deferred items (§7) each carry a written reason for deferral.
- Full suite green on both lanes, 0 skipped, 0 warnings.

---

## 7. Deliberately deferred past 1.0.0

These are **features and design decisions, not defects.** Each stays open with its reason recorded,
so 1.0.0's backlog is honest roadmap rather than hidden debt.

| Item | Why it is not in this phase |
|---|---|
| **1.** Memory does not learn paraphrases | A feature (new `memory_fingerprints` table), not a defect. The registry learns aliases; memory not doing so is a capability gap. |
| **2.** The recall candidate cap is load-bearing | Needs measurement before a change — the right cap is an empirical question, not a cleanup. |
| **3.** No management surface for task memory | A feature: new API and dashboard surface. |
| **20.** A same-backend model failure stays frozen | Needs a retry-count column — a schema change and a policy decision (how many retries, over what window) that is the user's to make. |
| **21.** Vision does not work offline | Needs a vision-capable local model. Nothing in the contract, fetcher or cache changes for it; it is a roadmap item, not a fix. |
| **22.** No runtime step-down between backends | The backlog entry says it deserves its own phase, and it does: a per-call fallback makes the producer a property of the *call*, breaking the `LLMClient.name` contract the manifest depends on. |

Also deferred: **mypy `--strict`** (decision 2), filed as a post-1.0.0 ratchet.

---

## 8. Known limits, stated up front

- **A Postgres CI lane does not make the suite database-agnostic.** It proves the suite passes on
  both, not that every behaviour is identical across them. The three tz-aware helpers exist because
  the two databases genuinely differ.
- **mypy at default settings will not catch everything strict would** — missing annotations and
  implicit `Any` stay legal. That is the accepted trade of decision 2.
- **Draining a whole backlog per tick changes the latency profile**: a project with many queued tasks
  now occupies its lane longer in one tick. That is the intended fix for head-of-line blocking, but
  it is a change in behaviour, not purely a bug fix.

---

## 9. Out of scope

- Any of the six items in §7.
- New features of any kind. If a fix appears to need one, that is a signal the item belongs in §7.
- Frontend work beyond item 8's entries and what a fixed backend contract requires.
