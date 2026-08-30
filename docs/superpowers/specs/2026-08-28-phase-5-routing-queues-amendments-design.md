# Phase 5 (v0.6.0) — Project routing, concurrent queues, and the amendment detector

**Status:** approved 2026-08-28
**Implements:** §5.4 (project router), §5.9's amendment detector and concurrent execution, of
`2026-08-18-ley-khaa-design.md`.
**Builds on:** `2026-08-27-phase-4-registry-memory-design.md`, which deferred §5.4 here on purpose.
**Also closes:** items 4, 5 and 6 of `2026-08-28-phase-5-backlog.md`.
**Explicitly deferred:** §5.1 real Slack/Discord adapters, §5.2 vision intake, Ollama fallback,
backlog items 1, 2, 3, 7 and 8 (memory fingerprint aliases, the recall-cap follow-up, the memory
management surface, the carried-forward test gaps, and the frontend polish list).

## 1. Goal

Every task ley-khaa has ever run has been `project="default"`, hardcoded at the single
`TaskRepository.create` call site, and has executed **inline on the request thread**: `POST
/messages` promotes a candidate, calls `TaskDriver.advance()`, and does not return until synthesis
has made two Opus calls and a sandbox has run. One task at a time, one project, and an HTTP request
that can block for minutes.

That is the last structural gap between what the system does and what §5.4 and §5.9 describe. This
phase closes it in three moves that are really one:

```
                          ┌── project A queue ──▶ worker A ──▶ advance() ──▶ …
message ─▶ crystallize ─▶ ROUTE ──┼── project B queue ──▶ worker B ──▶ advance() ──▶ …
                     │    └── project C queue ──▶ worker C ──▶ advance() ──▶ …
                     │
                     └─▶ AMENDS an active task in that project? ──▶ fold it in, or ask
```

The claim this phase has to earn, stated so it can only be settled by a test:

> **Two clients' requests route to their own projects and run at the same time; two requests in the
> same project never do; and a mid-flight follow-up amends the running task instead of spawning a
> duplicate.**

## 2. Decisions

Settled during the 2026-08-28 brainstorming session. Not open for re-litigation during execution.

1. **A DB-backed queue with in-process workers.** No broker, no Redis, no second process type.
   `docker compose up` stays one command and the SQLite dev loop keeps working. Claiming is an
   optimistic conditional `UPDATE`, which is what `TaskRepository.claim()` already is.
2. **The queue is not a table.** It is the `tasks` table filtered by project, state and lease. A
   separate queue table would be a second source of truth about what should run, and the two would
   drift.
3. **One worker per project.** Serial within a project, concurrent across projects. This is the
   direct reading of §5.4's "each project has its own task queue", and it is what makes amendment
   detection tractable — the set of tasks a new request could be amending stays small and stable.
4. **Routing is two-stage, deterministic first.** A free binding lookup, then one Haiku call only
   on a miss, gated at a confidence floor and treated as untrusted. This is the third instance of a
   shape `RegistryMatcher` and `MemoryMatcher` already prove; a third divergent matcher would be
   the anomaly, not the consistency.
5. **The autonomy dial decides whether an amendment folds, with a structural guard above it.** The
   dial is headline feature #2 and this gives it a second, genuinely different kind of decision to
   govern. But a target at `EXECUTING` or `VALIDATING` always parks for a human regardless of mode —
   that is a structural fact about live workspaces, not a threshold to tune.
6. **The amendment decision is held on the candidate, not on a placeholder task.** "Does this
   candidate become its own task?" is the question `CandidateState` already models.
7. **Backlog items 5 and 6 ship here, not later.** They are cosmetic races on a threadpool today
   and reachable ones the moment real workers exist. Item 4 is settled by this phase by definition.

## 3. Architecture

### 3.1 What does not change

`TaskDriver.advance()` is untouched. It is already re-entrant, synchronous, single-task, and every
step already ends in a state claim that makes a duplicate pass a no-op. The whole phase is a change
of **who calls it** — today HTTP handlers and the sweeper, inline; after this phase, a worker.

