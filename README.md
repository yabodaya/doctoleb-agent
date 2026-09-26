# Doctoleb WhatsApp Agent

WhatsApp messaging + AI layer of Doctoleb, an AI receptionist for clinics.

- Instructions for Claude Code and hard rules: `CLAUDE.md`
- Architecture: `docs/architecture.md`
- Booking Service contract (draft): `docs/booking-contract.md`
- Build plan: `docs/slices/README.md`

## Getting started
1. `cp .env.example .env` and fill in values
2. Work through `docs/slices/` in order, starting with VS-001

## Run it locally
```bash
cp .env.example .env     # fill in values; the Postgres/Redis defaults work as-is
docker compose up --build
```
- API: http://localhost:8000
- Liveness: `GET /health` -> `200 {"status":"ok"}`
- Readiness: `GET /health/ready` -> `200` when Postgres and Redis answer, `503` otherwise

Tests and lint:
```bash
docker compose exec api pytest
docker compose exec api ruff check .
```

Working outside Docker needs [uv](https://docs.astral.sh/uv/): `uv sync`, then `uv run pytest`.

## Database and migrations

```bash
docker compose exec api alembic upgrade head          # apply
docker compose exec api alembic downgrade -1          # undo the last revision
docker compose exec api alembic revision --autogenerate -m "what changed"
```

Alembic reads the DSN from `DATABASE_URL` via `app.config`, not from `alembic.ini`
— `sqlalchemy.url` there is deliberately empty so no credential is committed.
`./migrations` and `./alembic.ini` are bind-mounted into the api container, which
is what makes `--autogenerate` write the new revision onto the host.

Autogenerate does **not** detect CHECK constraint changes. Widening one of the
`StrEnum`s in `app/db/enums.py` needs a hand-written migration plus a new case in
`tests/db/test_constraints.py`.

## Running the tests

Tests that need a real PostgreSQL are marked `db`. They use a separate database,
`doctoleb_test`, created on first run, and each test runs in a transaction that is
rolled back.

```bash
docker compose exec api pytest        # everything (the full run)
docker compose up -d postgres && uv run pytest   # everything, from the host
uv run pytest                         # nothing running: db tests skip, the rest pass
uv run pytest -m "not db"             # skip them explicitly
```

Set `TEST_DATABASE_URL` to point somewhere else; otherwise it is derived from
`DATABASE_URL`.
