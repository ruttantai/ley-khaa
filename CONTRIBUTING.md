# Contributing

## Conventions
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org) — `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
- **Branches (v1, solo):** work on `main`, keep it green. As the project scales: short-lived `feature/<slug>` branches merged via PR.
- **Versioning:** SemVer, git tags (`0.1.0`, …, `1.0.0`).

## Tests
- Backend: `cd backend && python -m pytest -v`
- Frontend: `cd frontend && npm test`

All data used in development is synthetic.