That is the seam, and it is the reason this phase is additive rather than a rewrite.

### 3.2 The lease

Migration `0005` adds three columns to `TaskRow`:

| column | meaning |
|---|---|
| `lease_owner` | the worker id currently driving this task; `NULL` when free |
| `lease_expires_at` | when the lease goes stale; extended by heartbeat while work is in flight |
| `lease_attempts` | how many times this task has been reclaimed from an expired lease |

`TaskRepository.claim_lease(task_id, *, owner, ttl)` is a conditional update:

```sql
UPDATE tasks
   SET lease_owner = :owner,
       lease_expires_at = :now + :ttl,
       lease_attempts = lease_attempts
                      + CASE WHEN lease_owner IS NULL THEN 0 ELSE 1 END
 WHERE id = :task_id
   AND (lease_owner IS NULL OR lease_expires_at < :now)
```

The `CASE` is load-bearing and easy to get wrong: `lease_attempts` counts **reclaims of an expired
lease**, not claims. Incrementing unconditionally would count every ordinary hand-off between
states, so a healthy task that simply passed through several steps would trip the cap and fail for
no reason. A test pins this directly: a task driven normally through its whole lifecycle ends with
`lease_attempts == 0`.

`rowcount == 1` means we won it; `0` means another worker holds it and this caller simply stops —
the same contract as `claim()` and `claim_for_promotion()`. It is portable: no `FOR UPDATE SKIP
LOCKED`, so Postgres and the SQLite dev path behave identically.

`release_lease(task_id, owner)` clears the lease, guarded on `lease_owner = :owner` so a worker
whose lease already expired cannot clear its successor's.

**This is what finally makes `EXECUTING` recoverable.** `Orchestrator.advance_stalled()` excludes
`EXECUTING` today for a documented reason: nothing leases the row, so a sweeper re-drive would
relaunch paired Opus calls onto a live workspace every 15 seconds with no ceiling. A heartbeated
lease is exactly the missing piece — only an **expired** lease is reclaimable, which means the
worker process actually died. `lease_attempts` caps the retry: past
`settings.max_lease_attempts`, the task goes to `FAILED` with a reason naming the lease, rather
than looping forever on a poison task.

### 3.3 The dispatcher

`orchestrator/dispatcher.py`:

- `runnable_projects()` asks the repository for the distinct projects holding at least one runnable
  task — a task whose state is neither terminal nor human-waiting, and whose lease is free or
  expired. "Human-waiting" is `driver._WAITING`, promoted from a module-private to a shared
  constant rather than restated: two definitions of which states block on a person would drift, and
  the one that drifted would silently either stall tasks or drive tasks out from under a human.
- The supervisor keeps at most one in-flight worker per project, and at most
  `settings.max_concurrent_projects` overall.
- A worker claims the project's oldest runnable task by lease, then runs `advance()` in a thread
  (`asyncio.to_thread`, as `_periodic_sweeper` already does, so the sync orchestrator never blocks
  the event loop), heartbeating the lease from the async side while that thread is busy.
- On return it releases the lease and loops. On an exception it releases, logs, and lets the
  ordinary retry path apply — a dispatcher that dies on one bad task is worse than the inline
  execution it replaces.

The existing `_periodic_sweeper` keeps its job (promoting candidates whose debounce has elapsed)
and loses nothing to the dispatcher; they run side by side in `lifespan`.

### 3.4 Inline mode, and why it exists

HTTP handlers stop calling `advance()`. `POST /messages`, `/approve`, `/answer`, `/mode` and
`/spec` now mutate state and return immediately; the dashboard already polls.

This changes what roughly five hundred existing tests **mean** — many assert that a task reached
`done` synchronously after a request. `settings.dispatch_mode` (`LEY_KHAA_DISPATCH`) takes
`inline` or `workers`:

