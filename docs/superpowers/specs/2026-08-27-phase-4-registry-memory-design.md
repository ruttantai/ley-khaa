# Phase 4 (v0.5.0) — The Workflow Registry and Task Memory: two caches that learn

**Status:** approved 2026-08-27
**Implements:** §5.6 (workflow registry as a learned cache), §5.14 (task memory) of
`2026-08-18-ley-khaa-design.md`.
**Builds on:** `2026-08-25-phase-3-executor-design.md`, which deferred §5.6 here on purpose.
**Explicitly deferred:** §5.4 (project routing, concurrent per-project queues, amendment
detection), §5.1 real Slack/Discord adapters, §5.2 vision intake, Word output, auto-promotion.

## 1. Goal

Phase 3 made the system able to do arbitrary work: every task is solved by a Python script an
Opus call writes for it, run in a sandbox. That is the right default and the wrong steady state —
the tenth identical universe check re-derives, re-pays for, and re-risks a program that was
already proven eight times over.

This phase adds the two caches §5.6 and §5.14 describe, and they chain:

```
messages ─▶ crystallize ─▶ [MEMORY?] ─▶ interpret ─▶ dial ─▶ [REGISTRY?] ─▶ synthesize ─▶ sandbox ─▶ validate
                             │ hit                              │ hit
                             └──── remembered TaskSpec ─────────┴──▶ frozen script ──▶ sandbox ──▶ validate
```

Memory short-circuits the interpreter. The registry short-circuits synthesis. Both are Opus calls,
so the measurable claim this phase has to earn is:

> **The second time the same request arrives, it is served with zero LLM calls and produces
> byte-identical output.**

That is the definition of done, and it is a test, not a story.

## 2. Decisions

Settled during the 2026-08-27 brainstorming session. Not open for re-litigation during execution.

1. **Registry and memory ship in the same phase.** They are one loop — "have I seen this
   before?" — and they share one matching shape. Building them in separate phases means building
   the matcher twice and designing the second one around the first one's accidents.
2. **Deterministic fingerprint first, one cheap model call on a miss.** This is the Crystallizer's
   proven cheap-filter-then-LLM shape (§5.3), reused. It is not only a cost decision: the
   deterministic half is what keeps both fast paths alive with **no `ANTHROPIC_API_KEY`**, because
   `HeuristicLLM` answers "no match" and the system falls back to fingerprint-only matching rather
   than losing the feature.
3. **Promoted workflows are database rows, promoted by a human through the dashboard.** §5.6 says
   curated in v1. A row carries the frozen source, its hash, and provenance back to the bundle it
   came from, so a promoted capability is inspectable and revocable. Auto-promotion after N clean
   runs is rejected for v1: it lets LLM-written code become permanent with nobody having read it.
4. **Every synthesized script reads `inputs/params.json`.** The synthesizer contract changes so
   scripts never hardcode filenames. Promotion is then a *pure copy* of proven source, and a
   cached run is the same bytes against a different binding. The alternative — rewriting the
   script at promotion time — was rejected outright: the code that gets frozen would not be the
   code that was proven, which breaks the audit chain the whole bundle design rests on.
5. **A memory hit fills the spec; the dial decides what happens next.** Memory does not get its
   own approval policy. It contributes a `familiarity` signal to the existing deterministic
   autonomy engine and the engine recommends a mode as it always has.
6. **Inputs are never remembered.** A remembered spec reuses `operation`, `output_format`, and
   input *names*; the resolver re-resolves those names against the **current** task's attachments
   and catalog on every run. Last week's spec must not be able to quietly reuse last week's file.
7. **A cached run is validated exactly like a synthesized one.** A wrong cache hit is worse than a
   cache miss — it runs confident, deterministic code that computes the wrong thing — so the fast
   path buys no trust with the validator.
8. **The state machine does not change.** Both caches live inside existing steps (`_interpret`,
   `_execute`). No new states, no new edges. Same discipline as Phase 3.

