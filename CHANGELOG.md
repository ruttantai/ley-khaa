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

## [0.1.0] — 2026-08-18
### Added
- Foundation walking skeleton: FastAPI backend, task state machine, orchestrator (stub), Task API.
- React/Vite/Tailwind dashboard listing tasks.
- Docker Compose (Postgres + backend + frontend) with startup seed of a synthetic demo task.
- Repo hygiene: README, LICENSE, CONTRIBUTING, CHANGELOG, CI.