- `inline` — the call sites drive the task themselves, exactly as they do today. Pinned in
  `conftest.py` beside `LEY_KHAA_LLM=heuristic` and `LEY_KHAA_DEBOUNCE_SECONDS=0`.
- `workers` — the app default. Call sites enqueue and return.

The existing suite therefore keeps its meaning instead of being rewritten into something weaker,
and a dedicated suite exercises the worker path, leases, expiry and concurrency on its own terms.
Inline mode is not a test-only shim: it is also the honest configuration for a single-operator
CLI-style run, and it is documented as such rather than hidden.

### 3.5 Project routing

Two tables, in migration `0005`:

**`projects`** — `name` (slug, primary key), `display_name`, `description`, `active`,
`created_at`. The `description` is not decoration: it is what the stage-2 prompt reasons over, so a
project with an empty description is unroutable by stage 2 by construction, and the API says so on
creation.

**`project_bindings`** — `source`, `client`, `conversation_id` (nullable), `project`,
`created_by_stage`. `MessageRow` already carries `source` (the channel) and `client`, so no intake
field is added. Most-specific wins: a binding with a `conversation_id` beats one without, so a
project can claim a whole client *and* a single conversation can be pinned elsewhere.

`ProjectRouter.route(candidate) -> RoutingDecision(project, stage, confidence, reason)`:

- **Stage 1, free.** Binding lookup on the candidate's messages' `(source, client,
  conversation_id)`, most-specific first. This is the steady state: after the first routed
  conversation, every later message in it is free.
- **Stage 2, one Haiku call, only on a miss.** Classify the candidate's title and summary against
  the active projects' descriptions. Gated at `ROUTING_CONFIDENCE_FLOOR`. The answer is untrusted:
  the named project must exist and be active, or the decision falls back to `default`.
- **The learning rule.** A stage-2 match at or above the floor **writes the binding** for that
  conversation, so it is free forever after. This mirrors the registry appending an alias to the
  proven row — and it deliberately gets right the thing backlog item 1 records memory getting
  wrong: it writes one binding per conversation, it does not fork a row per phrasing.

`HeuristicLLM` gains a deterministic `_route` so CI and `docker compose up` stay green with no
`ANTHROPIC_API_KEY`, exactly as `_interpret` and the matchers already do.

**Call site:** `Orchestrator._promote`, replacing `project="default"`. Still the only
`TaskRepository.create` call site.

**Seeding:** a `default` project must always exist. It is installed idempotently at startup, the
way the seed workflows are — and, per the lesson that produced commit `8cebd1f`, the migration's
docstring says the migration creates the *table* and does not claim to seed the *row*.

### 3.6 Settling the scoping asymmetry (backlog item 4)

Memory stays scoped by project; the registry stays global. Recorded as a decision with its reason,
not left to default:

> A remembered `TaskSpec` carries `recipient` and is reused wholesale, so sharing one across
> clients misdelivers work. A workflow is code, identified by the sha256 of its source and bound
> positionally to a run's own inputs; it carries no client data and reusing it across clients is
> the entire point of a cache.

Phase 4's CHANGELOG and matcher comments currently say "every task is `project='default'`, so
memory is shared across every client" — a true statement that **stops being true in this phase**.
Updating those two places is part of the phase, not a follow-up.

### 3.7 The amendment detector

`AmendmentDetector.detect(candidate, active_tasks) -> AmendmentProposal | None`, called from
`_promote` after routing:

- **Stage 1, free.** Are there any active tasks in the routed project? Almost always no, and then
  the phase costs nothing on the common path. **Active** means any state that is not `DONE` or
  `FAILED` — deliberately including `AWAITING_APPROVAL` and `NEEDS_CLARIFICATION`, because a task
  parked in front of a human is exactly the one a follow-up message is most likely to be amending.
- **Stage 2, one Haiku call.** Given the new candidate and the active tasks' titles and specs, does
  this amend one of them? Returns `task_id | none`, a confidence, and a one-line human-readable
  reason. Untrusted: the named task must exist, be active, and belong to that project, or the
  proposal is discarded.

