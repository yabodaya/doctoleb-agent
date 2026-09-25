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
