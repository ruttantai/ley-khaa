# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com); versioning is [SemVer](https://semver.org).

## [Unreleased]

## [0.2.0] — 2026-08-19

> **Upgrading from 0.1.0:** this release adds columns to the `messages` table and the project has
> no migration tooling yet. Drop your existing database first — `docker compose down -v` for the
> Postgres volume, or delete your local `leykhaa.db` for the SQLite dev path. A fresh clone is
> unaffected.
### Added
- Intake gateway: canonical multi-modal `Message` (text/table/image attachments), idempotent per external id.
- Task Crystallizer stage A — cheap per-message relevance and topic filter.
- Task Crystallizer stage B — stateful LLM candidate engine: candidates own only their own message ids, interleaved topics become separate candidates, readiness and missing-field tracking.
- Readiness gate debouncing emission until the conversation settles.
- Model Router (`model_for(stage, complexity)`) with a testable policy table and a thinking-capability flag.
- `HeuristicLLM` offline fallback so a fresh clone demos with no API key.
- Conversation simulator plus a golden messy synthetic conversation fixture.
- API: `GET /candidates`, `GET /conversations/{id}/messages`, `POST /simulate/{name}`; `POST /messages` now returns an intake ack.
- Dashboard panel showing candidates forming, with state and owned-message counts.
- `POST /candidates/sweep` re-evaluates ready candidates against the readiness gate, so a debounce can actually elapse (the gate is evaluated at message arrival, when no time has passed yet).
- Background sweeper on the FastAPI lifespan (`LEY_KHAA_SWEEP_SECONDS`, default 15) so the debounce gate has a trigger in live use, not only in the backdated demo.
- Stage A verdicts (`relevant`, `topic`, `confidence`) are persisted on the message and used to prune known chatter out of the stage B window.
- `anthropic` SDK dependency.

### Changed
- The orchestrator no longer turns every message into a task — only a `ready` candidate is promoted.
- Promotion claims a candidate with a conditional update before creating its task, so concurrent sweeps cannot double-create a task.
- Model message ids are validated against the conversation before they can reach a Task.
- README expanded for a public audience: problem framing, phase roadmap, both run paths
  (`docker compose up`, plus a no-Docker SQLite dev path), the full endpoint list, and an
  honest description of the offline `HeuristicLLM` stand-in.

### Fixed
- The offline stand-in keyed every candidate the same way, so a conversation could only ever produce one task; the key is now derived from the messages a candidate owns.
- `POST /messages` with empty or whitespace-only text returns 422 instead of 500.

### Verified
- `docker compose up` confirmed working from a fresh clone on Docker 29 / Colima (Apple Silicon):
  Postgres healthy, backend seeds the demo by replaying a synthetic conversation, frontend serves
  on :5173. This was the last unverified part of 0.1.0.

## [0.1.0] — 2026-08-18
### Added
- Foundation walking skeleton: FastAPI backend, task state machine, orchestrator (stub), Task API.
- React/Vite/Tailwind dashboard listing tasks.
- Docker Compose (Postgres + backend + frontend) with startup seed of a synthetic demo task.
- Repo hygiene: README, LICENSE, CONTRIBUTING, CHANGELOG, CI.
