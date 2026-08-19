# Changelog

All notable changes to this project are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com); versioning is [SemVer](https://semver.org).

## [Unreleased]
### Changed
- README expanded for a public audience: problem framing, phase roadmap, verified local dev
  run path (SQLite + dev servers, no Docker), and an explicit note that the Compose path is
  not yet run on real hardware.

## [0.1.0] — 2026-08-18
### Added
- Foundation walking skeleton: FastAPI backend, task state machine, orchestrator (stub), Task API.
- React/Vite/Tailwind dashboard listing tasks.
- Docker Compose (Postgres + backend + frontend) with startup seed of a synthetic demo task.
- Repo hygiene: README, LICENSE, CONTRIBUTING, CHANGELOG, CI.
