# Phase 6 (v0.7.0) — Real Slack and Discord channel adapters

**Status:** approved 2026-08-30
**Implements:** §5.1 (channel adapters) and the §6.2 / §6.4 notify-back paths of
`2026-08-18-ley-khaa-design.md`; closes the §11 DoD line "Real Slack + Discord adapters function
when tokens are supplied; simulator works with none."
**Builds on:** `2026-08-28-phase-5-routing-queues-amendments-design.md`, whose per-client project
routing and dispatcher this phase feeds.
**Explicitly deferred:** §5.2 vision intake, the Ollama fallback, and every open item in
`2026-08-28-phase-5-backlog.md`.

## 1. Goal

Every message ley-khaa has ever processed arrived through `POST /messages` or the fixture
simulator. The intake gateway was built adapter-shaped from the start — its docstring still reads
"simulator now; Slack/Discord later" — but nothing has ever connected to a real conversation.

This phase makes the secretary reachable where the work is actually discussed, in both directions:

```
Slack / Discord ──▶ allowlist ──▶ translate ──▶ IntakeGateway ──▶ (unchanged pipeline)
       ▲                                                                    │
       └──────────────── notify: clarification, parked, done, failed ◀──────┘
```

The claim this phase has to earn, stated so a test can settle it:

> **A message posted in an allowlisted channel becomes a task in the right project; the bot asks its
> clarifying question back in that thread; a reply in the thread answers it; and none of the bot's
> own messages are ever ingested as new work.**

This is also the first phase that touches the outside world and the first that handles secrets.
Every decision below that looks conservative is conservative on purpose.

## 2. Decisions

Settled during the 2026-08-30 brainstorming session. Not open for re-litigation during execution.

1. **Both platforms, ingest *and* notify.** §5.1's "adapters only ingest and notify" is the whole
   interface, and notify is what closes §6.2 (a conversation that produced no task says so) and
   §6.4 (the clarification loop). A shared interface makes the second platform much cheaper than
   the first.
2. **In-process, outbound WebSocket.** Slack Socket Mode and Discord Gateway both dial out, so
   there is no public URL, no tunnel, and no inbound port. Adapters run as supervised asyncio tasks
   in the FastAPI lifespan beside the Phase 5 dispatcher. This is Phase 5 decision #1 applied
   again: no second process type, `docker compose up` stays one command.
3. **The channel is an inbox and a reply surface, not a control panel.** Clarifying questions are
   answered by replying in the thread. Approve, reject, and mode override stay in the dashboard,
   because approval releases work to run unattended and the channel has no notion of who is
   entitled to do that.
4. **An explicit channel allowlist.** The bot ignores any message from a channel not named in
   configuration. Being invited to a channel is not consent to ingest it. Enforced before anything
   is persisted.
5. **Notification is a `Notifier` seam injected into the driver**, not a table and not a third
   background loop. It is the same shape as `LLMClient` and `SandboxRunner`, both of which already
   have offline implementations that keep CI green.
6. **Dead-letters are persisted and surfaced**, per §7. A dropped inbound message that leaves no
   trace is the worst failure mode an intake system can have.
7. **Ollama stays out.** Unrelated work; it would widen the review surface on the riskiest phase so
   far.

## 3. Architecture

### 3.1 What does not change

The pipeline downstream of `IntakeGateway.accept()` is untouched: crystallizer, router, dispatcher,
driver, executor. An adapter's entire job is to turn a platform event into the raw dict that gateway
already accepts, and to post text back. That is the seam that makes this phase additive.

Three things this phase depends on already exist and are proven:

| Already built | Why it matters here |
|---|---|
| `MessageRow.external_id` is **unique and indexed**, and `MessageRepository.add()` dedupes on it with a race retry | Slack and Discord both redeliver on timeout. The idempotency §7 demands is already implemented and tested. |
| `ProjectRouter` binds on `client` | Setting `client` to the workspace/guild id gives per-client project routing with no new code. |
| `reply_to_task_id` and `Orchestrator._route_reply` | The clarification answer path exists; this phase only has to decide when to use it. |

### 3.2 The split that makes adapters testable

Each adapter is two pieces, and the boundary is the point:

- **`translate.py` — a pure function.** Platform event dict → the raw dict `IntakeGateway` accepts.
  No network, no tokens, no I/O. This is where the allowlist check, the self-message filter,
  thread-id derivation, and attachment mapping live. It is unit-tested against captured real
  payloads committed as fixtures.
- **`client.py` — the connection.** A thin Socket Mode / Gateway wrapper that receives events, hands
  them to `translate`, and posts outbound text. Deliberately as small as possible, because it is the
  only part CI can never exercise.

**Everything that can be wrong lives in the half that can be tested.** A defect in thread derivation
or the allowlist is a unit-test failure; the untestable half holds no decisions.

### 3.3 Components

