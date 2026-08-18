# ley-khaa (เลขา) — Design Spec

**Date:** 2026-08-18
**Status:** Approved design — ready for implementation planning
**Tagline:** Your AI secretary — turns the way people actually talk into finished, validated work.

---

## 1. Problem

Real requests do not arrive as clean, single, structured instructions. They arrive as
**conversations** — spread across multiple messages, interleaved with noise, tangents, and
false starts, with a fuzzy beginning and an "okay, this is actually a request now" endpoint.
Today a human has to read that mess, extract the real task, decide whether it's even a task,
gather the inputs, do the work, check it, and produce a business-ready output.

**ley-khaa** automates that pipeline **without requiring the human to hand-extract the task**,
while keeping the human exactly as in control as they want to be.

### The two headline problems this solves

1. **Task Crystallization** — extracting a coherent, actionable task from a messy, multi-message,
   noisy conversation stream (with a start and an end), so the user never manually extracts it.
2. **Adjustable Autonomy** — the system assesses its own confidence and the risk of the task and
   *recommends* how much it should do on its own (Suggest / Co-pilot / Auto). Humans can always
   override. Most "AI agent" demos are all-or-nothing; this is deliberately not.

Everything else in the system exists to serve these two.

---

## 2. Scope

### In scope for v1 (deliberately ambitious — chosen by the product owner)

- Multi-channel intake: **real Slack + Discord adapters** (activate with tokens) **plus a built-in
  local message simulator** for instant fresh-clone demos.
- **Task Crystallizer** (the focus feature): hybrid design — cheap per-message relevance/topic
  filter, then an LLM pass that maintains stateful task candidates and readiness detection.
- Multi-client **project routing** with **concurrent, per-project task queues**.
- **Interpreter**: NL → validated `TaskSpec`.
- **Workflow registry** with **2 workflow types**: universe set-difference and summary-stats/aggregation.
- **Autonomy engine**: confidence + risk → recommended mode, with a plain-English reason.
- **Human-in-the-loop** layer: approve / edit-spec / answer-clarification.
- **Orchestrator**: per-task state machine, concurrency, **amendment detection** for ad-hoc
  requests that arrive mid-flight.
- **Executor + Validator** with a clarification loop.
- **Document generation**: Excel (primary), Word (optional).
- **Task memory**: learns recurring workflow definitions ("the usual universe check").
- **Web dashboard** (React) running on localhost.
- **One-command run** (`docker compose up`) with seeded synthetic data and auto-fired demo requests.
- All data and communications are **synthetic**. No real employer data, credentials, or infrastructure.

### Roadmap (documented in-repo, NOT built in v1)

Microsoft Teams + email adapters · real cloud deployment · **Tauri desktop packaging** for
non-technical users (no API key path via local model) · richer memory/learning · auth / RBAC ·
observability dashboards · additional workflow types.

### Non-goals

- Not a hosted SaaS in v1 (local-first; users run it themselves).
- No connection to any real employer Slack/data/credentials/infra. Ever.

---

## 3. Audience & distribution

- **v1:** technical users who clone from GitHub, read the README, and run `docker compose up`.
  Priority is **local-first, self-hostable**.
- **Long-term:** packaged **downloadable desktop software** (Tauri/Electron) so non-technical
  people can install it. Local-first now makes that path natural; SQLAlchemy keeps a SQLite swap
  open for the desktop build.

---

## 4. Architecture

```
Channels ──► Intake Gateway ──► Task Crystallizer ──► Project Router ──► Interpreter ──► Autonomy Engine
(Slack/Discord/  (normalize to    (conversation →      (which project?)   (NL→TaskSpec)   (confidence+risk
 local simulator) canonical        task candidate;                                        → recommended mode)
                  Message)         readiness)                                                    │
                                        │                                                        ▼
                                   not a task ──► notify user                            HITL (approve/edit/clarify)
                                                                                                 │
   Task Memory ◄──── learns ──── Document Gen ◄── Validator ◄── Executor ◄─────────────────────┘
   ("the usual")                 (Excel/Word)     (clarify loop) (runs workflow on synthetic data)
```

The **Orchestrator** wraps this as a **per-task state machine** so many tasks run concurrently,
persist across pause/resume, and can be amended mid-flight.

Every box is a small, independently testable unit behind a clear interface: you can state what it
does, how to use it, and what it depends on, and test it in isolation.

---

## 5. Components