## 3. Architecture

Two new packages, mirroring `executor/`:

```
backend/ley_khaa/registry/      backend/ley_khaa/memory/
├── models.py    row + signature ├── models.py    row
├── fingerprint.py  normalize    ├── fingerprint.py  normalize
├── matcher.py   2-stage match   ├── matcher.py   2-stage match
├── binder.py    roles -> files  ├── recall.py    hit -> TaskSpec
├── promote.py   bundle -> row   └── record.py    DONE -> row
└── seeds/       two workflows
```

Both matchers have the same shape and the same failure mode: **no match is always a legal answer,
and it costs only a fall-through to the existing path.** Neither may raise into the driver.

### 3.1 The `params.json` contract

The runner writes this into `inputs/` before the sandbox starts, on **both** lanes:

```json
{
  "inputs": {"bloomberg": "bloomberg_universe.csv", "factset": "factset_universe.csv"},
  "output": "deliverable/output.xlsx",
  "seed": 20260827
}
```

Keys are the spec's own input names. The synthesis prompt gains a rule: read paths from
`inputs/params.json`, never hardcode a filename, resolve inputs relative to `inputs/`. The offline
`HeuristicLLM` scripts change to match.

`params.json` lands in `inputs/`, so it is covered by the existing `input_hashes()` tamper check
and travels with the bundle — re-running `generator/run.sh` from the bundle root reproduces the
run faithfully, binding included.

At promotion, those keys become the workflow's **roles**. A cached run writes `params.json` using
the *workflow's* role names bound to *this* run's files, so the frozen script reads the names it
was born with and needs no rewriting.

### 3.2 Registry data model

`WorkflowRow`:

| column | meaning |
|---|---|
| `name` | unique, human-chosen at promotion (`set_difference`) |
| `description` | one line, shown in the dashboard and in the matcher prompt |
| `operation_aliases` | JSON list of normalized operation strings that match this workflow |
| `output_format` | as promoted; compared via `formats.expected_suffixes()`, not by string |
| `inputs` | JSON `[{"role": "bloomberg", "suffixes": [".csv"]}]`, ordered |
| `source` / `source_sha256` | the frozen script and its hash |
| `origin` | `seed` or `promoted` |
| `promoted_from_task_id` / `promoted_at` | provenance back to the bundle that proved it |
| `runs_ok` / `runs_failed` / `last_used_at` | usage, shown in the dashboard |
| `quarantined` | set on a failed cached run; blocks matching until a human clears it |

### 3.3 Matching and binding

**Stage 1 — fingerprint (free, offline, auditable).** A candidate is a non-quarantined workflow
where the normalized `spec.operation` is in `operation_aliases`, the input count is equal, and the
output formats agree — meaning `formats.expected_suffixes()` returns the same non-empty tuple for
both. Comparing raw strings would make `excel` and `xlsx` different formats when the existing
module already knows they are the same. Operation normalization: lowercase, non-alphanumeric to
`_`, collapse runs, strip edges.

**Stage 2 — one Haiku call, only on a miss.** The spec's intent/operation/inputs/output_format and
every workflow's name, description, and signature go to `Stage.REGISTRY_MATCH`; the model returns a
workflow name or null, plus a confidence and a reason. Gated at **≥ 0.8**. `HeuristicLLM` returns
null, so offline behaviour is fingerprint-only.

**Stage 3 — bind, and refuse to guess.** Resolved inputs are bound to the workflow's roles in
declared order, checking each file's suffix against the role's allowed suffixes. Any mismatch,
count difference, or ambiguity is **not a match** — it falls through to synthesis. A bind failure
is never resolved by picking the likeliest file.

**The learning bit.** When a stage-2 match then *passes validation*, that run's normalized
operation is appended to the workflow's `operation_aliases`. The same phrasing is a free stage-1
hit forever after. This is the only way aliases grow, and it is visible as a row a human can edit.

### 3.4 Executing the fast path

