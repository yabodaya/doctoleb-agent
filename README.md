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

## WhatsApp webhook

- `GET /webhooks/whatsapp` — Meta's one-time subscription handshake. Answers
  `hub.challenge` as plain text when `hub.mode=subscribe` and `hub.verify_token`
  matches `META_VERIFY_TOKEN`; `403` otherwise.
- `POST /webhooks/whatsapp` — every delivery. Verifies `X-Hub-Signature-256`
  against the raw body using `META_APP_SECRET`, splits the envelope into one
  event per message and per status, stores them in `webhook_inbox`, returns
  `200`. Nothing else happens in the request.

Both secrets are empty by default, and empty means **reject**: the app starts
without a Meta app so the rest of the repo is workable, but the webhook trusts
nothing until `.env` is filled in. The startup log says which one is missing.

Status codes: `401` invalid or missing signature, `400` valid signature but the
body is not JSON, `200` stored (or already stored — a duplicate is a success),
`503` storage failed, so Meta retries and the unique `provider_event_id` makes
the retry safe.

### Testing it locally, without Meta

```powershell
$env:META_APP_SECRET = "whatever-you-put-in-.env"
uv run python scripts/sign_webhook.py "local smoke test"
```

Unsigned requests are the other half of the check — this must answer `401`:

```powershell
curl.exe -X POST http://localhost:8000/webhooks/whatsapp -H "Content-Type: application/json" -d "{}"
```

> On Windows, use `curl.exe`, not `curl`. In PowerShell `curl` is an alias for
> `Invoke-WebRequest`, which takes different flags: `-d` and `-H` are silently
> misread and you end up debugging a request you never sent.

Check what landed, ids only — `payload` holds the message text (hard rule 8):

```powershell
docker compose exec postgres psql -U doctoleb -d doctoleb -c "select provider_event_id, status, tenant_id, created_at from webhook_inbox order by created_at desc limit 5;"
```

### A public URL for Meta (Windows)

Meta must reach your machine over HTTPS, and `docker compose` publishes the API
on `127.0.0.1:8000` only. A tunnel bridges the two.

**Recommended: cloudflared.** No account, no signup, no request cap, and no
browser interstitial.

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints a `https://<random-words>.trycloudflare.com` URL. That plus
`/webhooks/whatsapp` is the Callback URL for the Meta dashboard.

**The catch, and when to prefer ngrok:** a quick tunnel's URL changes every time
you restart it, and each change means re-verifying the callback URL in the Meta
dashboard. ngrok's free tier includes one reserved domain that survives restarts
— worth the signup and the authtoken if you restart often:

```powershell
winget install --id ngrok.ngrok
ngrok config add-authtoken <token>
ngrok http --url=<your-reserved-domain>.ngrok-free.app 8000
```

**What the tunnel exposes.** Everything the app serves, to anyone who learns the
URL. Two mitigations are in place: the webhook itself is signature-verified, and
the app serves no `/docs`, `/redoc` or `/openapi.json` unless `DOCS_ENABLED=true`.

`DOCS_ENABLED` is off by default and is **not** tied to `APP_ENV` — the tunnel
runs while `APP_ENV=development`, so tying the docs to `APP_ENV` would publish
them at exactly the wrong moment. Set `DOCS_ENABLED=true` in `.env` when you want
Swagger UI locally, and do not run a tunnel while it is on.

`/health` and `/health/ready` stay reachable; they report dependency status and no
credentials. Treat the tunnel URL as private, and stop the tunnel when you are not
testing.
