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
uv run python scripts/sign_webhook.py "local smoke test"
```

It signs with the `META_APP_SECRET` already in your `.env`, read through
`app.config` - the same value the app verifies against. Do not export the secret
into your shell to do this: PowerShell's PSReadLine writes every command you type
to a plaintext history file that outlives the session.

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

## The worker, and what happens to a message

The webhook does not answer anything. It verifies, deduplicates, stores and
enqueues, and `arq` picks the job up in a separate process — that is hard rule 1,
and it is why the api stays fast when Meta is slow.

```powershell
docker compose up            # api, worker, postgres, redis
docker compose logs -f worker | Select-String "inbox event|reply generated"
```

One line per job. `event_id` is the `webhook_inbox` row id, and every outcome is
one of: `replied`, `replied_fallback`, `sent_without_id`, `stored_no_reply`,
`dropped_not_ai_active`, `already_replied`, `status_advanced`,
`status_not_moved`, `status_ignored`, `skipped`, `dead_lettered`.

### AI replies

Every text message is answered by an OpenAI model, once, from the clinic's
system prompt and the recent conversation.

**What is sent to OpenAI:** the system prompt (`app/agent/prompts.py` — review
it, it is the clinic's rules), the last `AGENT_HISTORY_MESSAGES` messages of
this conversation, and the new message. A voice note with no transcript and any
non-text message become fixed placeholders — `[patient sent a voice note]`,
`[patient sent a photo, file, location or other non-text message]` — never the
payload behind them. **Never sent:** the patient's WhatsApp profile name, their
phone number, any `wamid`, and no tenant, contact, conversation or inbox id.

**Settings** (all in `.env.example`, all optional): `OPENAI_API_KEY`,
`OPENAI_CHAT_MODEL` (no default — model names come from `.env`, never from
code), `OPENAI_TIMEOUT_SECONDS`, `OPENAI_MAX_OUTPUT_TOKENS`,
`AGENT_HISTORY_MESSAGES`, `AGENT_FALLBACK_REPLY`. With the key or the model
blank the app still boots and the worker says so at startup — every reply is
then the fallback.

**The fallback** (`AGENT_FALLBACK_REPLY`) is what the patient gets when no AI
reply can be produced: no credit, OpenAI down for all five tries, a blank
setting, a truncated or filtered completion. It goes out through the same
exactly-once path as any reply, so a retry re-sends the stored text and never
asks the model again.

**A dead letter whose `error` starts with `openai_` belongs to an event that
WAS answered** — with the fallback. Its inbox row is `PROCESSED`, not `FAILED`.
The dead letter is there so a human hears that the AI could not answer, not
because the patient was left in silence. An event can carry two dead letters
(one `openai_`, one `http_`) when the fallback itself was then refused by Meta:
two different things went wrong, and each has a different fix.

Token counts and the prompt version are on the `reply generated` line. Never
paste a prompt or a reply into an issue.

### Tools (VS-006)

The model can now **look things up** before it answers. It never runs anything
itself: it asks for a tool by name, our code validates the arguments and decides
what actually runs, against which clinic.

**The three tools**, all read-only:

| Tool | What it returns |
|---|---|
| `get_clinic_information` | name, address, opening hours (closed days marked), policies |
| `list_doctors` | each doctor's `doctor_id`, name, specialty and services |
| `search_available_slots` | one doctor's free times between two clinic-local datetimes |

The model must call `list_doctors` first: `search_available_slots` takes a
`doctor_id` and does no name lookup, so it cannot invent a doctor.

> **The booking data is FAKE.** VS-006 ships an in-memory demo clinic -
> "Doctoleb Demo Clinic", 1 Demo Street, Dr. Karim Haddad - and the worker says
> so on every start (`booking service is the in-memory FAKE`). **Never put this
> worker in front of real patients.** VS-011 connects the real Booking Service.

**The clock message.** The date is deliberately NOT in the system prompt (the
prompt's SHA-256 is pinned to its version, and a prompt that changed daily could
not be pinned). Instead each turn carries a separate `system` message with the
clinic's current date and time in **Asia/Beirut**, tomorrow's date, and the next
seven dates with weekday names. `tzdata` is a runtime dependency because Windows
hosts have no system time zone database.

**The limits.** One turn makes at most **4 model calls** and runs under one
deadline, `AGENT_TURN_TIMEOUT_SECONDS` (default 45). Four is three for the
normal flow - list, search, answer - plus one for a single self-correction after
an invalid-arguments error. `JOB_TIMEOUT_SECONDS` rose **60 → 90** to cover the
turn plus the Meta send (45 + 10 = 55), which puts the claim lease at 120s.

**New dead-letter reasons**, all `replied_fallback` (the patient WAS answered):

| Reason | What it means |
|---|---|
| `agent_max_model_calls` | the model still wanted tools on its 4th call. Permanent — a retry would loop the same way. |
| `agent_turn_timeout` | the whole turn passed `AGENT_TURN_TIMEOUT_SECONDS`. Retried with backoff; falls back on the last try. |
| `agent_tool_crashed` | a bug in our tool code. The dead letter carries the exception class name only. |

### Reading the tables

**Take the database credentials from `docker-compose.yml` and `.env`** rather than
copying them from here — compose defaults `POSTGRES_USER` and `POSTGRES_DB`, and a
changed `.env` makes every command below fail with an authentication error that
looks like something else:

```powershell
Select-String -Path docker-compose.yml, .env -Pattern "POSTGRES_USER|POSTGRES_DB"
```

Then, substituting those values:

```powershell
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select id, status, attempts, locked_until is null as free, tenant_id is not null as has_tenant, created_at from webhook_inbox order by created_at desc limit 10;"
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select direction, modality, status, provider_message_id is not null as has_wamid, reply_to_message_id is not null as is_reply from messages order by created_at desc limit 10;"
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select job_name, source_event_id, error, attempts, created_at from dead_letter_jobs order by created_at desc limit 10;"
```

**What each turn cost, and which tools it called** (VS-006). Both tables hold
codes, counts and ids only — by design, so they are safe to open casually:

```powershell
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select created_at, outcome, reason, model_calls, prompt_tokens, completion_tokens, duration_ms from agent_runs order by created_at desc limit 12;"
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select r.created_at, t.sequence, t.model_call, t.tool_name, t.argument_names, t.status, t.error_code, t.duration_ms from tool_executions t join agent_runs r on r.id = t.agent_run_id order by r.created_at desc, t.sequence limit 30;"
```

A `tool_name` of `unknown` means the model named a tool that does not exist: the
name it wrote is never stored, because it is model-written text.
`argument_names` holds the names of arguments **we declared** that were present —
never their values, and never a key the model invented.

**Select ids, statuses and reason codes. Never `payload`, never `text`, never
`provider_event_id`, and never paste a prompt or a generated reply into an
issue** (hard rule 8). A terminal transcript is as public as a log:
`payload` and `text` hold the patient's words, and a `provider_event_id` is a
`wamid`, which is base64 and commonly decodes to include their phone number.

`free` in the first query is the claim lease. `false` on a row that nothing is
working on means a worker died holding it; it becomes claimable again by itself
once the lease expires (`JOB_TIMEOUT_SECONDS + JOB_LEASE_MARGIN_SECONDS`).

### When a reply does not arrive

| What you see | What it means |
|---|---|
| no `inbox event` line at all | the job was never enqueued, or the worker is not running. Check the api log for `enqueue failed`. |
| `dead_lettered` with `unknown_phone_number` | the number is not in `WHATSAPP_TENANT_MAP`, and there is no default tenant (hard rule 4). |
| `dead_lettered` with `http_401` | `META_ACCESS_TOKEN` is wrong or expired. Permanent, so it dead-letters on the first try rather than after five. |
| `dead_lettered` with `http_400 code_131030` | the recipient is not on the test number's allowed list. |
| `retrying … reason=http_500` | Meta's problem. Deferrals are 5s, 10s, 20s, 40s, then a dead letter. |
| `stored_no_reply` | the message was stored but its type is not in `WHATSAPP_REPLY_TO_TYPES` (default: `text` only). |
| `dropped_not_ai_active` | a human holds the conversation, or it is closed (hard rule 7). |
| `status_before_wamid` in a dead letter | a delivery status for a message this tenant never sent — often a message sent by hand from the Meta Business app. |
| `replied_fallback` + `openai_insufficient_quota` | the OpenAI account has no credit. Permanent: every reply falls back until it is topped up. |
| `replied_fallback` + `openai_model_unset` / `openai_api_key_unset` | the setting is blank in `.env`. The worker's startup log says so too. |
| `replied_fallback` + `openai_http_404_…` | `OPENAI_CHAT_MODEL` names a model this key cannot use. |
| `replied_fallback` + `openai_http_401_…` | `OPENAI_API_KEY` is wrong or revoked. |
| `replied_fallback` + `openai_reply_truncated` / `openai_empty_reply` | `OPENAI_MAX_OUTPUT_TOKENS` is too small for the model — likely a reasoning model spending the budget on hidden reasoning. |
| `retrying … reason=openai_http_429` or `openai_timeout` | OpenAI is rate-limiting or slow. Retried with backoff; the fifth failure falls back. |
| `retrying … reason=http_timeout` | the Meta send passed `META_SEND_TIMEOUT_SECONDS`. Meta may still have accepted it. |
| `dropped_not_ai_active` with no `reply generated` line | a human held the conversation before the job started: the model was not called. |
| `replied_fallback` + `agent_max_model_calls` | the model kept asking for tools. Look at that run's `tool_executions`: a repeated `INVALID_ARGUMENTS` usually means a tool description needs work. |
| `replied_fallback` + `agent_turn_timeout` | the whole turn was too slow. `agent_runs.duration_ms` and `model_calls` say where it went. |
| `replied_fallback` + `agent_tool_crashed` | a bug in our tool code. The dead letter names the exception class; the `tool_executions` row names the tool. |
| a reply naming times the clinic does not have | the booking data is the **fake** (see the warning above), not a real schedule. |