Inside `ExecutionRunner.run`, after inputs resolve and before synthesis:

1. Match and bind. No match → today's synthesis path, unchanged.
2. Write `params.json` for the binding; `write_generator(n, workflow.source)` so the bundle still
   contains the exact code that ran and `run.sh` still points at it.
3. Run the same sandbox, judge with the same validator.
4. Pass → `runs_ok += 1`, `last_used_at`, alias learning. Manifest records the cached lane.
5. Fail → `runs_failed += 1`, **`quarantined = True`**, and fall back to synthesis *within the same
   run*. Both attempts stay in `generator/`.

The registry lane gets **exactly one attempt**, and it does not consume either of the two synthesis
attempts (`_MAX_ATTEMPTS`). A cached script is proven code, not a draft: if it fails it is wrong for
this request, and re-running it unchanged would fail identically. So there is no repair loop on the
cached lane, and a quarantining failure still leaves a full synthesize-and-repair cycle to rescue
the task — worst case three sandbox runs.

Manifest additions: `"lane": "registry" | "synthesis"`, and on a cached run
`"workflow": {"name", "sha256", "matched_by": "fingerprint" | "model", "binding": {...}}`.
`models.synthesis` is **null** on a cached run — no model wrote that script, and the manifest may
not claim otherwise (the rule fixed in `ef55897`).

### 3.5 Task memory

`MemoryRow`: `project`, `fingerprint` (indexed), `intent`, `spec` (JSON), `source_task_id`,
`times_seen`, `last_seen_at`.

**Recorded** only when a task reaches `done` with a passing verdict — the same "proven before it is
cached" rule promotion uses. A repeat hit increments `times_seen` rather than inserting a row.

**Fingerprint:** the significant token set of the request's source-message texts — lowercased,
split on non-alphanumerics, bare numbers dropped, a small module-level stopword frozenset dropped
(articles, pronouns, auxiliaries, and the politeness words a chat request is full of: `please`,
`thanks`, `hi`, `can`, `could`, `would`), deduplicated, sorted, hashed — scoped by
`TaskRow.project`. Deterministic and explainable; paraphrase is what stage 2 is for. The stopword
list lives in one place and is pinned by a test, so it cannot drift silently and start splitting
requests that used to fingerprint together.

**Recall** happens in `TaskDriver._interpret`, before the Interpreter call, with the same two
stages (`Stage.MEMORY_MATCH`, gated ≥ 0.8, offline returns null). On a hit the remembered spec is
reused with `source_message_ids` re-pointed at the current task's messages. Nothing else is
copied: `inputs` are names, and the resolver resolves them against this task at execution time.

**The dial.** `recommend()` gains a `familiarity: int` argument — the remembered `times_seen`, 0
when the spec was freshly interpreted. It contributes **+0.05 of confidence per remembered run,
capped at +0.15**, and a reason clause a human can argue with: *"I've done this 3 times before."*
The cap matters: familiarity must be able to help a good spec over the line, never to drag a spec
with missing fields there on repetition alone — the `_MISSING_FIELD_PENALTY` of 0.2 stays strictly
larger than the whole bonus. These numbers join the four already pinned by
`test_autonomy_engine.py`, so a drift fails a test rather than quietly loosening the dial.

### 3.6 Model routing

Two new stages, both Haiku at both complexities, both small: `REGISTRY_MATCH` and `MEMORY_MATCH`
(`max_tokens` 1024). They are cost-avoidance calls; routing them to Opus would defeat their
purpose.

### 3.7 Persistence

Alembic `0004_registry_memory`: `workflows` and `task_memory` tables. The two seed workflows are
installed at startup instead, by `ensure_seed_workflows` (`registry/seeds/__init__.py`, called
from `api/app.py`) — deliberately not in the migration, because a migration that imports
application code rots when that code moves. The existing model-drift guard covers both new
models.

## 4. Seed workflows

