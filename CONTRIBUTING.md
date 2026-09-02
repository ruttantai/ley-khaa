# Contributing

## Conventions
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org) — `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
- **Branches (v1, solo):** work on `main`, keep it green. As the project scales: short-lived `feature/<slug>` branches merged via PR.
- **Versioning:** SemVer, git tags (`0.1.0`, …, `1.0.0`).

## Tests
- Backend: `cd backend && python -m pytest -v` — runs on SQLite, which needs nothing installed.
- Backend on Postgres: `cd backend && DATABASE_URL=postgresql+psycopg://ley:ley@localhost:5432/leykhaa python -m pytest -v --database=postgres`.
  The same suite against the database `docker compose up` actually deploys; CI runs both lanes.
  The tests confine themselves to a `ley_khaa_test` schema, which they drop and recreate on every
  run, so pointing this at your own compose database will not touch its data.
- `--database=sqlite|postgres` is optional and asserts only: it fails the run if the lane you asked
  for is not the lane you got, so a lost `DATABASE_URL` cannot quietly re-run SQLite and report it as
  Postgres. Omit it and the lane is inferred from `DATABASE_URL`.
- Frontend: `cd frontend && npm test`

All data used in development is synthetic.