Below `AMENDMENT_CONFIDENCE_FLOOR`, there is no proposal and the candidate promotes normally.

### 3.8 Deciding, and folding

**The dial decides.** The autonomy engine scores the proposal from the detector's confidence, the
target's current state, and whether the target's spec has missing fields — using the **target
task's** `effective_mode`, because it is that task's work being changed. `AUTO` above threshold
folds automatically. `SUGGEST` and `COPILOT` park it for a human.

**The guard overrides the dial.** A target at `EXECUTING` or `VALIDATING` has a live sandbox
workspace and a half-written bundle. It always parks, whatever the mode says. This is structural: it
is expressed as a state-set membership test outside the scoring path, so no threshold change can
reach past it.

**Folding reuses a proven path.** It is what `_route_reply` already does for an answered
clarification: append the candidate's message ids to the target's `source_message_ids`, claim the
target back to `CLASSIFIED`, clear its `open_question`, re-enqueue. The interpreter then re-reads
the enlarged message set, which is precisely the behaviour wanted — the amendment is *interpreted*,
not stapled on.

It needs two new edges in `domain/states.py::_ALLOWED`:

- `INTERPRETED → CLASSIFIED`
- `AWAITING_APPROVAL → CLASSIFIED`

Both are genuinely reachable by this phase's code, and each gets its own test. Phase 2 removed a
declared-but-unreachable edge (`INTERPRETED → NEEDS_CLARIFICATION`); this phase does not add one
back.

**The race.** The target can move between the decision and the fold. The fold is therefore a
conditional claim on the state that was observed when the decision was made. If the claim loses,
the target has moved on — the candidate returns to triage with a fresh proposal rather than folding
into a task that is no longer where it was. Loser stops; same contract as everywhere else.

### 3.9 Holding a parked decision

`CandidateState` gains `AWAITING_TRIAGE`, reachable from `READY`, terminating in `PROMOTED` or
`ABANDONED`. `CandidateRow` gains `amends_task_id`, `amendment_reason`, `amendment_confidence`.

Auto-folding never enters `AWAITING_TRIAGE`: it goes `READY → PROMOTED` with `task_id` set to the
task it joined. That is honest — `PROMOTED` means "this candidate's request is now carried by task
X", which is exactly true of a folded candidate.

Two new repository claims, both conditional updates in the existing style:
`claim_for_triage(READY → AWAITING_TRIAGE)` and `claim_for_fold(AWAITING_TRIAGE → PROMOTED)`.

### 3.10 Backlog items 5 and 6

**Item 5 — read-modify-write counters.** `WorkflowRepository.record_success` / `record_failure`
become atomic (`SET runs_ok = runs_ok + 1`). The `operation_aliases` JSON append cannot be an
atomic increment, so it becomes a compare-and-swap on the observed value with one retry; on a lost
retry it gives up, which costs one extra Haiku call and never corrupts the list.

**Item 6 — writes landing before the claim that authorises them.** `save_memory_hit` and
`save_spec` move to after their state claim wins. `_remember` already does this correctly two
functions away and is the model to follow.

## 4. API and dashboard

| endpoint | purpose |
|---|---|
| `GET /projects` | list, with queue depth and what is currently leased |
| `POST /projects` | create (name, display name, description) |
| `GET /projects/{name}/queue` | that project's tasks in queue order |
| `GET /triage` | candidates in `AWAITING_TRIAGE` with their proposals |
| `POST /candidates/{id}/fold` | fold into `amends_task_id` |
| `POST /candidates/{id}/separate` | promote as its own task |

The dashboard gains a **Projects** view — a column per project showing its queue and its in-flight
task — and a **triage tray** listing parked amendment proposals with the detector's reason and both
buttons. Both follow `Registry.tsx`'s patterns. Task cards gain a project badge.

`POST /messages` gains `project` and `queued` in its response, since it no longer returns a task
that has finished.