`set_difference` and `summary_stats` ship as `origin=seed` rows whose source is **hand-written, not
LLM-written** — they are what a hardened, promoted capability is supposed to look like, and they
double as fixtures. Both read `params.json`, both are deterministic, both write one deliverable.
They satisfy the v1 DoD line *"a request matching a seed registry workflow takes the fast path and
runs the proven code."*

## 5. API and dashboard

| endpoint | purpose |
|---|---|
| `POST /tasks/{id}/promote` | `{name, description}` → a `WorkflowRow` from that task's winning attempt |
| `GET /registry` | list workflows: origin, signature, uses, quarantine state |
| `POST /registry/{name}/unquarantine` | a human clears a failed workflow |
| `DELETE /registry/{name}` | remove a promoted workflow |

`promote` guards: the task must be `done` with a passing verdict; the source is read from the
bundle attempt that produced the deliverable, through the **existing `_contained()` check** — the
workspace is written by untrusted generator code, so every candidate path is resolved and
containment-checked before it is read (the Task 11 ruling); the name must be unique and match a
conservative pattern.

Dashboard: a **Promote** button on a passing bundle panel, a **Registry** page listing workflows
with their origin and usage, and a **remembered** badge on a task whose spec came from memory,
linking to the source task.

## 6. Error handling

| failure | behaviour |
|---|---|
| Matcher raises anything | swallowed at the boundary, logged, treated as no match |
| Match model call fails or times out | no match; the run synthesizes as before |
| Bind ambiguous or suffix mismatch | no match; never guessed |
| Cached script crashes or fails validation | quarantine the workflow, fall back to synthesis in the same run |
| Promote against a failed or unfinished task | 409, no row written |
| Duplicate workflow name | 409 |
| Memory recall raises | swallowed; the Interpreter runs normally |
| Remembered spec no longer resolves its inputs | the existing `UnresolvedInputs` clarification path, unchanged |

The rule behind every row: **a cache that fails must cost only the work it was trying to save.**

## 7. Testing

- Unit: normalizers, fingerprints, the two matchers (both stages, the confidence gate, the offline
  null path), the binder's refusals, alias learning on a passing stage-2 match.
- Contract, `[docker]`, against the real image: both seed workflows produce correct deliverables.
- Quarantine: a poisoned cached workflow fails, quarantines, and the same run still succeeds via
  synthesis, with both attempts in `generator/`.
- Manifest honesty on the cached lane: `lane`, `workflow.sha256` matching the row, `binding`
  recording what was actually bound, and `models.synthesis` null.
- Memory: recorded only on a passing `done`; `times_seen` increments; inputs are re-resolved, not
  remembered; the dial's familiarity clause appears.
- Security: promotion cannot read a path outside the bundle.
- **End-to-end, the phase's whole claim:** run the same request twice against a counting fake LLM;
  the second run makes **zero** calls and produces a byte-identical deliverable.
- Migration drift guard passes; frontend tests for the Promote button, the Registry page, and the
  remembered badge; `npx tsc --noEmit` clean.

## 8. Out of scope

§5.4 project routing, concurrent per-project queues, and amendment detection · real Slack and
Discord adapters · vision intake · Word output · auto-promotion · workflow versioning (a promoted
name is replaced by delete-then-promote, not versioned) · cross-project memory sharing.

## 9. Definition of done

- A request matching a seed workflow takes the fast path, runs the proven code, and its manifest
  says so.
- A synthesized script that passes can be promoted from the dashboard and is used by the next
  matching request.
- A request phrased differently is matched by the model once, then matched free by fingerprint
  afterwards.
- A repeated request has its spec filled from memory with no interpreter call, and the dial's
  reason says how many times it has been seen.
- **The same request, asked twice, is served the second time with zero LLM calls and byte-identical
  output.**
- A poisoned cached workflow quarantines itself and the task still completes.
- `docker compose up` on a fresh clone with no `ANTHROPIC_API_KEY` still demonstrates both caches,
  fingerprint-only.
- Backend and frontend suites green, no skips, no warnings; typecheck clean.
