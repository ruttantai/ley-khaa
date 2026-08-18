# ley-khaa (เลขา)

> Your AI secretary — turns the way people actually talk into finished, validated work.

**Positioning:** a personal automation layer for developers. It sits *in front of* coding agents
(Claude Code, Codex) and does the toil of reading messy conversations and prompt-engineering them
into well-structured tasks. See `docs/superpowers/specs/2026-08-18-ley-khaa-design.md`.

> ⚠️ All datasets and communications in this project are **synthetic**. It connects to no real
> employer data, credentials, or infrastructure.

## Status

**v0.1.0 — Foundation / walking skeleton.** A seeded synthetic message flows through a task state
machine and appears in the dashboard. Intelligence (crystallizer, interpreter, autonomy, executor)
arrives in later phases — see `docs/superpowers/plans/`.

## Run

```bash
docker compose up
```

- Dashboard: http://localhost:5173
- API:       http://localhost:8000  (`/health`, `/tasks`, `/tasks/{id}`, `POST /messages`)

## Develop

```bash
cd backend && pip install -e ".[dev]" && python -m pytest -v
cd frontend && npm install && npm test
```
