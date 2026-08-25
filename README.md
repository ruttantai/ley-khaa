# ley-khaa (เลขา)

> Your AI secretary — turns the way people actually talk into finished, validated work.

**ley-khaa** is a personal automation layer for developers. It sits *in front of* coding agents
(Claude Code, Codex) and does the toil you currently do by hand: reading a messy, multi-message
conversation, working out what was actually asked, and prompt-engineering it into a well-structured,
executable task.

> ⚠️ All datasets and communications in this project are **synthetic**. It connects to no real
> employer data, credentials, or infrastructure.

## The two problems it solves

1. **Task Crystallizer** — real requests arrive scattered across a noisy thread with fuzzy start and
   end. A cheap relevance filter plus an LLM candidate-state engine assembles them into one coherent,
   validated task.
2. **Adjustable Autonomy Dial** — the system reads confidence + risk and recommends
   *Suggest* / *Co-pilot* / *Auto*, in plain English. The human always keeps the override.

Two design principles run through everything: the executor is **synthesis-first** (it writes and
sandbox-runs a Python script for the open-ended case, with a workflow registry acting as a learned
cache of proven fast paths), and every run emits a **reproducible Output Bundle** — deliverable,
generator code, exact inputs, seeded manifest — so any result can be audited and re-run.

## Status

**v0.3.0 — Interpreter, Autonomy engine, and human-in-the-loop.** A crystallized request is
interpreted into a validated `TaskSpec` — intent, inputs, operation, output format, recipient,
urgency, and what's still missing. The autonomy engine scores that spec's confidence and risk and
recommends *Suggest* / *Co-pilot* / *Auto* with a plain-English reason. A task parks at
`awaiting_approval` unless the effective mode is Auto. From there a human can approve, reject,
re-dial the mode, edit the spec inline, or answer a clarifying question — and that answer re-enters
the pipeline as a real message, the same route a Slack thread reply will take. **The executor is
still a stub**: an approved or auto-run task walks `executing → validating → done` without doing any
real work — real execution is v0.4.0. The offline `HeuristicLLM` stand-in deliberately never scores
high enough to earn Auto on its own, so a no-API-key clone can never run a task unattended.

| Phase | Tag | Scope | State |
|-------|-----|-------|-------|
| 0 | `v0.1.0` | Walking skeleton: state machine, task API, dashboard, CI | ✅ shipped |
| 1 | `v0.2.0` | Intake + **Task Crystallizer** | ✅ shipped |
| 2 | `v0.3.0` | Interpreter + **Autonomy engine** + human-in-the-loop | ✅ shipped |
| 3 | `v0.4.0` | Synthesis-first executor, validator, Output Bundle | 📋 planned |
| 4 | `v0.5.0` | Multi-channel adapters, project routing, task memory | 📋 planned |
| — | `v1.0.0` | Definition of done (spec §11) | 🎯 target |

Phases 2–4 are an indicative grouping of the spec's components, not yet broken down into written plans.

Design spec: [`docs/superpowers/specs/2026-08-18-ley-khaa-design.md`](docs/superpowers/specs/2026-08-18-ley-khaa-design.md).
Phase plans: [`docs/superpowers/plans/`](docs/superpowers/plans/).

## Run

```bash
docker compose up
```

> Upgrading from 0.2.0? This release adds Alembic. A database created by 0.2.0 has the tables but
> no `alembic_version`, so the app stamps it at the baseline automatically on first start and then
> applies the new columns. No manual drop is needed — and this is the last release that will ever
> ask. A fresh clone needs nothing.

- Dashboard: http://localhost:5173
- API: http://localhost:8000
  - `GET /health`
  - `GET /tasks`, `GET /tasks/{id}`
  - `POST /messages` — ingest one message through the full pipeline
  - `GET /candidates` — task candidates and their state
  - `GET /conversations/{id}/messages`
  - `POST /simulate/{name}` — replay a synthetic conversation fixture
  - `POST /candidates/sweep` — re-check ready candidates against the debounce gate
  - `POST /tasks/{id}/approve` — run an approved task through to completion
  - `POST /tasks/{id}/reject` — fail a parked task with a reason
  - `POST /tasks/{id}/mode` — override the autonomy mode (or clear the override)
  - `PATCH /tasks/{id}/spec` — edit the interpreted spec inline; re-scores the recommendation
  - `POST /tasks/{id}/answer` — answer a clarifying question; re-enters as a real message

Brings up Postgres + backend + frontend and seeds the demo by replaying a synthetic
conversation through the real intake pipeline. Verified from a fresh clone on Docker 29
(Colima on Apple Silicon).

### Which model actually runs

Stage A and Stage B call Claude through one seam. With no `ANTHROPIC_API_KEY` in the
environment the backend falls back to **`HeuristicLLM`** — a deterministic, regex-based
stand-in, not a model. It keeps a fresh clone and CI runnable without credentials, and it
is intentionally dumb: keyword matching, no language understanding, no reasoning about
which messages belong to the same request. It logs a warning at startup saying so. Every
result you see in the demo without a key comes from that stub.

For the real path, export a key before starting; Compose passes it through:

```bash
export ANTHROPIC_API_KEY=sk-...
docker compose up
```

To force the stand-in even with a key set, run with `LEY_KHAA_LLM=heuristic`.

### Local dev (no Docker)

The backend reads `DATABASE_URL`, so it runs on SQLite with no Postgres:

```bash
# backend
cd backend && pip install -e ".[dev]"
DATABASE_URL="sqlite:///./leykhaa.db" uvicorn ley_khaa.api.app:app --port 8000

# frontend (separate shell)
cd frontend && npm install && npm run dev
```

## Develop

```bash
cd backend  && python -m pytest -q   # 244 tests
cd frontend && npm test              # 13 tests (vitest)
```

## Stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy · Postgres (SQLite for dev/test) · custom
synchronous state-machine orchestrator (with a background sweeper for the debounce gate) ·
tiered model router (Claude Haiku ↔ Opus, with a deterministic `HeuristicLLM` stand-in when no
API key is set) · React · Vite · Tailwind · Docker Compose.

## Conventions

[SemVer](https://semver.org) tags per milestone · [Conventional Commits](https://www.conventionalcommits.org) ·
TDD throughout · `main` stays green and runnable at every tag.

## License

MIT — see [LICENSE](LICENSE).