### 5.1 Channel adapters
Common `Adapter` interface. Real **Slack** and **Discord** bot adapters (activate when tokens are
supplied). A built-in **message simulator** replays synthetic conversations against the same
interface so a fresh clone demos with zero external accounts. Adapters only *ingest* and *notify*;
they hold no business logic.

### 5.2 Intake gateway
Normalizes any inbound message to a canonical `Message` (source, client, conversation/thread id,
author, text, timestamp, message id). **Idempotent per message id** so retries never duplicate.

### 5.3 Task Crystallizer  *(headline #1)*
Replaces the naive single-message classifier. Hybrid, two-stage, each stage separately testable:

- **Stage A — cheap relevance/topic filter:** a lightweight per-message pass tags each message as
  noise vs. task-relevant and assigns a coarse topic. Prunes obvious chatter cheaply.
- **Stage B — LLM crystallizer:** over the relevant rolling window per conversation, maintains
  **task candidates** as stateful objects:
  - lifecycle: `forming → crystallizing → ready`
  - each candidate owns only the **message ids that belong to it** — unrelated messages are
    assigned to no candidate, so **noise is excluded by construction**; multiple concurrent topics
    in one channel become multiple candidates (handles interleaving).
  - **boundary detection:** *start* = enough signal that a task is forming; *end/readiness* =
    required `TaskSpec` fields are present and no open questions remain.
  - **debounce:** waits for a conversational pause or a readiness threshold instead of firing
    mid-thought.
  - if a candidate stalls with missing fields, it can raise a **proactive clarifying question**
    (via HITL) rather than guess.

Only a `ready` candidate, with its owned messages, flows onward.

### 5.4 Project router
Assigns each ready candidate to the correct **project/workspace**; multi-client aware. Each project
has its **own task queue**. New requests targeting an **active** task in the same project are routed
to the amendment detector (see 5.9).

### 5.5 Interpreter
LLM → validated **`TaskSpec`** (Pydantic v2):
`intent · inputs · operation · output_format · recipient · urgency · missing_fields · source_message_ids`.
Malformed output → re-prompt with schema, then escalate to HITL.

### 5.6 Workflow registry
Each workflow type declares required inputs + output schema so the planner can select and bind
correctly. v1 ships two distinct types:

- **Universe set-difference** — compare list A vs list B; report missing / extra / duplicates
  (normalize identifiers first).
- **Summary-stats / aggregation** — group a dataset and produce summary statistics.