```
backend/ley_khaa/adapters/
├── base.py          # ChannelAdapter protocol, AdapterError
├── supervisor.py    # starts adapters whose tokens exist; restarts with backoff
├── notifier.py      # Notifier protocol, ChannelNotifier, NullNotifier
├── slack/
│   ├── translate.py # pure: slack event -> raw dict
│   └── client.py    # Socket Mode wrapper
└── discord/
    ├── translate.py
    └── client.py
```

`ChannelAdapter` protocol:

```python
class ChannelAdapter(Protocol):
    name: str                                    # "slack" | "discord" | "simulator"
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def notify(self, dest: Destination, text: str) -> None: ...
```

`Destination` carries what a threaded reply needs: `conversation_id`, and the `external_id` of the
message to thread under.

**The existing `Simulator` is retrofitted to this protocol.** It already goes through
`IntakeGateway`, so the change is small — and it means the interface has three implementations from
the start rather than being shaped around Slack and bolted onto afterwards.

### 3.4 Lifecycle and supervision

`AdapterSupervisor` starts, in the FastAPI lifespan beside the dispatcher, exactly those adapters
whose tokens are present. **No tokens → no adapters → a fresh clone behaves precisely as it does
today**, which is what keeps `docker compose up` a zero-account demo.

Each adapter runs as its own supervised task. A crash is logged, dead-lettered and restarted with
exponential backoff; it never propagates into the API or the dispatcher. This is the acknowledged
cost of decision #2, so supervision is a named unit with its own tests rather than an afterthought.

### 3.5 Ingest flow

1. Event arrives on the WebSocket.
2. **Allowlist check.** Channel not listed → dropped and counted. Nothing is persisted. This is
   first, before any parsing, so an unlisted channel cannot reach storage by any path.
3. **Self-message filter.** Any message authored by this bot is discarded.
4. `translate()` produces the raw dict:

   | field | value |
   |---|---|
   | `source` | `"slack"` / `"discord"` |
   | `client` | workspace (team) id / guild id — what `ProjectRouter` binds on |
   | `conversation_id` | `slack:{team}:{channel}:{thread_ts or ts}`, `discord:{guild}:{channel}:{thread_id or message_id}` — deterministic, no mapping table |
   | `external_id` | platform message id — the dedupe key |
   | `author`, `text`, `timestamp`, `attachments` | direct |

5. `IntakeGateway.accept()` → unchanged pipeline.

**The self-message filter is load-bearing, not hygiene.** The bot posts into the channel it reads.
Without the filter, every notification is ingested as a new request and the system feeds itself
without limit. It gets a dedicated test, and that test must fail if the filter is removed.

### 3.6 Notify flow

`Notifier` is injected into `TaskDriver`. `NullNotifier` is the default, so every existing test and
every no-token run is unchanged.

Destination resolution needs **no new mapping table**: task → `source_message_ids` →
`MessageRepository.get_many()` → the originating `MessageRow` carries `source`, `conversation_id`
and `external_id`. `ChannelNotifier` routes to the adapter named by `source`.

**Notification policy** — only when a human is needed, or the work is finished:

| State | Message |
|---|---|
| `needs_clarification` | the open question |
| `awaiting_approval` | the recommended mode and its reason |
| `done` | completion and the deliverable |
| `failed` | the failure reason |

Nothing else. A bot narrating every transition is noise, and noise gets muted.

`advance()` is re-entrant, so re-notification is guarded by a `last_notified_state` column on
`TaskRow`: notify only when the state differs from what was last announced.

**Outbound work never fails a task.** Any failure is dead-lettered and logged, never raised. A wedged
Slack API must not be able to stop work from completing.

**The sync/async boundary is specified here rather than left to implementation**, because it is the
one place this design meets Phase 5's threading model head-on. `TaskDriver.advance()` is
*synchronous* and already runs inside `asyncio.to_thread` on a dispatcher worker; the Slack and
Discord clients are *async* and live on the main event loop. So `ChannelNotifier.notify()` is called
from a worker thread and must hand its coroutine to the loop with
`asyncio.run_coroutine_threadsafe(coro, loop)`, where `loop` is captured at lifespan start and held
by the supervisor.

It is **fire-and-forget**: the driver does not wait on the returned future, so a slow or wedged
platform API cannot extend a task's execution time. The future's exception is consumed by a
done-callback that writes the dead-letter. Under `LEY_KHAA_DISPATCH=inline` there is no worker
thread and the loop may not be running at all, so `ChannelNotifier` falls back to dead-lettering
the notification rather than attempting delivery — inline mode is a single-operator dashboard mode
and has no channel to answer into.

### 3.7 Clarification replies

The routing rule — *a message arriving in a conversation whose task is in `needs_clarification` is
that task's answer* — lives in the **orchestrator**, not the adapter. §5.1 says adapters hold no
business logic, and deciding what a message means is business logic.

