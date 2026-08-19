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

**v0.1.0 — Foundation / walking skeleton.** A seeded synthetic message flows through the task state
machine and appears in the dashboard. Verified end to end on the local dev path (see below).
Intelligence — crystallizer, interpreter, autonomy engine, executor — arrives in later phases.

| Phase | Tag | Scope | State |
|-------|-----|-------|-------|
| 0 | `v0.1.0` | Walking skeleton: state machine, task API, dashboard, CI | ✅ shipped |
| 1 | `v0.2.0` | Intake + **Task Crystallizer** | ⏳ next |
| 2 | `v0.3.0` | Interpreter + **Autonomy engine** + human-in-the-loop | 📋 planned |
| 3 | `v0.4.0` | Synthesis-first executor, validator, Output Bundle | 📋 planned |
| 4 | `v0.5.0` | Multi-channel adapters, project routing, task memory | 📋 planned |
| — | `v1.0.0` | Definition of done (spec §11) | 🎯 target |

Phase 1 is planned; phases 2–4 are an indicative grouping of the spec's components, not yet
broken down into written plans.

Design spec: [`docs/superpowers/specs/2026-08-18-ley-khaa-design.md`](docs/superpowers/specs/2026-08-18-ley-khaa-design.md).
Phase plans: [`docs/superpowers/plans/`](docs/superpowers/plans/).

## Run

### Local dev (no Docker required) — *verified*

```bash
# backend — SQLite, no Postgres needed
cd backend && pip install -e ".[dev]"
DATABASE_URL="sqlite:///./leykhaa.db" uvicorn ley_khaa.api.app:app --port 8000

# frontend (separate shell)
cd frontend && npm install && npm run dev
```

- Dashboard: http://localhost:5173
- API: http://localhost:8000 — `/health`, `/tasks`, `/tasks/{id}`, `POST /messages`

### One-command (Docker + Postgres) — *not yet run on real hardware*

```bash
docker compose up
```

The Compose stack (Postgres + backend + frontend) is written and builds in CI, but has not yet been
executed on a machine with Docker installed. Use the local dev path above until that is confirmed.

## Develop

```bash
cd backend  && python -m pytest -v   # 19 tests
cd frontend && npm test              # vitest
```

## Stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy · Postgres (SQLite for dev/test) · custom async
state-machine orchestrator · tiered model router (Claude Haiku ↔ Opus, Ollama offline fallback) ·
React · Vite · Tailwind · Docker Compose.

## Conventions

[SemVer](https://semver.org) tags per milestone · [Conventional Commits](https://www.conventionalcommits.org) ·
TDD throughout · `main` stays green and runnable at every tag.

## License

MIT — see [LICENSE](LICENSE).