### 5.7 Autonomy engine  *(headline #2)*
Scores **confidence** (interpreter certainty, missing fields, crystallizer readiness) and **risk**
(irreversibility, urgency, money-touching) → recommends **Suggest / Co-pilot / Auto** with a
plain-English reason (e.g. "94% sure, low risk → I suggest Auto"; "ambiguous and touches money →
stay in Suggest"). Human can always override the recommended mode.

| Mode | System does | Human does |
|------|-------------|------------|
| Suggest | Proposes the interpretation only | Approves every step |
| Co-pilot | Executes with checkpoints | Approves at key gates |
| Auto | Runs end-to-end | Reviews final output |

### 5.8 Human-in-the-loop layer
Surfaces in the dashboard: **approve**, **edit the spec**, or **answer a clarification**. The mode
from the autonomy engine determines how many gates the human sees.

### 5.9 Orchestrator (state machine)
Per-task lifecycle:
`received → classified → interpreted → awaiting-approval → executing → validating →
(needs-clarification) → done | failed`.
Concurrent across tasks and projects; state persisted in Postgres so tasks survive pause/resume.
**Amendment detector:** when a new request references an active task in the same project (e.g.
"actually also flag duplicates"), it proposes *"fold into the running task, or treat as separate?"*
instead of spawning a duplicate. If a new request is marked urgent, the autonomy engine may propose
reprioritizing the queue.

### 5.10 Executor + Validator
Executor runs the selected workflow on synthetic data. Validator checks the results (shape, sanity,
required-field coverage); on failure it bounces a **clarification** back to HITL rather than emitting
a bad output.

### 5.11 Document generator
**Excel** via `openpyxl` (primary); **Word** via `python-docx` (optional). Output is downloadable
from the dashboard.

### 5.12 Task memory
Stores recurring workflow definitions keyed per project/client. Recognizes phrasings like "do the
usual universe check" and pre-fills the `TaskSpec`, subject to the same HITL/autonomy gates.

### 5.13 Web dashboard (React)
Localhost UI: incoming requests **grouped by project**, live task states, the interpreted spec +
the autonomy recommendation and reason, approve / edit / clarify controls, and output download.
This is what makes it feel like software and seeds the future desktop app.

---

## 6. Key data flows

### 6.1 Happy path
Conversation messages → intake → crystallizer emits a `ready` candidate → router files it to a
project → interpreter produces `TaskSpec` → autonomy engine recommends a mode → HITL gate(s) per
mode → executor runs workflow → validator passes → document generated → delivered/downloadable →
task memory updated.

### 6.2 Not a task
Crystallizer never produces a `ready` candidate (or classifies the thread as non-task) → user gets
a notify-back, no task created.

### 6.3 Ad-hoc request mid-flight
New message arrives while a task runs → intake → crystallizer → router. If it targets an **active**
task in the same project → amendment detector asks fold-in vs. separate. If urgent → autonomy engine
may propose reprioritizing. Otherwise → its own concurrent task instance. All task state persisted.

### 6.4 Clarification loop
Missing fields at crystallization *or* validation failure → proactive clarifying question via HITL →
answer merges back into the candidate/spec → flow resumes.

---

## 7. Error handling

- LLM call failure → retry, then **fall back to local Ollama model**.
- Malformed `TaskSpec` → re-prompt with schema, then escalate to HITL.
- Validation failure → clarification loop (never emit a bad document).
- Adapter error → dead-letter + surface in UI.
- Every message **idempotent by id**; retries never duplicate tasks.

---

## 8. Technology choices

| Concern | Choice | Rationale |
|---|---|---|
| Backend | Python · FastAPI · Pydantic v2 | Fast to build; Pydantic gives validated specs for free |
| ORM / DB | SQLAlchemy · **Postgres** (container) | Real/marketable; SQLAlchemy keeps SQLite-for-desktop open |
| Orchestration | **Custom async state machine** | More transparent/demoable than LangGraph; stronger portfolio signal |
| LLM | **Claude API** (Haiku for dev) + **Ollama** local fallback | Quality + cheap dev; local path enables keyless downloadable app |
| Frontend | React · Vite · Tailwind | Standard, brandable, wraps into Tauri later |
| Documents | openpyxl (Excel), python-docx (Word) | Business-ready outputs |
| Synthetic data | Faker-generated securities datasets | Deterministic, seedable, no real data |
| Run | Docker Compose | One-command boot for the clone-and-run audience |

---

## 9. Testing (TDD)

- **Unit** tests per unit: relevance filter, crystallizer candidate/readiness logic (golden
  conversation fixtures → expected candidates), interpreter (golden messages → expected specs),
  each workflow (deterministic on seeded data), autonomy scoring (table-driven), amendment detector.
- **Integration**: one test drives a full conversation through the simulator end-to-end
  (messy input with noise → correct task → correct Excel output).
- Synthetic datasets are **seeded and deterministic** so tests and demos are reproducible.

---

## 10. Repository layout

```
ley-khaa/
├── backend/          # FastAPI app, orchestrator, engines
├── frontend/         # React + Vite + Tailwind dashboard
├── adapters/         # Slack, Discord, simulator (common Adapter interface)
├── workflows/        # set-difference, summary-stats, registry
├── synthetic-data/   # Faker generators + seed fixtures
├── docs/             # architecture, roadmap, this spec
├── docker-compose.yml
└── README.md         # "your AI secretary…" story + one-command run
```

`docker compose up` boots Postgres + backend + frontend, seeds synthetic data, and the simulator
fires a few example conversations so the dashboard is alive on first load.

---

## 11. Definition of done (v1)

- Fresh clone → `docker compose up` → dashboard live with seeded demo conversations.
- A messy, multi-message, noisy synthetic conversation is crystallized into the correct task with
  no manual extraction.
- The autonomy engine recommends a mode with a readable reason; human can override.
- Requests from multiple clients route into the correct projects with concurrent queues.
- An ad-hoc mid-flight request triggers the amendment detector.
- At least the two workflow types run and produce a downloadable Excel output.
- Task memory recognizes a repeated request and pre-fills its spec.
- Real Slack + Discord adapters function when tokens are supplied; simulator works with none.
- Tests (unit + one end-to-end integration) pass.
- README tells the "AI secretary" story and documents synthetic-only data.
