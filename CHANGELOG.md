# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com); versioning is [SemVer](https://semver.org).

## [Unreleased]
### Changed
- README expanded for a public audience: problem framing, phase roadmap, and both run paths
  (`docker compose up`, plus a no-Docker SQLite dev path).

### Verified
- `docker compose up` confirmed working from a fresh clone on Docker 29 / Colima (Apple Silicon):
  Postgres healthy, backend seeds the demo task, frontend serves on :5173. This was the last
  unverified part of 0.1.0.

## [0.2.0] — 2026-08-19
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

### Changed
- The orchestrator no longer turns every message into a task — only a `ready` candidate is promoted.

## [0.1.0] — 2026-08-18
### Added
- Foundation walking skeleton: FastAPI backend, task state machine, orchestrator (stub), Task API.
- React/Vite/Tailwind dashboard listing tasks.
- Docker Compose (Postgres + backend + frontend) with startup seed of a synthetic demo task.
- Repo hygiene: README, LICENSE, CONTRIBUTING, CHANGELOG, CI.