Concretely, `Orchestrator.ingest` gains: when the arriving message names no `reply_to_task_id` but
its conversation has a task in `needs_clarification`, treat it as that task's answer and take the
existing `_route_reply` path.

This changes existing intake behaviour for the HTTP path too, so it is specified as its own task
with its own tests. A message in a conversation with **no** clarifying task is unaffected and flows
on to the crystallizer and Phase 5's amendment detector as before.

### 3.8 Dead-letters

A new `dead_letters` table: `id`, `source`, `kind` (`inbound` | `outbound` | `connection`),
`reason`, `payload` (redacted), `created_at`. Written when translation fails, when a notify fails,
and when an adapter connection errors.

Surfaced at `GET /dead-letters` and in a dashboard panel. §7 asks for "dead-letter + surface in UI",
and a dropped message with no visible trace is the failure this exists to prevent.

## 4. Safety and secrets

This is the first phase handling credentials, and the project's standing rule is that data is
synthetic and never employer-adjacent.

- **Tokens come from environment variables only.** Never committed, never logged, never written into
  a bundle, never returned by an API. Dead-letter payloads are redacted before storage.
- **The allowlist is the boundary.** Ingestion from an unlisted channel is impossible, not merely
  discouraged.
- **Startup logs exactly which channels are live**, so what the bot is listening to is always
  visible rather than inferred from config.
- **Documentation states plainly**: create a scratch workspace for this. Never point it at anything
  work-adjacent. The synthetic-data commitment does not survive being pointed at a real channel.

## 5. Configuration

| Variable | Meaning |
|---|---|
| `LEY_KHAA_SLACK_BOT_TOKEN`, `LEY_KHAA_SLACK_APP_TOKEN` | Slack; both required or the adapter does not start |
| `LEY_KHAA_SLACK_CHANNELS` | comma-separated allowlist of channel ids |
| `LEY_KHAA_DISCORD_BOT_TOKEN` | Discord |
| `LEY_KHAA_DISCORD_CHANNELS` | comma-separated allowlist of channel ids |

An adapter with a token but an **empty** allowlist starts and ingests nothing, logging that plainly.
That is the safe reading of an incomplete configuration.

## 6. Dependencies

`slack_sdk` (Socket Mode) and `discord.py` (Gateway) — the official/standard clients, both
supporting the outbound-WebSocket mode decision #2 requires. Pinned, and added to the backend image
only; the sandbox image is untouched.

## 7. Testing

- **Pure translation** against captured real payloads committed as fixtures: thread derivation,
  allowlist rejection, self-message rejection, attachment mapping, dedupe key.
- **Supervision**: a crashing adapter restarts with backoff and does not take down its neighbour or
  the API.
- **The loop, offline**: a `FakeAdapter` and `FakeNotifier` drive ingest → task → clarification →
  in-thread answer → resume, with no network and no tokens. Third instance of a seam this codebase
  has proven twice.
- **Notification policy** table-driven: exactly the four states notify; a fifth does not.
- **Re-notification** is suppressed when `advance()` re-runs.
- **Dead-letters** are written on inbound, outbound and connection failure, and are redacted.

CI needs no tokens and no network. The thin connection wrappers are the only part verified by hand,
once, against real workspaces.

Every new assertion is held to the discipline this project has enforced since Phase 4: **delete the
behaviour the assertion guards, watch the test fail for the right reason, restore it.**

## 8. Definition of done

- A message in an allowlisted Slack channel becomes a task in the correct project; the same for
  Discord.
- A message in a non-allowlisted channel is provably not persisted.
- The bot's own messages are never ingested.
- A task entering `needs_clarification` asks its question in the originating thread; a reply in that
  thread answers it and the task resumes.
- `done` and `failed` notify with the deliverable or the reason.
- Redelivery of the same platform message creates no duplicate task.
- A failed notify dead-letters, is visible in the dashboard, and does not fail the task.
- With no tokens set: no adapters start, `docker compose up` and the whole suite are unchanged and
  green with no `ANTHROPIC_API_KEY`.
- The full suite green (baseline 639 backend / 49 frontend at the start of this phase), no skips,
  no warnings, typecheck clean.

## 9. Known limits, stated up front

- **No interactive buttons.** Approve, reject and mode override remain dashboard actions
  (decision #3). A phone-only workflow is not possible in this phase.
- **No outbound delivery guarantee.** Notification is best-effort with dead-lettering, not a durable
  outbox. If delivery guarantees are ever needed, §5.1's contract is where that changes.
- **Attachments are carried, not understood.** Images ingested from a channel are stored as
  attachments; vision extraction remains §5.2 and Phase 7.
- **One workspace per platform.** Multi-workspace Slack installation (OAuth distribution) is out of
  scope; this is a single-operator tool.
- **Threads only, no DMs.** Direct messages are not ingested in this phase.
