# Doctoleb WhatsApp Agent: Claude Code Instructions

## What this repo is
The WhatsApp messaging + AI layer of Doctoleb, an AI receptionist for clinics.
It receives WhatsApp messages (text, later voice notes), runs them through the Agent Core
(OpenAI + controlled tools), and replies through the Meta WhatsApp Cloud API.

This repo does NOT own clinic, doctor, schedule or appointment data. Those belong to the
Booking Service (built separately). We talk to it ONLY through the client in
`app/integrations/booking/`, following `docs/booking-contract.md`.

Read `docs/architecture.md` before any non-trivial change.

## Stack
- Python 3.12, FastAPI, Pydantic v2
- PostgreSQL + SQLAlchemy 2.x (async) + Alembic migrations
- Redis + arq for background jobs (queue is behind an interface so it can move to SQS later)
- httpx for outbound HTTP (Meta, Booking Service)
- OpenAI Python SDK (model names come from env vars, never hardcoded)
- pytest + pytest-asyncio, ruff
- Docker Compose for local dev (api, worker, postgres, redis)

## Hard rules. Never violate these, even if asked mid-task.
1. The webhook endpoint only does: verify signature -> dedupe -> store raw event -> enqueue -> return 200.
   No OpenAI calls, no Meta sends, no slow work inside the webhook request.
2. Every Meta event is deduplicated by its message ID. The same event delivered twice must
   produce exactly one stored message and one reply.
3. The LLM never gets database access or raw HTTP access. It can only call the tools defined in
   `app/agent/tools/`, and every tool validates its arguments with Pydantic.
4. tenant_id is resolved by our backend from the receiving WhatsApp phone_number_id. It is never
   taken from LLM output and never exposed as a tool argument.
5. The agent may only tell a patient an appointment is booked, changed or cancelled after the
   Booking Service returned success. On failure, it says so honestly and offers alternatives or handoff.
6. Every booking-changing call sends an idempotency key derived from the source message ID.
7. Before sending ANY AI reply, the worker re-reads the conversation state. If it is HUMAN_ACTIVE
   or CLOSED, the reply is dropped (and logged by ID only).
8. Patient content (message text, transcripts, audio, names, phone numbers) never goes into logs,
   exceptions sent to error trackers, or test fixtures from real data. Log IDs, not content.
9. Secrets only come from environment variables (`.env`, never committed). `.env.example` lists them.
10. The agent handles administration only: info, scheduling, handoff. Medical questions, symptoms
    or urgent-sounding messages trigger `request_human_handoff()` (plus an emergency notice when urgent).
11. External calls (Meta, OpenAI, Booking Service) have timeouts and bounded retries with backoff.
    Jobs that keep failing go to a dead-letter table rather than retrying forever.

## Layout
```
app/
  main.py                 # FastAPI app factory
  config.py               # settings from env (pydantic-settings)
  api/                    # routes: health, whatsapp webhook
  channels/whatsapp/      # Meta client, payload models, signature check, media download
  agent/                  # Agent Core: process_turn(), prompts, tools/
  integrations/booking/   # BookingClient protocol + FakeBookingClient + HttpBookingClient
  integrations/openai/    # thin wrappers: chat/tool calling, transcription, TTS
  worker/                 # arq worker + job functions
  queue/                  # enqueue interface
  db/                     # models, session, repositories
migrations/               # Alembic
tests/
docs/
```

## How to work in this repo
- Work one slice at a time. The current slice is in `docs/slices/` (Status: IN PROGRESS).
- Start every slice in plan mode: read CLAUDE.md, the slice file, and the relevant docs, then
  propose a plan and WAIT for approval before writing code.
- Stay inside the slice's scope. If something out of scope seems needed, list it under
  "Follow-ups" in the slice file instead of doing it.
- A slice is done only when: acceptance criteria pass, `pytest` passes, `ruff check` is clean,
  and the slice file's Status and Notes are updated.
- After finishing, explain what was built function by function: what each does, why it exists,
  and which hard rule it protects. The developer is learning; clarity matters more than brevity here.
- If requirements are ambiguous, ask. Do not guess.

## Commands
- `docker compose up --build`: run everything
- `docker compose exec api alembic upgrade head`: apply migrations
- `docker compose exec api pytest`: tests
- `ruff check . && ruff format .`