## 5. Error handling

- **Stage-2 routing fails (transport, malformed, low confidence).** Fall back to `default`. Routing
  must never block intake; a misrouted task is recoverable, a dropped one is not.
- **Stage-2 amendment detection fails.** No proposal; the candidate promotes normally. A missed
  amendment costs a duplicate task, which a human can see and reject.
- **A worker dies mid-task.** Its lease expires, the dispatcher reclaims it once, `lease_attempts`
  caps the loop, and past the cap the task fails with a reason naming the lease rather than
  disappearing.
- **A fold's conditional claim loses.** Back to triage with a fresh proposal.
- **A project has no description.** It is unroutable by stage 2 and only reachable by binding.
  `POST /projects` says so; it is not silently accepted as a routable project.

## 6. Testing

The Phase 4 discipline, demanded in every dispatch and **verified in review** rather than accepted
on an implementer's self-report: for each new assertion, delete the behaviour it guards, watch the
test fail for the right reason, restore it. Phase 4 produced eight separate findings of tests that
passed for the wrong reason; self-reported mutation testing was wrong more than once.

The phase's headline claim is proven by a **barrier, not a sleep**:

- Two clients' conversations interleaved through intake; each task asserted to land in the right
  project.
- Tasks in **different** projects genuinely overlap — worker A blocks on a barrier only worker B can
  release, so serial execution deadlocks and times out. The test cannot pass by accident of timing.
- Tasks in the **same** project never overlap.

Also pinned, each as its own test:

- Two dispatchers racing one lease: exactly one wins.
- An expired lease reclaimed exactly once; `release_lease` guarded on owner.
- The `lease_attempts` cap failing a poison task instead of looping.
- Stage-2 routing writing its binding once, and the second message in that conversation making no
  model call (a real call counter around the offline `HeuristicLLM`, as
  `test_caches_end_to_end.py` does — not a canned fake).
- The structural guard blocking an `AUTO` fold on an `EXECUTING` target.
- A fold whose conditional claim loses returning the candidate to triage.
- `INTERPRETED → CLASSIFIED` and `AWAITING_APPROVAL → CLASSIFIED`, one test each.
- Concurrent `record_success` not losing an increment.
- Both dispatch modes covered: the existing suite on `inline`, the new suite on `workers`.

## 7. Out of scope

Real Slack and Discord adapters (§5.1), vision intake (§5.2), the Ollama fallback, priority or
urgency-based queue reordering (§5.9's "the autonomy engine may propose reprioritizing the queue" —
noted, not built), cross-project amendment detection, and backlog items 1, 2, 3, 7 and 8.

Queue reordering is called out explicitly because it is the one §5.9 sentence this phase reads and
does not implement: with one worker per project and FIFO within it, urgency has nowhere to act yet.
It wants the `TaskSpec`'s `urgency`, which is only known after interpretation — i.e. after the task
has already been dequeued. Doing it properly means a re-queue step, and that is its own piece of
work.

## 8. Definition of done

- A message from client A and a message from client B route into different projects, and their
  tasks run **at the same time** — proven by the barrier test, not by inspection.
- Two requests in the same project run one after the other, never together.
- A stage-2 routing decision writes a binding; the next message in that conversation routes free.
- A mid-flight follow-up in an active project is detected as an amendment, folds into the running
  task under `AUTO`, and parks for a human under `SUGGEST`/`COPILOT` or against an `EXECUTING`
  target.
- A folded amendment is re-interpreted over the enlarged message set — the resulting spec reflects
  the follow-up, it is not appended to the old one.
- `POST /messages` returns before execution finishes; the dashboard shows the task moving through
  its states.
- A killed worker's task is reclaimed once and completes; past the attempt cap it fails visibly.
- The dashboard shows per-project queues and a triage tray.
- The whole suite green on both dispatch modes, no skips, no warnings, `docker compose up` and CI
  green with no `ANTHROPIC_API_KEY`.
