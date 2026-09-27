# VS-003 Receive WhatsApp Webhook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, checkpointing with the developer after every task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A real WhatsApp message from a phone is signature-verified, split into its individual events, deduplicated and stored in `webhook_inbox` exactly once — and nothing else happens inside the request.

**Architecture:** Two endpoints on one path. `GET /webhooks/whatsapp` answers Meta's subscription handshake. `POST /webhooks/whatsapp` reads the **raw** request body, verifies `X-Hub-Signature-256` against it, parses the JSON itself, splits the envelope into one inbox item per message and per status callback, and stores them all in one transaction through VS-002's `WebhookInboxRepository.store_if_new`. The database's unique `provider_event_id` is the only dedupe authority. Nothing in this slice resolves a tenant, calls Meta, calls OpenAI, or enqueues — VS-004 adds the enqueue call at one marked seam.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.1.1 (async) + asyncpg, PostgreSQL 16, pytest + pytest-asyncio, ruff, Docker Compose, cloudflared (or ngrok) for the local public URL.

**Spec:** `docs/slices/VS-003.md` (scope and acceptance), with `CLAUDE.md` (hard rules) and `docs/architecture.md` (flow and ownership) as binding context. `docs/slices/VS-001.md` and `docs/slices/VS-002.md` Notes carry the environment and schema facts this slice builds on; their Follow-ups are triaged below.

**Sequencing constraint:** Tasks 1–6 are fully testable with fake secrets and synthetic payloads, today, with no Meta account. **Task 7 is BLOCKED** until the developer's Meta developer app exists (waiting on Meta's new-account restriction). The slice's first acceptance criterion ("Meta dashboard webhook verification succeeds") lives there, so VS-003 reaches *code complete* at Task 6 and *DONE* only after Task 7.

---

## Understand first

**What a webhook is.** Our API normally answers questions someone asks it. A webhook is the reverse: we hand Meta a URL, and Meta makes an HTTP request to *us* whenever something happens — a patient sent a message, a message we sent was delivered. There is no polling and no connection we keep open. The consequence that shapes this whole slice: the caller is a stranger on the public internet. Anyone who learns the URL can POST to it. The only thing that makes a request trustworthy is the signature, so verifying it is the first statement in the handler, before anything else looks at the body.

**Why the signature uses the RAW body.** Meta computes `HMAC-SHA256(app_secret, exact_bytes_of_the_body)` and sends the result in `X-Hub-Signature-256`. HMAC is over *bytes*, not over meaning. `{"a": 1}` and `{"a":1}` are the same JSON document and different byte strings, with completely different digests. So if we parse the body into a dict and then re-serialise it to check the signature, verification fails on perfectly genuine requests — and the "fix" someone reaches for is to stop verifying. Worse, parsing first means untrusted input has already been through our validation code before we knew who sent it. Hence two rules in this slice: read `await request.body()` first, and **never declare a Pydantic body parameter on the POST handler** — FastAPI would read and validate the body before the handler runs, and a validation failure would answer 422 to a request we never authenticated.

**Why Meta retries.** Meta cannot tell the difference between "Doctoleb received this and is working on it" and "Doctoleb's database was down". It only sees our HTTP status. If we do not answer `200` quickly, it assumes the delivery failed and sends the same event again later — and it may also deliver the same event twice with no failure at all, or out of order (`docs/architecture.md`). That is *good*: retries are what stop a message being lost when Postgres restarts. It is only safe because of hard rule 2 — the same event ID can be stored once and no more, enforced by a unique constraint, not by us checking first. Retries plus idempotency turns "at-least-once delivery" into "exactly one stored message". So the failure semantics below are deliberate: a storage failure must answer `503` (ask Meta to try again), never `200` (tell Meta to forget it).

---

## Global Constraints

- Python 3.12 only. Dependency manager is **uv**. This slice adds **no new dependency** — `hmac`, `hashlib` and `json` are standard library, FastAPI and Pydantic are already present. `uv.lock`, the Dockerfile and its uv pin (`ghcr.io/astral-sh/uv:0.12.19`) are untouched.
- **Hard rule 1 is the shape of the endpoint.** `POST /webhooks/whatsapp` does exactly: verify signature → derive one dedupe key per item → store → return 200. No OpenAI, no Meta call, no media download, no tenant resolution, no enqueue, no `await` on anything but our own database.
- **Hard rule 2 is delegated to the database.** Dedupe is `INSERT ... ON CONFLICT (provider_event_id) DO NOTHING`, which VS-002's `WebhookInboxRepository.store_if_new` already does. The endpoint never does "SELECT then INSERT": two Meta deliveries can land in two workers at the same instant and both would see nothing.
- **Hard rule 4: no tenant resolution here.** `webhook_inbox.tenant_id` stays `NULL` on every row this slice writes. `phone_number_id` is preserved *inside the stored payload* so VS-004 can resolve the tenant from it. `DEV_TENANT_ID` is not read by any code in this slice.
- **Hard rule 8: log identifiers, never content.** No log line, exception message, or `HTTPException.detail` in this slice may contain the request body, message text, a `from`/`wa_id` phone number, or a WhatsApp profile name. Log lines carry `provider_event_id` values (a `wamid` is an opaque Meta identifier, not patient content) and counts. Every test payload is built from an integer by `tests/whatsapp_factories.py`; nothing is copied from a real delivery.
- **Hard rule 9: no secret in committed source.** `META_APP_SECRET` and `META_VERIFY_TOKEN` come from `Settings`. Tests use the obviously fake values in `tests/whatsapp_factories.py` and never the developer's real ones. The signature header value is never logged.
- **Hard rule 11 does not bite yet** — this slice makes no outbound call. It is why the storage failure path answers 503 instead of swallowing the error: the retry that eventually succeeds is Meta's, not ours.
- Linting: `ruff check .` clean, `ruff format .` leaves no diff. Line length 100, target `py312`.
- **`pytest` must still pass with no Postgres and no Redis running** (VS-001 constraint, unchanged). Tests needing a real database are marked `@pytest.mark.db`. Everything about signatures, the handshake, payload splitting and failure codes is provable with nothing running — see the counts in each task.
- Stay inside VS-003 scope. **Out of scope:** the enqueue interface and jobs, tenant resolution, contact/conversation/message writes, the Meta send client, status-event *interpretation* (VS-004); OpenAI (VS-005); media download and transcription (VS-008). Anything that looks necessary but is out of scope goes under "Follow-ups" in `docs/slices/VS-003.md`, not into the code.
- Every commit message ends with the trailer:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

---

## The response contract

This is the whole endpoint in one table. Every row has a test.

| Situation | Status | Stored | Why |
|---|---|---|---|
| `X-Hub-Signature-256` missing or empty | **401** | nothing | Unauthenticated. We never parse the body. |
| Header without the `sha256=` prefix, or not hex | **401** | nothing | A malformed signature is an invalid signature. |
| Digest does not match the raw body | **401** | nothing | Forged or tampered. |
| `META_APP_SECRET` empty or unset | **401** | nothing | An empty HMAC key is a *valid* key. "Not configured" must mean "reject". |
| Valid signature, body is not JSON | **400** | nothing | Retrying cannot fix it, so do not ask Meta to retry. Loud, not silent. |
| Valid signature, JSON we do not model (unknown `field`, empty `entry`, junk nesting) | **200** | nothing | Meta adds fields without notice; a 500 here would make it retry forever. |
| Valid signature, items found, some or all already stored | **200** | the new ones | Hard rule 2. A duplicate is a success, not an error. |
| Valid signature, items found, storage raised | **503** | nothing (one transaction) | Ask Meta to retry. Dedupe makes the retry safe. |

**The storage failure is a deliberate 503, not an uncaught exception.** Letting the original error propagate would produce a 500 *and* make uvicorn log the full traceback — and a database exception's own message can quote the data that caused it (PostgreSQL's `CONTEXT:` line on an invalid `jsonb` value includes a snippet of the JSON, which is patient content; SQLAlchemy's `hide_parameters=True` does not touch it). So the handler logs `type(error).__name__` and nothing else, then raises `HTTPException(503) from None`: `from None` cuts the `__cause__` chain so no formatter anywhere can walk back to the original message. Meta treats 503 exactly like 500 — it retries — and we lose nothing.

**200 never means "we looked at it".** It means "every event in this request is durably in `webhook_inbox`, or was already there". The only 200 with nothing stored is the row above where there was genuinely nothing to store.

`GET /webhooks/whatsapp`:

| Situation | Status | Body |
|---|---|---|
| `hub.mode == "subscribe"` and `hub.verify_token` matches | **200** | `hub.challenge`, as `text/plain`, byte for byte |
| wrong token, wrong mode, missing parameters, or unset `META_VERIFY_TOKEN` | **403** | `forbidden` |

Missing query parameters answer **403, not 422**: the parameters are declared optional so FastAPI cannot answer a validation error to an unauthenticated caller, and an incomplete handshake is a failed handshake.

---

## Dedupe granularity

One POST from Meta is **not** one event. A single `entry[].changes[].value` can carry several `messages` and several `statuses`, and the two can arrive in the same request.

**One `webhook_inbox` row per item**, with the `provider_event_id` derived like this:

| Item | `provider_event_id` |
|---|---|
| an inbound message | `msg:<wamid>` |
| a status callback | `status:<wamid>:<status>` |
| a message with no usable `id` | `msg:sha256:<hex>` |
| a status with no usable `id` or `status` | `status:sha256:<hex>` |

The hash keys are the fallback, not the normal path — see assumption A1. They are `sha256` over the **canonical JSON** of the item, `json.dumps(item, sort_keys=True, separators=(",", ":"))`, so that an identical redelivery hashes identically even if Meta reorders the object's keys. They exist so that "we cannot read this item's id" never means "we drop a patient's message": the item is stored, deduped against its own redeliveries, and visible to a human.

Why not one row per request: the request has no identity of its own. Meta assigns no request id, and hashing the body would make a redelivery that merely reordered its items look like a new event — while a redelivery that added one new message would look like a duplicate and lose it. The `wamid` is the only stable identity Meta gives us, and hard rule 2 is stated in terms of message IDs.

Why the prefixes: a message and its own `sent` status share one `wamid`. Without `msg:`/`status:` they would collide and the status would silently swallow the message. The status word is part of the key because `sent`, `delivered` and `read` for one message are three distinct events (and legitimately arrive in three requests), while the *same* status delivered twice is one event.

**Each row's payload is self-contained.** VS-004 reads only the row, never the original request, so the payload carries the item plus the envelope metadata around it:

```json
{
  "kind": "message",
  "object": "whatsapp_business_account",
  "entry_id": "<WABA id>",
  "field": "messages",
  "metadata": {"display_phone_number": "...", "phone_number_id": "..."},
  "contacts": [{"profile": {"name": "..."}, "wa_id": "..."}],
  "item": { "...the raw message or status object, unchanged..." }
}
```

`metadata.phone_number_id` is what VS-004 resolves the tenant from (hard rule 4). `contacts` is carried on message rows only — it is where the WhatsApp profile name lives, and VS-004 matches it to the message by `wa_id`. Status rows carry `recipient_id` inside `item` instead. `kind` is an explicit field so **VS-004 reads `payload["kind"]` and never string-splits `provider_event_id`** — the key format is ours and may change.

**All rows from one POST go in one transaction.** One session, one `commit()` at the end. A POST that stored three of five items and then failed would answer 503, Meta would redeliver, and the three stored rows would be correctly deduped — so partial storage is not a correctness bug. It is still wrong: it makes "was this request stored?" unanswerable, and it splits one atomic delivery into two half-states for no gain.

**The loop, not one multi-row INSERT.** The endpoint calls `store_if_new` once per item inside the one transaction. This reuses VS-002's repository unchanged, gives each item its own independent conflict resolution, and sidesteps multi-row `ON CONFLICT` semantics entirely when Meta puts the same item in one request twice.

---

## Assumptions

Listed, not decided silently. Each has a named consequence.

**A1. Meta normally sets `id` on a message and on a status, and `status` on a status — but we do not depend on it.** An item that fails its model (a missing `id`, or a status with no `status`) is **still stored**, under a content-hash key: `msg:sha256:<hex>` / `status:sha256:<hex>` over the canonical JSON of the item. Nothing is dropped, and hard rule 2 still holds — an identical redelivery produces the identical hash and therefore one row. The log names the kind only, never the item. *Why not skip it:* a skipped item is a lost patient message with nothing but a `WARNING` to show for it. *Why not 503:* Meta would redeliver the same unreadable item until it gave up, and it would be lost anyway, with no record. *Consequence:* a genuinely malformed item that Meta redelivers with a changed `timestamp` hashes differently and lands twice. Two rows a human has to read beats zero rows nobody knows about, and VS-004 dead-letters what it cannot interpret.

**A2. A derived `provider_event_id` fits `webhook_inbox.provider_event_id` (`VARCHAR(255)`).** A `wamid` is ~60–130 characters, and the longest prefix adds 15. *Not defended in code:* truncating would be worse than failing, because two different `wamid`s could truncate to one key and collide. An over-length key raises from asyncpg and becomes a 503.

**A3. `META_APP_SECRET` and `META_VERIFY_TOKEN` default to `""`, and the app still starts without them.** *Why:* `/health` must stay up, and every other slice's tests and local work must run without Meta credentials — making them required would mean `pytest` and `docker compose up` fail for a developer who has no Meta app yet, which is precisely the developer this slice has. The failure is moved to request time instead, where it is *total*: an unset secret rejects every POST with 401 and an unset verify token rejects every handshake with 403. Contrast `DATABASE_URL`, which VS-001 made required with no default: there is no safe degraded mode for a database, whereas "a webhook that trusts nothing" is a correct and safe state. A startup `WARNING` names which of the two is unset (names only, never values), so the reason a handshake keeps failing is visible in the log.

**A4. Malformed JSON with a valid signature answers 400.** It means someone holding our app secret sent a broken body; retrying will not change it. 400 keeps it out of Meta's retry queue and out of our inbox, and is loud in the log.

**A5. The models validate; the raw dict is what gets stored.** Every model — envelope and item alike — uses `extra="allow"`, and the payload written to `webhook_inbox` is the raw `dict` from `json.loads`, never `model_dump()` of a narrowed model. `InboundMessage` and `StatusUpdate` exist to say what we *require* of an item (a message needs an `id`; a status needs an `id` and a `status`) and to give the extractor one place to read those fields from — not to define the stored shape. With `extra="ignore"` or a narrow model as the storage source, everything we did not think to declare — `text.body`'s siblings, `audio.id` (VS-008), interactive replies, a status's `pricing` and `errors` — would be silently deleted from the one place VS-004 and VS-008 read. We never dynamically `getattr` an extra key; we read declared fields and store the original.

**A6. Only `X-Hub-Signature-256` is accepted.** Meta also sends the legacy SHA-1 `X-Hub-Signature`. It is ignored; SHA-1 is not a fallback we want. *Consequence:* none today, and it is one function to extend.

**A7. The OpenAPI surface is gated on its own setting, `DOCS_ENABLED`, default `false` — not on `APP_ENV`.** An `APP_ENV`-based gate would be useless for the exposure it is meant to fix: the tunnel runs *while* `APP_ENV=development`, which is exactly when a condition like `app_env == "development"` would serve `/docs` to the internet. So the two decisions are separated: `APP_ENV` says what kind of environment this is, `DOCS_ENABLED` says whether this process publishes its schema, and it is off unless someone deliberately turns it on. A developer who wants Swagger UI sets `DOCS_ENABLED=true` in `.env` and — knowing why — does not run a tunnel at the same time.

*`/health` and `/health/ready` stay publicly reachable through the tunnel.* Their bodies name dependency status only and carry no credential (VS-001 made sure of that). Real path restriction belongs to the tunnel, is documented in the README, and is a follow-up rather than code.

**A8. A status delivered twice with the same `status` value is one event; only the first is kept.** *Consequence:* `webhook_inbox` is not a status *history* — it records first-seen per `(wamid, status)`. VS-004 must not count rows to count deliveries.

**A9. `webhook_inbox.provider` stays `"whatsapp"`** (VS-002's column default). Nothing in this slice writes it explicitly.

**A10. The path is `/webhooks/whatsapp`**, from `docs/architecture.md`. Not an assumption — recorded so the Meta dashboard value in Task 7 has one source.

---

## VS-001 and VS-002 follow-ups: what is pulled in, and what is not

**Pulled in: exactly one** — the VS-001 follow-up this slice's own tunnel creates.

| Follow-up | Origin | Verdict |
|---|---|---|
| "VS-003's tunnel will expose the whole api, including `/docs`, `/openapi.json` and `/health/ready`" | VS-001 | **Pulled in (Task 1).** This slice creates the exposure, so it owns it. `create_app()` serves the OpenAPI surface only when the new `DOCS_ENABLED` setting is true, and it defaults to false — deliberately independent of `APP_ENV`, because the tunnel runs while `APP_ENV=development` (A7). A route-inventory test pins what the tunnel can reach. `/health/ready` stays public — see A7. |
| `ConversationRepository.get_or_create_open` has no retry; VS-004's caller must treat its `IntegrityError` as retryable | VS-002 | Not in scope. VS-004's job code, not the webhook. |
| No index on `webhook_inbox.status` | VS-002 | Not in scope, and still premature: this slice only inserts. VS-004 is the first reader with a query pattern. |
| `webhook_inbox.payload` retains patient text and phone numbers indefinitely | VS-002 | Not in scope, but **this slice is what makes it real** — until now the column was empty in production terms. Re-stated as a follow-up on VS-003 with the same conclusion: pair the retention policy with VS-008's audio retention. |
| `as_duplicate()` translates every `IntegrityError`, not only unique violations | VS-002 | Not in scope. The webhook path never calls it: `store_if_new` uses `ON CONFLICT DO NOTHING` and raises no duplicate error. |
| Test database created and never dropped; `tenant_id` type (A3 in the VS-002 plan) still unconfirmed | VS-002 | Not in scope. This slice writes no `tenant_id` at all, so it neither depends on nor advances that question. |
| Container healthchecks, multi-stage Dockerfile, `restart: unless-stopped`, structured JSON logging with a request id, sequential readiness probes, orphaned-future log line, Redis no-credential log test, compose credential defaults | VS-001 | Not in scope. Nothing in this slice touches them. Structured logging with a request id is tempting now that there is a request worth tracing — it stays a follow-up rather than growing this slice. |

---

## Review Focus

Nine conditions the slice implies but does not spell out. Each has a test in the task that owns the code.

1. **Verification must precede parsing, on the exact bytes.** Two failure shapes hide here: re-serialising the body before hashing (breaks genuine requests), and letting FastAPI validate a body model before the handler runs (answers 422 to an unauthenticated caller, and parses untrusted input first). Tests: a body with unusual whitespace verifies (Task 2, Task 4); a bad signature on a *malformed* body answers 401, not 400 or 422 (Task 4); a valid signature on a shape we do not model answers 200, not 422 (Task 4).
2. **An unset secret must reject, not accept.** `hmac.new(b"", body)` is a valid HMAC and `compare_digest("", "")` is `True`. Without explicit guards, deploying with an empty `META_APP_SECRET` would let anyone forge a signature, and an empty `META_VERIFY_TOKEN` would hand the handshake to anyone who sent an empty token. Tests: an unset secret rejects a signature computed *with* the empty key (Task 2); an unset verify token answers 403 to `hub.verify_token=` (Task 4).
3. **One POST is many events.** The naive reading of hard rule 2 — one row per request — loses messages whenever Meta batches. Tests: three messages plus two statuses become five rows; the same POST twice still yields five (Task 5).
4. **A 200 must mean stored — and the failure that says otherwise must not narrate itself.** Two opposite mistakes: `try/except: return 200` to "keep Meta happy" converts a database outage into permanently lost messages; letting the original exception propagate answers 500 *and* hands uvicorn a traceback whose message can quote the offending data (PostgreSQL's `CONTEXT:` line on an invalid `jsonb` value). The handler does neither: it logs the exception class name, then raises `HTTPException(503) from None`. Tests: an injected storage failure answers 503, leaves zero rows, propagates no exception through the normal (`raise_app_exceptions=True`) client, and puts no patient content in the log (Task 5).
5. **A shape we do not model must not 500, and must not touch the database.** Meta adds webhook fields without notice and sends field types we never subscribed to. Test: an unknown `field` answers 200 with a session that raises if it is used at all (Task 4).
6. **Three statuses for one message are three events; the same status twice is one.** Getting this wrong in either direction is invisible until VS-004 either loses `delivered` or double-counts it. Tests: `sent`/`delivered`/`read` produce three rows; a repeated `sent` produces one (Task 5).
7. **The stored payload must be enough for VS-004 on its own — validating an item must not narrow it.** The row is the only thing the worker sees. If `phone_number_id` is dropped, tenant resolution is impossible (hard rule 4); if the models become the storage source, VS-008's `audio.id` disappears the moment `InboundMessage` fails to declare it. The item models validate and read; the raw dict is stored. Tests: `phone_number_id` and the raw item survive; an undeclared key inside the message survives the round trip; a non-text message type (`audio`) validates and stores with its media keys intact (Tasks 3 and 5).
8. **Nothing in a log line identifies a patient.** This is the first slice that handles real message text, and the tempting log line — "received: <body>" — is a hard rule 8 violation on day one. The failure paths are the sneaky ones: a 400 that echoes the body, or `logger.exception` on a storage error. Tests with `caplog` on the happy path, the rejected-signature path, the unparseable-body path and the storage-failure path (Tasks 4 and 5).
9. **The tunnel must publish the webhook and nothing else.** A public URL to a dev machine that also serves `/openapi.json` hands over the shape of every future endpoint. The gate must not be `APP_ENV`: the tunnel runs while `APP_ENV=development`, so that condition would be open at exactly the wrong moment. Tests: `/docs`, `/redoc` and `/openapi.json` are 404 by default *with `APP_ENV=development`*, they appear only when `DOCS_ENABLED` is true, `/health` still answers with them off, and the route inventory is exactly what we intend (Tasks 1 and 4).

---

## Running the tests

Unchanged from VS-002: `uv run pytest` with nothing running must pass, database tests skip with a reason.

```bash
uv run pytest                                    # nothing running: 88 passed, 43 skipped
docker compose up -d postgres && uv run pytest   # 131 passed
docker compose exec api pytest                   # 131 passed (the run acceptance is judged on)
```

Baseline before this slice: **74 tests (40 passed, 34 skipped with no Postgres)**.

The DB-backed endpoint tests live in `tests/api/` but need `tests/db/conftest.py`'s fixtures, which are scoped to `tests/db/`. `tests/api/conftest.py` re-exports them with an explicit import (`from tests.db.conftest import db_engine, db_session, migrated_database, test_database_url  # noqa: F401`) rather than moving them up to `tests/conftest.py` — moving them would make every test in the suite import Alembic and asyncpg for the sake of two modules.

Those tests override the `get_session` dependency with VS-002's `db_session`, which is bound to a connection in a transaction that is always rolled back, with `join_transaction_mode="create_savepoint"`. The endpoint calls `session.commit()`; the savepoint makes that real from the session's point of view and still invisible to the next test.

---

### Task 1: Meta settings, and closing the tunnel exposure

Three small changes that everything else stands on: the two Meta secrets in `Settings`, a `DOCS_ENABLED` switch, and an app that does not publish its own API documentation through a public tunnel.

**Files:**
- Modify: `app/config.py` (`meta_app_secret`, `meta_verify_token`, `docs_enabled`)
- Modify: `app/main.py` (`create_app(settings=None)`, OpenAPI surface gated on `docs_enabled`, startup warning for unset Meta secrets)
- Modify: `.env.example` (add `DOCS_ENABLED`; add comments to the existing Meta keys — **every existing key stays exactly as it is**, nothing renamed, reordered or removed)
- Modify: `tests/conftest.py` (add a `client_for` factory fixture)
- Create: `tests/api/__init__.py`
- Test: `tests/test_config.py` (+3), `tests/api/test_route_exposure.py` (3 new)

**Interfaces:**
- Consumes: `app.config.get_settings()`.
- Produces: `Settings.meta_app_secret: str = ""`, `Settings.meta_verify_token: str = ""`, `Settings.docs_enabled: bool = False`; `create_app(settings: Settings | None = None)` with `openapi_url`/`docs_url`/`redoc_url` set to `None` unless `docs_enabled`, and `app.state.settings`.

**Expected tests after this task: 80 — 46 passed, 34 skipped with no Postgres; 80 passed with it.**

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_meta_secrets_default_to_empty_and_do_not_block_startup(monkeypatch):
    """Assumption A3. The app must boot without a Meta app.

    Making these required would mean pytest and `docker compose up` fail for a
    developer who has no Meta developer app yet — which is exactly the developer
    this slice is written for. The rejection happens per request instead, where
    it is total: see tests/api/test_whatsapp_webhook.py and _verify.py.
    """
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.delenv("META_VERIFY_TOKEN", raising=False)

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:pw@db:5432/doctoleb",
        redis_url="redis://cache:6379/1",
    )

    assert settings.meta_app_secret == ""
    assert settings.meta_verify_token == ""


def test_meta_secrets_are_read_from_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.delenv("META_VERIFY_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://user:pw@db:5432/doctoleb\n"
        "REDIS_URL=redis://cache:6379/1\n"
        "META_APP_SECRET=not-a-real-secret\n"
        "META_VERIFY_TOKEN=not-a-real-token\n"
    )

    settings = Settings(_env_file=env_file)

    assert settings.meta_app_secret == "not-a-real-secret"
    assert settings.meta_verify_token == "not-a-real-token"


def test_docs_are_disabled_by_default_and_are_not_tied_to_app_env(monkeypatch):
    """Assumption A7.

    DOCS_ENABLED is deliberately its own switch. Gating the OpenAPI surface on
    APP_ENV would be open at exactly the wrong moment: the tunnel that publishes
    this API to the internet runs while APP_ENV=development.
    """
    monkeypatch.delenv("DOCS_ENABLED", raising=False)
    base = {
        "database_url": "postgresql+asyncpg://user:pw@db:5432/doctoleb",
        "redis_url": "redis://cache:6379/1",
    }

    assert Settings(_env_file=None, app_env="development", **base).docs_enabled is False
    assert Settings(_env_file=None, app_env="production", **base).docs_enabled is False
    # And it is a real env-driven boolean, not a constant.
    monkeypatch.setenv("DOCS_ENABLED", "true")
    assert Settings(_env_file=None, **base).docs_enabled is True
```

Create `tests/api/__init__.py` (empty) and `tests/api/test_route_exposure.py`:

```python
"""Review Focus 9, and VS-001's follow-up.

VS-003 puts this API behind a public tunnel. Everything the tunnel can reach is
reachable by anyone who learns the URL, so what the app publishes is now a
security question rather than a convenience one.
"""

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.main import create_app


def _app(docs_enabled: bool = False, app_env: str = "development") -> FastAPI:
    """A fresh app with `docs_enabled` set, without touching the process env.

    app_env defaults to "development" on purpose: that is what runs while the
    tunnel is up, so every assertion below is made under the configuration the
    exposure actually happens in.

    create_app() reads settings once, at build time, so the override has to be in
    place before the app exists — which is why this builds its own rather than
    using the `app` fixture.
    """
    return create_app(
        settings=Settings(
            _env_file=None,
            app_env=app_env,
            docs_enabled=docs_enabled,
            database_url="postgresql+asyncpg://user:pw@db:5432/doctoleb",
            redis_url="redis://cache:6379/1",
        )
    )


async def test_docs_are_not_served_by_default_even_in_development(client_for):
    """Review Focus 9.

    APP_ENV is development here — the tunnel's own configuration. The tunnel
    would otherwise hand a stranger the full shape of every endpoint this repo
    will ever have, including the ones not written yet.
    """
    async with client_for(_app()) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await client.get(path)).status_code == 404, path


async def test_docs_are_served_only_when_docs_enabled_is_set(client_for):
    async with client_for(_app(docs_enabled=True)) as client:
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/openapi.json")).status_code == 200


async def test_health_still_answers_with_the_docs_off(client_for):
    # Disabling the docs must not disable the app. /health is how the container
    # healthcheck and the developer both check the process is alive.
    async with client_for(_app()) as client:
        assert (await client.get("/health")).status_code == 200
```

Add the `client_for` factory to `tests/conftest.py`, next to the existing `client` fixture:

```python
@pytest.fixture
def client_for():
    """An async-context client factory for a caller-built app.

    The `client` fixture covers the common case (the `app` fixture's app). Tests
    that need an app configured differently — a different APP_ENV, different Meta
    secrets — build it themselves and wrap it with this.
    """

    def build(app):
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return build
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_config.py tests/api -v
```

Expected: `ValidationError`/`AttributeError` on `meta_app_secret` and `docs_enabled`, and `TypeError: create_app() got an unexpected keyword argument 'settings'`.

- [ ] **Step 3: Add the three settings to `app/config.py`**

After `log_level`:

```python
    # Whether this process publishes /docs, /redoc and /openapi.json.
    # Off by default, and deliberately NOT derived from app_env (assumption A7):
    # VS-003 puts this API behind a public tunnel so Meta can reach it, and that
    # tunnel runs while APP_ENV=development — so an app_env-based gate would be
    # open at precisely the moment the API is reachable from the internet.
    # Turn it on locally when you want Swagger UI, with no tunnel running.
    docs_enabled: bool = False
```

After `redis_url`:

```python
    # Meta WhatsApp Cloud API. Deliberately NOT required, and deliberately
    # empty by default (plan assumption A3):
    #   * the app must boot without a Meta app, or no other slice can be worked
    #     on and `pytest` fails for a developer who has not been granted one yet;
    #   * an empty value is not a permissive value. app_secret="" rejects every
    #     POST (an empty HMAC key is a valid key, so "unset" must mean "reject"),
    #     and verify_token="" rejects every handshake.
    # app/channels/whatsapp/signature.py and app/api/whatsapp.py enforce that.
    meta_app_secret: str = ""
    meta_verify_token: str = ""
```

- [ ] **Step 4: Gate the OpenAPI surface in `app/main.py`**

Add `import logging`, `logger = logging.getLogger(__name__)` and `from app.config import Settings, get_settings` at the top, then:

```python
def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. A factory, not a module-level app, so each test gets a
    clean instance and dependency overrides cannot leak between tests.

    `settings` is injectable for tests that need a differently configured app
    (a different APP_ENV, different Meta secrets). Production passes nothing and
    gets the process-wide settings.
    """
    settings = settings or get_settings()

    # VS-001 follow-up, closed here because VS-003 is what creates the exposure:
    # this API is about to sit behind a public tunnel so Meta can reach it. By
    # default it publishes no schema and no interactive docs.
    #
    # Gated on docs_enabled and NOT on app_env: the tunnel runs while
    # APP_ENV=development, so an app_env condition would be open exactly when the
    # API is reachable from the internet (assumption A7).
    #
    # openapi_url=None alone would disable /docs, but naming all three keeps the
    # intent readable and the test honest.
    app = FastAPI(
        title="Doctoleb WhatsApp Agent",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.include_router(health_router)

    if not settings.meta_app_secret:
        # Names only, never values (hard rule 9). Without this line, "every
        # handshake and every webhook is rejected" looks like a bug in the code
        # rather than a missing .env entry.
        logger.warning("META_APP_SECRET is not set: every WhatsApp webhook POST will be rejected")
    if not settings.meta_verify_token:
        logger.warning("META_VERIFY_TOKEN is not set: the Meta handshake will be rejected")

    return app
```

- [ ] **Step 5: Add `DOCS_ENABLED` and comment the Meta keys in `.env.example`**

Two edits, and nothing else. **Every existing key keeps its exact name, value and position** — the only additions are comment lines and one new key. `DOCS_ENABLED` is the only key whose absence changes behaviour, and its default is the safe one, so an existing `.env` that predates this slice keeps working unchanged.

In the `# App` block, after `LOG_LEVEL=INFO`:

```
# Serve /docs, /redoc and /openapi.json. Off by default and independent of
# APP_ENV: the tunnel that lets Meta reach this API runs while APP_ENV=development,
# so tying the docs to APP_ENV would publish them to the internet. Turn this on
# for local Swagger UI, with no tunnel running.
DOCS_ENABLED=false
```

In the `# Meta WhatsApp Cloud API` block, add two comments above the keys that already exist there (`META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID` and `META_API_VERSION` are left untouched):

```
# App Dashboard -> Settings -> Basic -> App Secret. Signs every webhook POST.
# Empty means every POST to /webhooks/whatsapp is rejected with 401.
META_APP_SECRET=
# A string you invent, then paste into the Meta webhook configuration form.
# Empty means the GET handshake is rejected with 403.
META_VERIFY_TOKEN=
```

- [ ] **Step 6: Run the tests, lint, format**

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format .
```

Expected: `46 passed, 34 skipped`.

- [ ] **Step 7: Checkpoint with the developer**

---

### Task 2: Signature verification

One function, ten tests. It is the entire authentication of this API, so it gets its own task and its own module.

**Files:**
- Create: `app/channels/__init__.py`, `app/channels/whatsapp/__init__.py`
- Create: `app/channels/whatsapp/signature.py`
- Create: `tests/channels/__init__.py`
- Create: `tests/whatsapp_factories.py`
- Test: `tests/channels/test_signature.py` (10 new)

**Interfaces:**
- Produces:
  - `app.channels.whatsapp.signature.SIGNATURE_HEADER = "X-Hub-Signature-256"`
  - `app.channels.whatsapp.signature.verify_signature(raw_body: bytes, header_value: str | None, app_secret: str) -> bool`
  - `tests/whatsapp_factories.py`: `APP_SECRET`, `VERIFY_TOKEN`, `PHONE_NUMBER_ID`, `PATIENT_TEXT`, `PROFILE_NAME`, `phone()`, `wamid()`, `text_message()`, `status_update()`, `contact()`, `envelope()`, `to_bytes()`, `digest()`, `signed()`.

**Expected tests after this task: 90 — 56 passed, 34 skipped with no Postgres.**

- [ ] **Step 1: Write the synthetic payload factory**

Create `tests/whatsapp_factories.py`:

```python
"""Synthetic Meta webhook payloads, and the signing helper tests use.

Hard rule 8: no fixture in this repo is built from a real delivery. Every value
here is derived from an integer, the phone numbers come from the same
documentation-safe range as tests/db/factories.py, and the secrets are obvious
fakes (hard rule 9 — a real one must never reach a test file).
"""

import hashlib
import hmac
import json
from typing import Any

APP_SECRET = "test-app-secret-not-a-real-one"
VERIFY_TOKEN = "test-verify-token-not-a-real-one"
PHONE_NUMBER_ID = "100000000000001"
WABA_ID = "200000000000002"

# The text a synthetic patient sends, and the synthetic profile name. Both are
# asserted ABSENT from every log line.
PATIENT_TEXT = "synthetic message body"
PROFILE_NAME = "Synthetic Patient"


def phone(n: int = 1) -> str:
    return f"96170{n:06d}"


def wamid(n: int = 1) -> str:
    return f"wamid.TEST{n:08d}"


def text_message(n: int = 1, body: str = PATIENT_TEXT, **extra: Any) -> dict[str, Any]:
    """One inbound text message, in Meta's shape."""
    message: dict[str, Any] = {
        "from": phone(n),
        "id": wamid(n),
        "timestamp": "1730000000",
        "type": "text",
        "text": {"body": body},
    }
    message.update(extra)
    return message


def status_update(n: int = 1, state: str = "sent", **extra: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "id": wamid(n),
        "status": state,
        "timestamp": "1730000001",
        "recipient_id": phone(n),
        "conversation": {"id": f"conv-{n:08d}"},
    }
    status.update(extra)
    return status


def contact(n: int = 1) -> dict[str, Any]:
    return {"profile": {"name": PROFILE_NAME}, "wa_id": phone(n)}


def envelope(
    messages: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    field: str = "messages",
    with_metadata: bool = True,
) -> dict[str, Any]:
    """A full webhook body. Only the keys Meta actually sends."""
    value: dict[str, Any] = {"messaging_product": "whatsapp"}
    if with_metadata:
        value["metadata"] = {
            "display_phone_number": phone(999),
            "phone_number_id": PHONE_NUMBER_ID,
        }
    if messages is not None:
        value["contacts"] = contacts if contacts is not None else [contact()]
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": WABA_ID, "changes": [{"field": field, "value": value}]}],
    }


def to_bytes(payload: dict[str, Any] | str | bytes) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload).encode("utf-8")


def digest(payload: dict[str, Any] | str | bytes, secret: str = APP_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), to_bytes(payload), hashlib.sha256).hexdigest()


def signed(
    payload: dict[str, Any] | str | bytes, secret: str = APP_SECRET
) -> tuple[bytes, dict[str, str]]:
    """The exact bytes to send, and the header that signs those exact bytes.

    Returning both together is the point: a test that builds the body twice —
    once to sign, once to send — can pass while the endpoint re-serialises, which
    is the bug Review Focus 1 exists to catch.
    """
    raw = to_bytes(payload)
    return raw, {"X-Hub-Signature-256": f"sha256={digest(raw, secret)}"}
```

- [ ] **Step 2: Write the failing signature tests**

Create `tests/channels/__init__.py` (empty) and `tests/channels/test_signature.py`:

```python
"""The entire authentication of this API. No database, no app, no network."""

import json

from app.channels.whatsapp.signature import SIGNATURE_HEADER, verify_signature
from tests.whatsapp_factories import APP_SECRET, digest, envelope, text_message, to_bytes

BODY = to_bytes(envelope(messages=[text_message()]))


def _header(value: str) -> str:
    return f"sha256={value}"


def test_the_header_name_is_the_one_meta_sends():
    # A typo here means every request arrives unsigned and every request is
    # rejected, which looks exactly like a wrong app secret.
    assert SIGNATURE_HEADER == "X-Hub-Signature-256"


def test_a_signature_from_the_right_secret_verifies():
    assert verify_signature(BODY, _header(digest(BODY)), APP_SECRET) is True


def test_a_signature_from_another_secret_is_rejected():
    forged = _header(digest(BODY, "someone-elses-secret"))

    assert verify_signature(BODY, forged, APP_SECRET) is False


def test_one_changed_byte_in_the_body_is_rejected():
    header = _header(digest(BODY))
    tampered = BODY.replace(b"synthetic", b"Synthetic")

    assert tampered != BODY
    assert verify_signature(tampered, header, APP_SECRET) is False


def test_a_missing_or_empty_header_is_rejected():
    for header in (None, "", "sha256=", "   "):
        assert verify_signature(BODY, header, APP_SECRET) is False, repr(header)


def test_a_header_without_the_sha256_prefix_is_rejected():
    # The bare digest is not accepted: tolerating it would mean guessing the
    # algorithm, and "sha1=" is a different, weaker one Meta also sends.
    for header in (digest(BODY), f"sha1={digest(BODY)}", f"SHA256={digest(BODY)}"):
        assert verify_signature(BODY, header, APP_SECRET) is False, header


def test_a_header_that_is_not_hex_is_rejected():
    # bytes.fromhex raises on junk; that must become False, not a 500.
    for header in (_header("not-hex-at-all"), _header("abc"), _header("é" * 64)):
        assert verify_signature(BODY, header, APP_SECRET) is False, header


def test_hex_case_does_not_matter():
    # Hex case carries no meaning. Comparing the decoded bytes rather than the
    # strings makes this true by construction instead of by a .lower() someone
    # can delete.
    assert verify_signature(BODY, _header(digest(BODY).upper()), APP_SECRET) is True


def test_an_unset_app_secret_rejects_even_a_correctly_computed_signature():
    """Review Focus 2.

    hmac.new(b"", body) is a perfectly valid HMAC. If an unset secret merely
    meant "the key is the empty string", anyone who guessed that META_APP_SECRET
    was missing could sign their own requests.
    """
    assert verify_signature(BODY, _header(digest(BODY, "")), "") is False
    assert verify_signature(BODY, _header(digest(BODY)), "") is False


def test_the_digest_is_over_the_exact_bytes_not_reserialised_json():
    """Review Focus 1.

    Meta's body is not what json.dumps() would produce: different spacing,
    different unicode escaping, a key order we do not control. HMAC is over
    bytes, so a verifier that re-serialises rejects genuine requests — and the
    usual "fix" for that is to stop verifying.
    """
    spaced = b'{"object": "whatsapp_business_account" ,   "entry": [] }'
    reserialised = to_bytes(json.loads(spaced))
    assert spaced != reserialised

    assert verify_signature(spaced, _header(digest(spaced)), APP_SECRET) is True
    # A digest over the re-serialised form must NOT verify against the original.
    assert verify_signature(spaced, _header(digest(reserialised)), APP_SECRET) is False
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/channels -v
```

Expected: `ModuleNotFoundError: No module named 'app.channels'`.

- [ ] **Step 4: Write `app/channels/whatsapp/signature.py`**

Create the two empty `__init__.py` files first (`app/channels/`, `app/channels/whatsapp/`), then:

```python
"""X-Hub-Signature-256 verification.

This is the only thing standing between our database and the public internet:
the webhook URL is reachable by anyone, so "Meta sent this" means exactly "the
digest matches, computed with our app secret, over the bytes we received".
"""

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="


def verify_signature(raw_body: bytes, header_value: str | None, app_secret: str) -> bool:
    """True only if `header_value` is Meta's HMAC-SHA256 of `raw_body`.

    Takes RAW bytes, never a parsed object. HMAC is defined over bytes, and
    re-serialising parsed JSON changes whitespace, escaping and key order — so a
    verifier that hashes `json.dumps(parsed)` rejects genuine deliveries. That is
    why the endpoint reads `await request.body()` before it parses anything, and
    why it declares no pydantic body parameter (FastAPI would parse first).

    Returns False instead of raising, for every failure: a malformed header is an
    invalid signature, not a server error. The caller turns False into 401.

    An empty `app_secret` rejects everything. hmac.new(b"", body) is a valid
    HMAC, so treating "unset" as "the key is the empty string" would let anyone
    who guessed the secret was missing forge a signature (Review Focus 2).
    """
    if not app_secret:
        return False
    if not header_value or not header_value.startswith(SIGNATURE_PREFIX):
        return False
    try:
        # Decoding to bytes, rather than comparing hex strings, handles upper and
        # lower case hex for free and rejects junk here instead of at compare
        # time. compare_digest on str also raises TypeError on a non-ASCII value,
        # which a hostile header supplies for free.
        received = bytes.fromhex(header_value.removeprefix(SIGNATURE_PREFIX))
    except ValueError:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    # compare_digest, never ==. A plain comparison returns as soon as two bytes
    # differ, so the time it takes measures how many leading bytes were right,
    # and an attacker recovers the digest one byte at a time.
    return hmac.compare_digest(expected, received)
```

- [ ] **Step 5: Run the tests, lint, format**

```bash
uv run pytest tests/channels -v && uv run pytest -q
uv run ruff check . && uv run ruff format .
```

Expected: 10 passed, then `56 passed, 34 skipped`.

- [ ] **Step 6: Checkpoint with the developer**

---

### Task 3: Payload models and the item split

The Pydantic layer from the slice's scope line, plus the function that turns one envelope into a list of things to store. Tolerant by design and lossless by design — two properties that pull in opposite directions, which is why they are one task.

**Files:**
- Create: `app/channels/whatsapp/payloads.py`
- Test: `tests/channels/test_payloads.py` (13 new)

**Interfaces:**
- Produces:
  - `InboxItemKind(StrEnum)` — `MESSAGE = "message"`, `STATUS = "status"`
  - `InboxItem` — frozen dataclass: `provider_event_id: str`, `kind: InboxItemKind`, `payload: dict[str, Any]`
  - `MetaWebhookEnvelope`, `Entry`, `Change`, `ChangeValue`, `Metadata` — tolerant envelope models
  - `InboundMessage` — `id` required; `from_` (alias `from`), `timestamp`, `type`, `text` optional
  - `StatusUpdate` — `id` and `status` required; `recipient_id`, `timestamp`, `errors` optional
  - `content_hash(item: dict) -> str` — sha256 over canonical JSON
  - `extract_inbox_items(payload: Any) -> list[InboxItem]` — never raises

**Expected tests after this task: 103 — 69 passed, 34 skipped with no Postgres.**

- [ ] **Step 1: Write the failing extraction tests**

Create `tests/channels/test_payloads.py`:

```python
"""Splitting one Meta envelope into the things we store.

No database and no app: this is a pure function over dicts.
"""

from app.channels.whatsapp.payloads import (
    InboundMessage,
    InboxItemKind,
    StatusUpdate,
    content_hash,
    extract_inbox_items,
)
from tests.whatsapp_factories import (
    PATIENT_TEXT,
    PHONE_NUMBER_ID,
    PROFILE_NAME,
    contact,
    envelope,
    phone,
    status_update,
    text_message,
    wamid,
)


def test_one_text_message_becomes_one_item_keyed_by_wamid():
    items = extract_inbox_items(envelope(messages=[text_message(1)]))

    assert [i.provider_event_id for i in items] == [f"msg:{wamid(1)}"]
    assert items[0].kind is InboxItemKind.MESSAGE


def test_a_status_is_keyed_by_wamid_and_status():
    # The msg: / status: prefixes are not decoration: a message and its own
    # `sent` callback share one wamid, so without them the status would collide
    # with the message and one of the two would be silently dropped.
    items = extract_inbox_items(envelope(statuses=[status_update(1, "sent")]))

    assert [i.provider_event_id for i in items] == [f"status:{wamid(1)}:sent"]
    assert items[0].kind is InboxItemKind.STATUS


def test_three_statuses_for_one_message_are_three_distinct_items():
    """Review Focus 6. sent, delivered and read are three events, not retries."""
    statuses = [status_update(1, s) for s in ("sent", "delivered", "read")]

    items = extract_inbox_items(envelope(statuses=statuses))

    assert [i.provider_event_id for i in items] == [
        f"status:{wamid(1)}:sent",
        f"status:{wamid(1)}:delivered",
        f"status:{wamid(1)}:read",
    ]
    assert len({i.provider_event_id for i in items}) == 3


def test_messages_and_statuses_in_one_post_all_become_items():
    """Review Focus 3. One POST is not one event."""
    body = envelope(
        messages=[text_message(1), text_message(2), text_message(3)],
        statuses=[status_update(8, "delivered"), status_update(9, "read")],
    )

    items = extract_inbox_items(body)

    assert len(items) == 5
    assert [i.kind for i in items] == [InboxItemKind.MESSAGE] * 3 + [InboxItemKind.STATUS] * 2


def test_the_stored_payload_carries_everything_vs004_needs():
    """Review Focus 7. The worker sees the row, never the request."""
    items = extract_inbox_items(envelope(messages=[text_message(1)], contacts=[contact(1)]))
    payload = items[0].payload

    # Tenant resolution (hard rule 4) is impossible without this.
    assert payload["metadata"]["phone_number_id"] == PHONE_NUMBER_ID
    assert payload["kind"] == "message"
    assert payload["field"] == "messages"
    assert payload["item"]["id"] == wamid(1)
    assert payload["item"]["text"]["body"] == PATIENT_TEXT
    # The profile name lives on the contacts list, not on the message.
    assert payload["contacts"][0]["profile"]["name"] == PROFILE_NAME
    assert payload["contacts"][0]["wa_id"] == phone(1)


def test_an_item_is_still_produced_when_the_envelope_has_no_metadata():
    # Storing it is still right: the row is a durable record of what arrived, and
    # VS-004 dead-letters what it cannot resolve. Dropping it here would lose a
    # real patient message to a shape change.
    items = extract_inbox_items(envelope(messages=[text_message(1)], with_metadata=False))

    assert len(items) == 1
    assert items[0].payload["metadata"] is None


def test_unknown_keys_inside_a_message_survive_the_round_trip():
    """Review Focus 7, the half a strict model would break.

    extra="ignore" would silently delete every key we did not declare — which is
    where VS-008's audio.id and Meta's next addition live.
    """
    body = envelope(
        messages=[text_message(1, referral={"source_type": "ad"}, context={"id": wamid(7)})]
    )

    item = extract_inbox_items(body)[0]

    assert item.payload["item"]["referral"] == {"source_type": "ad"}
    assert item.payload["item"]["context"] == {"id": wamid(7)}


def test_a_field_we_do_not_handle_produces_no_items():
    # Meta sends field types nobody subscribed to. 200 and nothing stored.
    body = envelope(field="account_update")
    body["entry"][0]["changes"][0]["value"] = {"phone_number": phone(1), "event": "VERIFIED"}

    assert extract_inbox_items(body) == []


def test_shapes_we_do_not_model_produce_no_items_and_no_exception():
    """Review Focus 5. None of these may raise; all of them answer 200 upstream."""
    shapes = [
        {},
        {"object": "whatsapp_business_account"},
        {"object": "whatsapp_business_account", "entry": []},
        {"entry": [{}]},
        {"entry": [{"changes": []}]},
        {"entry": [{"changes": [{"field": "messages"}]}]},
        {"entry": [{"changes": [{"field": "messages", "value": {}}]}]},
        # Structurally broken: entry is not a list of objects.
        {"entry": "nope"},
        {"entry": ["nope"]},
        {"entry": [{"changes": {"field": "messages"}}]},
        # Right shape, wrong element type.
        {"entry": [{"changes": [{"field": "messages", "value": {"messages": ["nope"]}}]}]},
        # Not even an object.
        [],
        "nope",
        None,
    ]
    for shape in shapes:
        assert extract_inbox_items(shape) == [], repr(shape)


def test_the_item_models_accept_unknown_fields():
    """The models say what we REQUIRE, not what Meta may send.

    A required `id` is the dedupe key; everything else is optional because Meta
    changes it without notice. If these models rejected unknown fields, every new
    WhatsApp feature would become a rejected patient message — and note that the
    extractor stores the raw dict either way, so a field missing from the model is
    still stored (assumption A5).
    """
    message = InboundMessage.model_validate(
        text_message(1, referral={"source_type": "ad"}, some_future_key=[1, 2, 3])
    )
    assert message.id == wamid(1)
    assert message.from_ == phone(1)
    assert message.type == "text"

    status = StatusUpdate.model_validate(
        status_update(1, "failed", errors=[{"code": 131047}], pricing={"billable": True})
    )
    assert status.id == wamid(1)
    assert status.status == "failed"
    assert status.recipient_id == phone(1)

    # Only the dedupe fields are required.
    assert InboundMessage.model_validate({"id": wamid(2)}).text is None
    assert StatusUpdate.model_validate({"id": wamid(2), "status": "sent"}).timestamp is None


def test_a_non_text_message_type_is_accepted_with_its_media_keys_intact():
    """VS-008 arrives as one of these, and must already be in webhook_inbox by
    the time anyone writes it. `text` is optional precisely so an audio note is
    not a validation failure."""
    audio = {
        "from": phone(1),
        "id": wamid(5),
        "timestamp": "1730000000",
        "type": "audio",
        "audio": {"id": "media-id-0001", "mime_type": "audio/ogg; codecs=opus", "voice": True},
    }

    item = extract_inbox_items(envelope(messages=[audio]))[0]

    assert item.provider_event_id == f"msg:{wamid(5)}"
    assert item.payload["item"]["type"] == "audio"
    assert item.payload["item"]["audio"]["id"] == "media-id-0001"


def test_an_item_without_an_id_is_stored_under_a_content_hash():
    """Assumption A1.

    An item that fails its model still has to be stored: skipping it would lose a
    patient message with nothing but a WARNING to show for it. The key is a hash
    of the item so hard rule 2 still holds — see the next test.
    """
    nameless = text_message(1)
    del nameless["id"]
    statusless = {"id": wamid(3), "recipient_id": phone(3)}
    body = envelope(messages=[nameless, text_message(2)], statuses=[statusless])

    items = extract_inbox_items(body)

    assert [i.provider_event_id for i in items] == [
        f"msg:sha256:{content_hash(nameless)}",
        f"msg:{wamid(2)}",
        f"status:sha256:{content_hash(statusless)}",
    ]
    # Stored raw and whole, exactly like an item that had an id.
    assert items[0].payload["item"] == nameless
    assert items[0].kind is InboxItemKind.MESSAGE
    assert items[2].kind is InboxItemKind.STATUS


def test_an_identical_redelivery_of_an_id_less_item_hashes_the_same():
    """Hard rule 2 for the fallback key.

    The hash is over canonical JSON — sorted keys, no whitespace — so Meta
    reordering the object between deliveries still produces one row. A genuinely
    different item must not collide with it.
    """
    nameless = text_message(1)
    del nameless["id"]
    reordered = dict(reversed(list(nameless.items())))
    assert list(reordered) != list(nameless)

    assert content_hash(reordered) == content_hash(nameless)

    different = dict(nameless, timestamp="1730000099")
    assert content_hash(different) != content_hash(nameless)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/channels/test_payloads.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.channels.whatsapp.payloads'`.

- [ ] **Step 3: Write `app/channels/whatsapp/payloads.py`**

```python
"""Meta webhook payload models, and the split into things we store.

Two properties, deliberately in tension:

Tolerant. Meta adds fields and sends field types nobody subscribed to. A model
that rejected them would turn every schema change on Meta's side into lost
patient messages, so every model rejects nothing, every list defaults to empty,
and extract_inbox_items() never raises. "We do not understand this" becomes an
empty list, which the endpoint turns into a plain 200.

Lossless. The models locate and validate items; they do NOT define what gets
stored. model_dump() of a narrowed model would delete exactly the keys we failed
to predict — text.body's siblings, VS-008's audio.id, a status's pricing and
errors — from the only place VS-004 and VS-008 can read them. Hence
extra="allow" throughout AND a raw dict as the stored payload: the models say
what we require (a message needs an id; a status needs an id and a status), and
the original object is what lands in webhook_inbox.payload.

Nothing is dropped either. An item that fails its model is stored under a
content-hash key rather than skipped (assumption A1), because a skipped item is a
lost patient message with only a WARNING to show for it.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class InboxItemKind(StrEnum):
    MESSAGE = "message"
    STATUS = "status"


@dataclass(frozen=True)
class InboxItem:
    """One thing to store: its dedupe key, its kind, and its self-contained payload."""

    provider_event_id: str
    kind: InboxItemKind
    payload: dict[str, Any]


class _Tolerant(BaseModel):
    # allow, not ignore. See the module docstring: ignore would strip the keys we
    # did not think of, and those are precisely the ones later slices need.
    # Nothing here ever reads an extra key by attribute; we only dump.
    model_config = ConfigDict(extra="allow")


class Metadata(_Tolerant):
    # phone_number_id is how VS-004 resolves the tenant (hard rule 4). Optional
    # here because a missing one must not cost us the message.
    phone_number_id: str | None = None
    display_phone_number: str | None = None


class InboundMessage(_Tolerant):
    """One inbound message, as far as this slice needs to understand it.

    `id` is the only required field: it is the dedupe key (hard rule 2), and it is
    the one thing we cannot do without. Everything else is optional because Meta
    changes message shapes without notice and because this slice interprets
    nothing — `type` is carried for VS-004's routing and `text` for its message
    row, and a voice note (type="audio") must validate here in VS-003 so that
    VS-008 finds it already in webhook_inbox.

    `from_` is aliased: `from` is a Python keyword. The trailing underscore never
    reaches the database — the stored payload is the raw dict.

    min_length=1 on `id`: an empty string would otherwise satisfy `str` and
    produce the key `msg:`, which every id-less message in the world would share.
    An empty id must take the content-hash path instead.
    """

    id: str = Field(min_length=1)
    from_: str | None = Field(default=None, alias="from")
    timestamp: str | None = None
    type: str | None = None
    text: dict[str, Any] | None = None


class StatusUpdate(_Tolerant):
    """One delivery-status callback.

    `id` (the wamid of the message WE sent) and `status` are both required: both
    are part of the dedupe key, because sent/delivered/read for one message are
    three separate events (assumption A8). `errors` is declared so VS-004 can see
    why a send failed without re-deriving the shape. min_length on both key
    fields, for the same reason as InboundMessage.id.
    """

    id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    recipient_id: str | None = None
    timestamp: str | None = None
    errors: list[Any] | None = None


class ChangeValue(_Tolerant):
    messaging_product: str | None = None
    metadata: Metadata | None = None
    # list[Any], not list[dict]: one malformed element must not invalidate the
    # whole change and lose the messages next to it. The extractor filters.
    contacts: list[Any] = Field(default_factory=list)
    messages: list[Any] = Field(default_factory=list)
    statuses: list[Any] = Field(default_factory=list)


class Change(_Tolerant):
    field: str | None = None
    value: ChangeValue | None = None


class Entry(_Tolerant):
    id: str | None = None
    changes: list[Change] = Field(default_factory=list)


class MetaWebhookEnvelope(_Tolerant):
    object: str | None = None
    entry: list[Entry] = Field(default_factory=list)


def extract_inbox_items(payload: Any) -> list[InboxItem]:
    """Split one webhook body into one InboxItem per message and per status.

    Never raises, and never logs content (hard rule 8): the most it says about an
    unusable payload is its shape.

    Returning [] means "nothing here to store", which the caller turns into a 200
    with no rows — never a 500. Meta would retry a 500 forever for a payload that
    will never change.
    """
    if not isinstance(payload, dict):
        logger.warning("whatsapp webhook: body is not a JSON object")
        return []
    try:
        envelope = MetaWebhookEnvelope.model_validate(payload)
    except ValidationError:
        # Only a structurally broken envelope reaches here (entry not a list of
        # objects). Unknown *fields* never do — that is what extra="allow" and
        # the list[Any] leaves are for.
        logger.warning("whatsapp webhook: unrecognised envelope shape")
        return []

    items: list[InboxItem] = []
    for entry in envelope.entry:
        for change in entry.changes:
            if change.value is None:
                continue
            items.extend(_items_from_change(envelope.object, entry.id, change))
    return items


def _items_from_change(
    obj: str | None, entry_id: str | None, change: Change
) -> list[InboxItem]:
    value = change.value
    if value is None:
        return []
    # Dumped once, so the stored metadata keeps any key Meta added to it.
    raw_metadata = value.model_dump().get("metadata")
    contacts = [c for c in value.contacts if isinstance(c, dict)]
    envelope_fields: dict[str, Any] = {
        "object": obj,
        "entry_id": entry_id,
        "field": change.field,
        "metadata": raw_metadata,
    }

    items: list[InboxItem] = []
    for message in value.messages:
        key = _message_key(message)
        if key is None:
            continue
        items.append(
            InboxItem(
                provider_event_id=key,
                kind=InboxItemKind.MESSAGE,
                payload={
                    "kind": InboxItemKind.MESSAGE.value,
                    **envelope_fields,
                    # The profile name lives here, not on the message. VS-004
                    # matches it to the message by wa_id.
                    "contacts": contacts,
                    # The RAW dict, not the validated model: see assumption A5.
                    "item": message,
                },
            )
        )
    for status in value.statuses:
        key = _status_key(status)
        if key is None:
            continue
        items.append(
            InboxItem(
                provider_event_id=key,
                kind=InboxItemKind.STATUS,
                payload={
                    "kind": InboxItemKind.STATUS.value,
                    **envelope_fields,
                    "item": status,
                },
            )
        )
    return items


def content_hash(item: dict[str, Any]) -> str:
    """sha256 over the item's CANONICAL JSON.

    sort_keys plus the tight separators mean two byte-different renderings of the
    same object hash identically, so a redelivery that reordered keys or changed
    spacing still deduplicates (hard rule 2). default=str so an unexpected
    non-JSON value cannot raise on the fallback path — the fallback exists to stop
    us losing data, and it must not be the thing that loses it.
    """
    canonical = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _message_key(item: Any) -> str | None:
    """`msg:<wamid>`, or `msg:sha256:<hex>` when the item fails its model.

    Assumption A1: an item we cannot read is still stored. The hash key keeps
    hard rule 2 true for it — an identical redelivery hashes identically and the
    unique constraint collapses the two.

    None only for something that is not a JSON object at all, which carries
    nothing to store. The log names the kind; the item is patient content.
    """
    if not isinstance(item, dict):
        logger.warning("whatsapp webhook: skipping non-object item kind=%s", InboxItemKind.MESSAGE)
        return None
    try:
        return f"msg:{InboundMessage.model_validate(item).id}"
    except ValidationError:
        logger.warning("whatsapp webhook: message item has no usable id, keying by content hash")
        return f"msg:sha256:{content_hash(item)}"


def _status_key(item: Any) -> str | None:
    """`status:<wamid>:<status>`, or `status:sha256:<hex>` on a model failure.

    The status word is part of the key: sent, delivered and read for one message
    are three events, while the same status delivered twice is one (A8).
    """
    if not isinstance(item, dict):
        logger.warning("whatsapp webhook: skipping non-object item kind=%s", InboxItemKind.STATUS)
        return None
    try:
        status = StatusUpdate.model_validate(item)
    except ValidationError:
        logger.warning(
            "whatsapp webhook: status item has no usable id or status, keying by content hash"
        )
        return f"status:sha256:{content_hash(item)}"
    return f"status:{status.id}:{status.status}"
```

- [ ] **Step 4: Run the tests, lint, format**

```bash
uv run pytest tests/channels -v && uv run pytest -q
uv run ruff check . && uv run ruff format .
```

Expected: 23 passed in `tests/channels` (10 signature + 13 payload), then `69 passed, 34 skipped`.

- [ ] **Step 5: Checkpoint with the developer**

---

### Task 4: The two endpoints

The handshake, the POST, the failure codes and the logging discipline — everything except the rows in the database, which Task 5 proves.

**Files:**
- Create: `app/api/whatsapp.py`
- Modify: `app/main.py` (include the router)
- Create: `tests/api/conftest.py`
- Test: `tests/api/test_whatsapp_verify.py` (7), `tests/api/test_whatsapp_webhook.py` (8), `tests/api/test_webhook_logging.py` (2), `tests/api/test_route_exposure.py` (+2)

**Interfaces:**
- Consumes: `verify_signature`, `extract_inbox_items`, `WebhookInboxRepository`, `get_session`, `get_settings`.
- Produces:
  - `app.api.whatsapp.router` — `GET`/`POST /webhooks/whatsapp`
  - `app.api.whatsapp.verify(...)`, `app.api.whatsapp.receive(...)`, `app.api.whatsapp._matches(...)`
  - `tests/api/conftest.py`: `meta_settings()`, `configure`, `no_database`, `use_database`, and the re-exported database fixtures. **No `raise_app_exceptions=False` client:** the endpoint answers 503 by raising `HTTPException`, so every test uses the normal client, and an exception escaping the handler fails the test instead of being quietly rendered as a 500.

**Expected tests after this task: 122 — 88 passed, 34 skipped with no Postgres.**

- [ ] **Step 1: Write the API test fixtures**

Create `tests/api/conftest.py`:

```python
"""Fixtures for the webhook endpoints.

The database fixtures are re-exported from tests/db/conftest.py rather than
promoted to tests/conftest.py: promoting them would make every test in the suite
import Alembic and asyncpg for the sake of two modules.
"""

from typing import Any

import pytest

from app.config import Settings, get_settings
from app.db.session import get_session
from tests.db.conftest import (  # noqa: F401  (re-exported fixtures)
    db_engine,
    db_session,
    migrated_database,
    test_database_url,
)
from tests.whatsapp_factories import APP_SECRET, VERIFY_TOKEN


def meta_settings(**overrides: Any) -> Settings:
    """Settings with fake Meta credentials. Never the developer's real ones."""
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": "postgresql+asyncpg://user:pw@localhost:5432/doctoleb",
        "redis_url": "redis://localhost:6379/0",
        "meta_app_secret": APP_SECRET,
        "meta_verify_token": VERIFY_TOKEN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def configure(app):
    """Point the app's settings dependency at fake Meta credentials.

    get_settings is used through Depends() in the handlers precisely so a test can
    replace it without touching the process environment or the lru_cache. Call
    the returned function again to change a value.
    """

    def apply(**overrides: Any) -> Settings:
        settings = meta_settings(**overrides)
        app.dependency_overrides[get_settings] = lambda: settings
        return settings

    apply()
    return apply


class ExplodingSession:
    """A session that fails the test if the endpoint touches the database.

    Used to prove the paths that must answer before any I/O: a rejected
    signature, and a payload with nothing to store.
    """

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the endpoint reached the database")

    async def commit(self) -> None:
        raise AssertionError("the endpoint committed")

    async def rollback(self) -> None:
        raise AssertionError("the endpoint rolled back")


@pytest.fixture
def no_database(app):
    """Override get_session with a session that must never be used."""

    async def override():
        yield ExplodingSession()

    app.dependency_overrides[get_session] = override


@pytest.fixture
def use_database(app, db_session):  # noqa: F811  (db_session is the re-exported fixture)
    """Run the endpoint against the rolled-back test-database session.

    The endpoint commits; db_session's join_transaction_mode="create_savepoint"
    makes that real for the session and still invisible to the next test.
    """

    async def override():
        yield db_session

    app.dependency_overrides[get_session] = override
    return db_session
```

There is deliberately **no** `raise_app_exceptions=False` client fixture here. The storage-failure path answers 503 by raising `HTTPException`, which is an ordinary response — so the normal `client` fixture can assert it, and if a raw exception ever escapes `receive()` instead, `ASGITransport`'s default re-raise makes the test fail loudly rather than showing a plausible 500. That behaviour is itself part of what Task 5 asserts.

- [ ] **Step 2: Write the failing handshake tests**

Create `tests/api/test_whatsapp_verify.py`:

```python
"""GET /webhooks/whatsapp — Meta's subscription handshake.

Meta calls this once, from the dashboard, when the callback URL is saved. It
sends a challenge and expects it back as plain text; anything else, including a
JSON-wrapped version of the right value, fails verification.
"""

from app.config import get_settings
from tests.whatsapp_factories import VERIFY_TOKEN

PATH = "/webhooks/whatsapp"
CHALLENGE = "1158201444"


def _query(mode: str = "subscribe", token: str = VERIFY_TOKEN, challenge: str = CHALLENGE):
    return {"hub.mode": mode, "hub.verify_token": token, "hub.challenge": challenge}


async def test_the_handshake_returns_the_challenge_as_plain_text(client, configure):
    response = await client.get(PATH, params=_query())

    assert response.status_code == 200
    assert response.text == CHALLENGE
    assert response.headers["content-type"].startswith("text/plain")


async def test_the_challenge_is_not_wrapped_in_json(client, configure):
    # A JSON body would be `"1158201444"` — quoted — and Meta's string comparison
    # would fail with no useful error anywhere.
    response = await client.get(PATH, params=_query())

    assert '"' not in response.text


async def test_a_wrong_verify_token_is_forbidden(client, configure):
    response = await client.get(PATH, params=_query(token="not-the-token"))

    assert response.status_code == 403
    assert CHALLENGE not in response.text


async def test_a_mode_other_than_subscribe_is_forbidden(client, configure):
    response = await client.get(PATH, params=_query(mode="unsubscribe"))

    assert response.status_code == 403


async def test_missing_query_parameters_are_forbidden_not_unprocessable(client, configure):
    # Declared optional on purpose: required parameters would answer 422 with a
    # field-by-field description of our API to an unauthenticated caller, and an
    # incomplete handshake is a failed handshake either way.
    for params in (
        {},
        {"hub.mode": "subscribe"},
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN},
    ):
        response = await client.get(PATH, params=params)
        assert response.status_code == 403, params


async def test_an_unset_verify_token_forbids_even_an_empty_token(client, configure):
    """Review Focus 2.

    compare_digest("", "") is True. Without an explicit guard, an app deployed
    with META_VERIFY_TOKEN unset would hand the handshake to anyone who sent
    hub.verify_token= — and then accept their webhook configuration.
    """
    configure(meta_verify_token="")

    for token in ("", VERIFY_TOKEN, "anything"):
        response = await client.get(PATH, params=_query(token=token))
        assert response.status_code == 403, token


async def test_the_verify_token_comes_from_the_environment(client, monkeypatch):
    """The dependency-override tests above would pass even if the handler read a
    hardcoded constant. This one proves the wiring from Settings."""
    monkeypatch.setenv("META_VERIFY_TOKEN", "token-from-the-environment")
    get_settings.cache_clear()
    try:
        response = await client.get(PATH, params=_query(token="token-from-the-environment"))
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.text == CHALLENGE
```

- [ ] **Step 3: Write the failing POST tests**

Create `tests/api/test_whatsapp_webhook.py`:

```python
"""POST /webhooks/whatsapp — everything that happens before a row exists."""

from tests.whatsapp_factories import APP_SECRET, digest, envelope, signed, text_message, to_bytes

PATH = "/webhooks/whatsapp"


async def test_a_missing_signature_header_is_unauthorised(client, configure, no_database):
    raw = to_bytes(envelope(messages=[text_message()]))

    response = await client.post(PATH, content=raw)

    assert response.status_code == 401


async def test_an_invalid_signature_is_unauthorised(client, configure, no_database):
    raw = to_bytes(envelope(messages=[text_message()]))
    headers = {"X-Hub-Signature-256": f"sha256={digest(raw, 'someone-elses-secret')}"}

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 401


async def test_an_unset_app_secret_rejects_a_correctly_signed_body(client, configure, no_database):
    """Review Focus 2, at the endpoint rather than the function."""
    configure(meta_app_secret="")
    raw, headers = signed(envelope(messages=[text_message()]), secret="")

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 401


async def test_the_signature_is_checked_before_the_body_is_parsed(client, configure, no_database):
    """Review Focus 1.

    401, not 400 and not 422: an unauthenticated request must be rejected before
    anything looks at its body. A 400 here would mean we parsed a stranger's
    input first; a 422 would mean FastAPI did.
    """
    response = await client.post(
        PATH,
        content=b"{not json at all",
        headers={"X-Hub-Signature-256": "sha256=deadbeef"},
    )

    assert response.status_code == 401


async def test_a_malformed_json_body_with_a_valid_signature_is_a_bad_request(
    client, configure, no_database
):
    # Assumption A4: someone holding our app secret sent a broken body. Retrying
    # will not fix it, so do not ask Meta to retry — and do not pretend it was
    # stored either.
    raw, headers = signed(b"{not json at all")

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 400


async def test_a_payload_shape_we_do_not_model_returns_200_not_422(client, configure, no_database):
    """Review Focus 5.

    A 422 here would prove the handler declares a pydantic body parameter, which
    would mean FastAPI parsed and validated the body before the signature was
    checked. A 500 would make Meta retry a payload that will never change.
    """
    raw, headers = signed({"object": "whatsapp_business_account", "entry": [{"changes": []}]})

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 200


async def test_an_unmodelled_payload_never_touches_the_database(client, configure, no_database):
    # `no_database` is a session that raises if it is used at all: proof, not
    # inference, that nothing in this path opens a transaction.
    body = envelope(field="account_update")
    body["entry"][0]["changes"][0]["value"] = {"event": "VERIFIED"}
    raw, headers = signed(body)

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 200


async def test_the_endpoint_accepts_the_exact_bytes_meta_sent(client, configure, no_database):
    """Review Focus 1, from the other side.

    A body json.dumps() would never produce — different spacing — must still
    verify, because the digest is over what arrived.
    """
    raw = b'{"object": "whatsapp_business_account" ,  "entry" : [ ]  }'
    headers = {"X-Hub-Signature-256": f"sha256={digest(raw, APP_SECRET)}"}

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 200
```

- [ ] **Step 4: Write the failing logging tests**

Create `tests/api/test_webhook_logging.py`:

```python
"""Hard rule 8 at the endpoint: log identifiers, never content.

This is the first slice that handles a real patient's words, and the tempting log
line — "received: <body>" — breaks the rule on day one. The failure paths are the
sneaky ones: a 400 that echoes the body it could not parse, or a logger.exception
that carries the statement's parameters.
"""

import logging

from tests.whatsapp_factories import (
    PATIENT_TEXT,
    PROFILE_NAME,
    envelope,
    phone,
    signed,
    text_message,
)

PATH = "/webhooks/whatsapp"


def assert_no_patient_content(caplog):
    """The four things that identify a person in a WhatsApp payload."""
    assert PATIENT_TEXT not in caplog.text
    assert PROFILE_NAME not in caplog.text
    assert phone(1) not in caplog.text
    assert "messaging_product" not in caplog.text  # i.e. no raw body anywhere


async def test_a_rejected_signature_logs_no_body(client, configure, no_database, caplog):
    raw, _ = signed(envelope(messages=[text_message(1)]))

    with caplog.at_level(logging.INFO):
        response = await client.post(
            PATH, content=raw, headers={"X-Hub-Signature-256": "sha256=00"}
        )

    assert response.status_code == 401
    assert_no_patient_content(caplog)


async def test_an_unparseable_body_logs_no_body(client, configure, no_database, caplog):
    # The 400 response and its log line must not echo what could not be parsed.
    broken = b'{"messaging_product": "whatsapp", "text": "' + PATIENT_TEXT.encode() + b'"'
    raw, headers = signed(broken)

    with caplog.at_level(logging.INFO):
        response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 400
    assert_no_patient_content(caplog)
    assert PATIENT_TEXT not in response.text
```

- [ ] **Step 5: Extend the route-inventory tests**

Append to `tests/api/test_route_exposure.py`:

```python
def test_the_app_exposes_only_the_expected_paths():
    """Review Focus 9.

    An inventory, not a spot check: the tunnel makes every route public, so a
    future slice adding an unauthenticated endpoint should fail here rather than
    be discovered from the outside.
    """
    app = _app()
    paths = {route.path for route in app.routes if getattr(route, "path", "").startswith("/")}

    assert paths == {"/health", "/health/ready", "/webhooks/whatsapp"}


def test_the_webhook_answers_both_methods_meta_uses():
    app = _app()
    methods = {
        method
        for route in app.routes
        if getattr(route, "path", None) == "/webhooks/whatsapp"
        for method in route.methods
    }

    # GET is the one-time handshake, POST is every delivery. Nothing else.
    assert methods == {"GET", "POST"}
```

If FastAPI's own default routes (`/openapi.json` and friends) appear in `app.routes` despite being disabled here, narrow the comprehension — never widen the expected set.

- [ ] **Step 6: Run the tests to verify they fail**

```bash
uv run pytest tests/api -v
```

Expected: 404 everywhere — `/webhooks/whatsapp` does not exist yet.

- [ ] **Step 7: Write `app/api/whatsapp.py`**

```python
"""The WhatsApp webhook.

Hard rule 1: this endpoint verifies, deduplicates, stores and returns 200. That
is all. No OpenAI call, no Meta call, no media download, no tenant resolution,
no reply — and no enqueue yet: VS-004 adds it at the seam marked in receive().

Hard rule 8: every log line here carries identifiers and counts. A wamid is an
opaque Meta identifier, not patient content; a message body, a profile name and
a phone number are, and none of them may appear in a log, an exception, or a
response body.
"""

import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.whatsapp.payloads import extract_inbox_items
from app.channels.whatsapp.signature import SIGNATURE_HEADER, verify_signature
from app.config import Settings, get_settings
from app.db.repositories import WebhookInboxRepository
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _matches(candidate: str, expected: str) -> bool:
    """Constant-time comparison of two secrets.

    Compared as bytes: hmac.compare_digest raises TypeError on a non-ASCII str,
    which a hostile query string supplies for free.
    """
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _forbidden() -> PlainTextResponse:
    return PlainTextResponse("forbidden", status_code=status.HTTP_403_FORBIDDEN)


@router.get("/whatsapp", response_class=PlainTextResponse)
async def verify(
    settings: Annotated[Settings, Depends(get_settings)],
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> PlainTextResponse:
    """Meta's subscription handshake, called once when the callback URL is saved.

    Returns hub.challenge as plain text, byte for byte. A JSON body would be the
    right value in quotes, and Meta's comparison would fail with no error
    anywhere that explains why.

    Every parameter is optional so that a missing one answers 403 rather than a
    422 describing our API to an unauthenticated caller.
    """
    expected = settings.meta_verify_token
    if not expected:
        # Review Focus 2: compare_digest("", "") is True, so an unset token would
        # otherwise authenticate anyone who sent an empty one.
        logger.warning("whatsapp handshake rejected: META_VERIFY_TOKEN is not set")
        return _forbidden()
    if hub_mode != "subscribe" or not hub_verify_token or not hub_challenge:
        logger.warning("whatsapp handshake rejected: incomplete request")
        return _forbidden()
    if not _matches(hub_verify_token, expected):
        logger.warning("whatsapp handshake rejected: verify token mismatch")
        return _forbidden()

    logger.info("whatsapp handshake verified")
    return PlainTextResponse(hub_challenge)


@router.post("/whatsapp", status_code=status.HTTP_200_OK)
async def receive(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
) -> dict[str, str]:
    """Verify, split, store, 200.

    Note what this signature does NOT contain: a pydantic body model. FastAPI
    would read and validate the body before this function ran — untrusted input
    parsed before authentication, and 422 answered to a request we never
    authenticated. The raw bytes are read here, by us, first.
    """
    raw_body = await request.body()

    if not verify_signature(raw_body, signature, settings.meta_app_secret):
        # No detail about which check failed, and never the header value: a
        # rejected caller learns "no" and nothing else.
        logger.warning("whatsapp webhook rejected: signature missing or invalid")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        # Assumption A4. Not 503: a retry cannot fix a body that is not JSON.
        # Neither the log nor the response echoes the body (hard rule 8).
        logger.warning("whatsapp webhook rejected: body is not valid JSON")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="malformed body"
        ) from None

    items = extract_inbox_items(payload)
    if not items:
        # A field type nobody subscribed to, or a shape Meta added. 200 with
        # nothing stored is the honest answer, and it stops Meta retrying
        # something that can never succeed.
        logger.info("whatsapp webhook accepted with no events to store")
        return {"status": "ok"}

    inbox = WebhookInboxRepository(session)
    new_event_ids: list[str] = []
    try:
        for item in items:
            # store_if_new is INSERT ... ON CONFLICT DO NOTHING (hard rule 2):
            # the database resolves the race between two deliveries, so there is
            # no SELECT-then-INSERT window and no IntegrityError to catch.
            row = await inbox.store_if_new(item.provider_event_id, item.payload)
            if row is not None:
                new_event_ids.append(item.provider_event_id)
        # One commit for the whole delivery: either every event in this request
        # is durable, or none is.
        await session.commit()
    except Exception as error:
        await session.rollback()
        # The exception CLASS NAME only, and nothing else, ever.
        #
        # hide_parameters=True on the engine keeps SQLAlchemy from appending the
        # bound parameters, but it does not touch what PostgreSQL itself puts in
        # the message: the CONTEXT line on an invalid jsonb value quotes a snippet
        # of the JSON, and here that JSON is a patient's message (hard rule 8).
        logger.error("whatsapp webhook storage failed error=%s", type(error).__name__)
        # 503, not a re-raise. Re-raising would answer 500 AND hand uvicorn's
        # exception logger the full traceback, message included — which is the
        # leak above, written to the log by a component we do not control.
        # `from None` clears __cause__ and __context__, so no formatter anywhere
        # can walk back to the original exception.
        #
        # Still a retryable status: Meta treats 503 like 500 and redelivers, and
        # dedupe makes the retry safe. Answering 200 here would lose the message
        # permanently to a transient database outage.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage unavailable"
        ) from None

    logger.info(
        "whatsapp webhook stored events=%d new=%d ids=%s",
        len(items),
        len(new_event_ids),
        ",".join(new_event_ids),
    )

    # --- VS-004 seam -------------------------------------------------------
    # VS-004 enqueues one job per id in `new_event_ids`, right here: after the
    # commit (a job must never see a row that is not committed yet) and before
    # the return. Nothing above this line changes, and duplicates are already
    # filtered out — `new_event_ids` holds only rows this request created.
    # ----------------------------------------------------------------------
    return {"status": "ok"}
```

- [ ] **Step 8: Include the router in `app/main.py`**

```python
from app.api.whatsapp import router as whatsapp_router
...
    app.include_router(health_router)
    app.include_router(whatsapp_router)
```

- [ ] **Step 9: Run the tests, lint, format**

```bash
uv run pytest tests/api -v && uv run pytest -q
uv run ruff check . && uv run ruff format .
```

Expected: `88 passed, 34 skipped`.

- [ ] **Step 10: Checkpoint with the developer**

---

### Task 5: Rows in the database

The slice's real acceptance criteria, against a real PostgreSQL: one row per item, one row after a duplicate delivery, all-or-nothing per request, 503 when storage fails, and no patient content in the log line that records success.

**Files:**
- Test: `tests/api/test_whatsapp_inbox.py` (8 new, all `@pytest.mark.db`)
- Test: `tests/api/test_webhook_logging.py` (+1, `@pytest.mark.db`)

No application code is expected to change in this task. If a test here fails, the fix belongs in Task 4's module.

**Expected tests after this task: 131 — 88 passed, 43 skipped with no Postgres; 131 passed with it.**

- [ ] **Step 1: Write the storage tests**

Create `tests/api/test_whatsapp_inbox.py`:

```python
"""The rows. Needs a real PostgreSQL: a unique constraint cannot be mocked.

Every test drives the endpoint over HTTP and then reads webhook_inbox through the
same session, so what is asserted is what a deployed app would have written.
"""

import logging

import pytest
import sqlalchemy as sa

from app.db.models import WebhookInbox
from tests.whatsapp_factories import (
    PATIENT_TEXT,
    PHONE_NUMBER_ID,
    envelope,
    signed,
    status_update,
    text_message,
    wamid,
)

pytestmark = pytest.mark.db

PATH = "/webhooks/whatsapp"


async def _post(client, body) -> int:
    raw, headers = signed(body)
    response = await client.post(PATH, content=raw, headers=headers)
    return response.status_code


async def _event_ids(session) -> list[str]:
    result = await session.scalars(
        sa.select(WebhookInbox.provider_event_id).order_by(WebhookInbox.provider_event_id)
    )
    return list(result.all())


async def test_one_message_becomes_one_inbox_row_with_no_tenant(client, configure, use_database):
    assert await _post(client, envelope(messages=[text_message(1)])) == 200

    row = await use_database.scalar(sa.select(WebhookInbox))
    assert row.provider_event_id == f"msg:{wamid(1)}"
    assert row.provider == "whatsapp"
    assert row.status == "RECEIVED"
    assert row.attempts == 0
    # Hard rule 4: the tenant is resolved by the worker from phone_number_id.
    # Resolving it here would mean doing work inside the webhook (hard rule 1).
    assert row.tenant_id is None


async def test_the_same_post_twice_stores_one_row_per_item(client, configure, use_database):
    """Hard rule 2, and the whole reason a 503 is safe.

    Meta redelivers on its own schedule and after any failure. Two identical
    deliveries must leave one row — enforced by the unique constraint, not by us
    checking first.
    """
    body = envelope(messages=[text_message(1)])

    assert await _post(client, body) == 200
    assert await _post(client, body) == 200

    assert await _event_ids(use_database) == [f"msg:{wamid(1)}"]


async def test_messages_and_statuses_in_one_post_become_one_row_each(
    client, configure, use_database
):
    """Review Focus 3. One row per request would lose four of these five."""
    body = envelope(
        messages=[text_message(1), text_message(2), text_message(3)],
        statuses=[status_update(8, "delivered"), status_update(9, "read")],
    )

    assert await _post(client, body) == 200

    assert await _event_ids(use_database) == sorted(
        [
            f"msg:{wamid(1)}",
            f"msg:{wamid(2)}",
            f"msg:{wamid(3)}",
            f"status:{wamid(8)}:delivered",
            f"status:{wamid(9)}:read",
        ]
    )


async def test_a_post_mixing_a_new_and_a_stored_message_stores_only_the_new_one(
    client, configure, use_database
):
    # Meta's retries are not always byte-identical: a redelivery can carry one
    # event we already have and one we do not. Per-item dedupe keeps both facts.
    assert await _post(client, envelope(messages=[text_message(1)])) == 200
    assert await _post(client, envelope(messages=[text_message(1), text_message(2)])) == 200

    assert await _event_ids(use_database) == [f"msg:{wamid(1)}", f"msg:{wamid(2)}"]


async def test_three_statuses_for_one_message_are_three_rows(client, configure, use_database):
    """Review Focus 6 and assumption A8 in one test.

    sent/delivered/read for one wamid are three events; the same status twice is
    one. Both halves matter to VS-004's message status column.
    """
    assert await _post(client, envelope(statuses=[status_update(1, "sent")])) == 200
    assert await _post(client, envelope(statuses=[status_update(1, "sent")])) == 200
    assert (
        await _post(
            client,
            envelope(statuses=[status_update(1, "delivered"), status_update(1, "read")]),
        )
        == 200
    )

    assert await _event_ids(use_database) == sorted(
        [f"status:{wamid(1)}:{s}" for s in ("sent", "delivered", "read")]
    )


async def test_the_stored_payload_is_what_vs004_needs(client, configure, use_database):
    """Review Focus 7, through JSONB and back.

    The worker reads this row and nothing else. Anything missing here is
    unrecoverable — the request is long gone.
    """
    body = envelope(messages=[text_message(1, context={"id": wamid(7)})])
    assert await _post(client, body) == 200

    row = await use_database.scalar(sa.select(WebhookInbox))
    assert row.payload["kind"] == "message"
    assert row.payload["metadata"]["phone_number_id"] == PHONE_NUMBER_ID
    assert row.payload["item"]["id"] == wamid(1)
    assert row.payload["item"]["text"]["body"] == PATIENT_TEXT
    # An undeclared key survived validation, JSONB and the round trip. VS-008's
    # audio.id arrives the same way.
    assert row.payload["item"]["context"] == {"id": wamid(7)}


async def test_a_storage_failure_answers_503_without_propagating_the_exception(
    client, configure, use_database, monkeypatch, caplog
):
    """Review Focus 4, all three halves of it.

    `except: return 200` would turn a database outage into permanently lost
    patient messages, because a 200 tells Meta to forget the event. Re-raising
    the original error would answer 500 and hand uvicorn a traceback whose
    message can quote the offending data. So: 503, and nothing about the cause
    leaves the process.

    The NORMAL client is the assertion, not a detail. ASGITransport re-raises
    application exceptions by default, so if `receive()` ever let the RuntimeError
    escape, this test would error out with that exception instead of reading a
    response — which is exactly the failure we want to be told about.
    """

    async def boom(self, provider_event_id, payload, provider="whatsapp"):
        # The message imitates a PostgreSQL error quoting the offending value.
        raise RuntimeError(f"invalid input syntax for type json, CONTEXT: {PATIENT_TEXT}")

    monkeypatch.setattr("app.api.whatsapp.WebhookInboxRepository.store_if_new", boom)
    raw, headers = signed(envelope(messages=[text_message(1)]))

    with caplog.at_level(logging.ERROR):
        response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 503
    assert await _event_ids(use_database) == []
    # The class name is in the log; the message the exception carried is not,
    # anywhere — not in the log, not in the response (hard rule 8).
    assert "RuntimeError" in caplog.text
    assert PATIENT_TEXT not in caplog.text
    assert PATIENT_TEXT not in response.text


async def test_every_row_from_one_post_lands_in_one_transaction(
    client, configure, use_database, monkeypatch
):
    """Half a delivery is not a correctness bug — Meta redelivers and dedupe
    absorbs it — but it makes "was this request stored?" unanswerable. One POST
    is one transaction."""
    from app.db.repositories import WebhookInboxRepository

    real = WebhookInboxRepository.store_if_new
    calls: list[str] = []

    async def fail_on_the_second(self, provider_event_id, payload, provider="whatsapp"):
        calls.append(provider_event_id)
        if len(calls) == 2:
            raise RuntimeError("second insert exploded")
        return await real(self, provider_event_id, payload, provider)

    monkeypatch.setattr("app.api.whatsapp.WebhookInboxRepository.store_if_new", fail_on_the_second)
    raw, headers = signed(envelope(messages=[text_message(1), text_message(2)]))

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 503
    assert len(calls) == 2
    # The first insert really happened, and was rolled back with the second.
    assert await _event_ids(use_database) == []
```

- [ ] **Step 2: Add the database-backed logging test**

Append to `tests/api/test_webhook_logging.py` (adding `pytest`, `status_update` and `wamid` to its imports):

```python
@pytest.mark.db
async def test_a_stored_webhook_logs_event_ids_and_never_content(
    client, configure, use_database, caplog
):
    """Hard rule 8 on the path that actually carries a patient's words.

    Logging the ids is the point — without them a production incident has nothing
    to correlate. Logging the body is the violation.
    """
    raw, headers = signed(
        envelope(messages=[text_message(1)], statuses=[status_update(2, "read")])
    )

    with caplog.at_level(logging.INFO):
        response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 200
    assert f"msg:{wamid(1)}" in caplog.text
    assert f"status:{wamid(2)}:read" in caplog.text
    assert_no_patient_content(caplog)
```

- [ ] **Step 3: Run the tests both ways, lint, format**

```bash
docker compose up -d postgres
uv run pytest tests/api -v
uv run pytest -q                                          # 131 passed
docker compose stop postgres && uv run pytest -q && docker compose start postgres
uv run ruff check . && uv run ruff format .
```

Expected: `131 passed`, then `88 passed, 43 skipped`. If anything *fails* rather than skips in the second run, a database test escaped the `db` marker.

- [ ] **Step 4: Checkpoint with the developer**

---

### Task 6: The tunnel, the README, and the local smoke test

Everything a developer needs to point Meta at a laptop — and to exercise the endpoint end to end *before* Meta exists.

**Files:**
- Create: `scripts/sign_webhook.py`
- Modify: `README.md`
- Modify: `docs/slices/VS-003.md` (Notes, Follow-ups; Status becomes IN PROGRESS, not DONE)
- Modify: `docs/slices/README.md` (status table)

- [ ] **Step 1: Write the local signing helper**

`scripts/sign_webhook.py`. Without it, testing the real endpoint by hand means computing an HMAC by hand, and the usual next step is disabling verification "just to check".

```python
"""POST a synthetic WhatsApp webhook to a local instance, correctly signed.

    export META_APP_SECRET=...              # bash
    $env:META_APP_SECRET = "..."            # PowerShell
    uv run python scripts/sign_webhook.py "hello from the test phone"

Hard rule 9: the secret comes from the environment, never from an argument (a
command line is visible to other processes and lands in shell history).
Hard rule 8: the text is whatever you pass; keep it synthetic, and never paste a
real patient message here.
"""

import hashlib
import hmac
import json
import os
import sys
import urllib.request

URL = os.environ.get("WEBHOOK_URL", "http://localhost:8000/webhooks/whatsapp")


def main() -> int:
    secret = os.environ.get("META_APP_SECRET", "")
    if not secret:
        print("META_APP_SECRET is not set; the endpoint would answer 401", file=sys.stderr)
        return 2

    text = sys.argv[1] if len(sys.argv) > 1 else "hello from scripts/sign_webhook.py"
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "200000000000002",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "96170999999",
                                "phone_number_id": os.environ.get(
                                    "META_PHONE_NUMBER_ID", "100000000000001"
                                ),
                            },
                            "contacts": [
                                {"profile": {"name": "Local Test"}, "wa_id": "96170000001"}
                            ],
                            "messages": [
                                {
                                    "from": "96170000001",
                                    # New on every run: reuse the same id and the
                                    # second run is correctly deduplicated and
                                    # stores nothing, which looks like a bug.
                                    "id": f"wamid.LOCAL{os.urandom(4).hex()}",
                                    "timestamp": "1730000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    raw = json.dumps(body).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        URL,
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310  (a localhost URL we built)
        print(response.status, response.read().decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Run it and confirm a row appears:

```bash
docker compose up -d
docker compose exec api alembic upgrade head
uv run python scripts/sign_webhook.py "local smoke test"
docker compose exec postgres psql -U doctoleb -d doctoleb -c "select provider_event_id, status, tenant_id, created_at from webhook_inbox order by created_at desc limit 5;"
```

(The `psql` call is one line on purpose: a `\` continuation is a bash-ism, and the developer re-runs these in PowerShell.)

Expected: `200 {"status":"ok"}`, then one `msg:wamid.LOCAL...` row with a null `tenant_id`. **Select ids and status, never `payload`** — that column holds message text, and a terminal transcript is as public as a log (hard rule 8).

- [ ] **Step 2: Add the webhook section to `README.md`**

After "Running the tests":

````markdown
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
````

- [ ] **Step 3: Write the Notes and Follow-ups into `docs/slices/VS-003.md`**

Status becomes `IN PROGRESS` — the first acceptance criterion needs Task 7. Append:

```markdown
## Notes
- One `webhook_inbox` row per ITEM, not per request: `msg:<wamid>` for messages,
  `status:<wamid>:<status>` for statuses. A request has no identity of its own,
  and one POST routinely carries several messages and statuses. The prefixes
  exist because a message and its own `sent` callback share one wamid.
- An item whose model fails (no `id`, or a status with no `status`) is NOT
  skipped: it is stored under `msg:sha256:<hex>` / `status:sha256:<hex>`, hashed
  over canonical JSON (`sort_keys=True, separators=(",", ":")`) so an identical
  redelivery still deduplicates. Skipping would lose a patient message with
  nothing but a WARNING to show for it. Only a non-object item is dropped.
- `InboundMessage` and `StatusUpdate` validate each item; they do not define what
  is stored. A message requires only `id`, a status `id` and `status` — the
  dedupe keys — with `min_length=1` so an empty value takes the hash path
  instead of producing the shared key `msg:`. `type` and `text` are optional
  because VS-008's audio note must validate here, in VS-003, to be in the inbox
  at all. `from_` is aliased (`from` is a keyword) and never reaches the
  database.
- `payload["kind"]` is an explicit field. VS-004 must read it rather than
  string-splitting `provider_event_id`; the key format is ours and may change.
- Every payload model uses `extra="allow"`, and the stored payload is the raw
  JSON rather than `model_dump()` of a narrowed model. With `extra="ignore"`, or
  with the models as the storage source, every key we failed to predict —
  VS-008's `audio.id`, a status's `pricing`, Meta's next addition — would be
  silently deleted from the only place the worker can read.
- `verify_signature` decodes the hex with `bytes.fromhex` instead of comparing
  hex strings: it handles either case for free, rejects junk before the
  comparison, and avoids `compare_digest`'s `TypeError` on a non-ASCII header,
  which a hostile caller supplies for free.
- An empty `META_APP_SECRET` rejects every POST and an empty `META_VERIFY_TOKEN`
  rejects every handshake, by explicit guard. `hmac.new(b"", body)` is a valid
  HMAC and `compare_digest("", "")` is `True`, so without the guards "unset"
  would mean "anyone can forge".
- Both Meta secrets are optional with an empty default, and the app still starts
  without them. Required fields would break `pytest` and `docker compose up` for
  a developer with no Meta app — the exact situation VS-003 was written in. The
  failure moved to request time, where it is total, plus a startup warning that
  names the missing variable (never its value).
- The POST handler takes NO pydantic body parameter. FastAPI would read and
  validate the body before the handler ran: untrusted input parsed before
  authentication, and 422 answered to a request we never authenticated. Proven by
  `test_a_payload_shape_we_do_not_model_returns_200_not_422`.
- Query parameters on the GET are optional so a missing one answers 403 rather
  than a 422 that describes our API to an unauthenticated caller.
- Malformed JSON with a valid signature answers 400, not 503: whoever holds our
  app secret sent a broken body, and a retry cannot fix it.
- A storage failure answers `HTTPException(503) from None`, and the original
  exception is never re-raised. `except: return 200` would turn a transient
  database outage into permanently lost patient messages, because 200 tells Meta
  to forget the event; re-raising would answer 500 AND hand uvicorn's exception
  logger the full traceback, message included. `hide_parameters=True` does not
  help there: PostgreSQL's own `CONTEXT:` line on an invalid `jsonb` value quotes
  a snippet of the JSON, which here is a patient's message (hard rule 8). So the
  log carries `type(error).__name__` only, and `from None` clears `__cause__` so
  nothing can walk back to the original. Meta treats 503 like 500 and retries.
- No test client in this repo uses `raise_app_exceptions=False`. Because the 503
  is a real response, the normal client asserts it, and an exception escaping the
  handler fails the test instead of being rendered as a plausible 500.
- All items from one POST go through one `store_if_new` loop and one `commit()`.
  The loop rather than a multi-row INSERT: VS-002's repository is reused
  unchanged, each item gets its own conflict resolution, and Meta repeating an
  item inside one request needs no special case.
- `tenant_id` stays NULL on every row. `phone_number_id` is preserved inside the
  payload for VS-004 to resolve from (hard rules 1 and 4).
- VS-001's tunnel follow-up is closed here: `create_app()` serves `/docs`,
  `/redoc` and `/openapi.json` only when the new `DOCS_ENABLED` setting is true,
  and it defaults to false. Deliberately NOT gated on `APP_ENV`: the tunnel runs
  while `APP_ENV=development`, so that condition would be open exactly when the
  API is reachable from the internet. `tests/api/test_route_exposure.py` pins the
  exact set of paths the tunnel can reach, and asserts the 404s *under*
  `APP_ENV=development`. `create_app()` now takes an optional `Settings` so a
  test can build a differently-configured app.
- `tests/api/conftest.py` re-exports the database fixtures from
  `tests/db/conftest.py` instead of promoting them to `tests/conftest.py`, which
  would make every test in the suite import Alembic and asyncpg.
- On Windows use `curl.exe`. PowerShell aliases `curl` to `Invoke-WebRequest`,
  which silently misreads `-d` and `-H`.

## Follow-ups
- `/health/ready` is still reachable through the tunnel. It leaks no credential,
  but it is an unauthenticated probe that opens a database and a Redis connection
  per call. Restrict it at the tunnel (a cloudflared named tunnel's ingress rules
  can match on path) or behind a shared secret before any non-dev exposure.
- `await request.body()` reads an unbounded body into memory before the signature
  is checked — it must, since the signature covers the whole body. Add a maximum
  body size (Meta's webhooks are small) before this endpoint is exposed anywhere
  but a dev tunnel.
- No rate limit and no source-IP check on the webhook. Meta publishes its egress
  ranges; an allowlist is a second layer once there is a real deployment.
- **A message containing a NUL character (`\u0000`) makes every delivery of it
  fail.** PostgreSQL's `jsonb` cannot represent `\u0000` and rejects the whole
  value, so the insert raises, the endpoint answers 503, Meta redelivers, and the
  same message fails identically until Meta gives up — a silently lost message
  plus repeated 503s in the log. Not fixed here because both fixes have real
  costs: sanitising edits a patient's message before it is stored (and hides that
  it happened), while a `text`/`bytea` column gives up `jsonb` querying for every
  row to accommodate a rare one. A third option is catching this one case and
  dead-lettering it. Decide before the first real traffic; until then the failure
  is at least loud rather than silent.
- An item keyed by content hash (assumption A1) whose redelivery differs by a
  single field — a changed `timestamp` — lands as a second row. Acceptable while
  the `WARNING` is rare; if it is not rare, dead-letter these instead.
- A `provider_event_id` longer than 255 characters raises from asyncpg and becomes
  a 503 rather than being handled (assumption A2). Truncating would risk two
  wamids colliding into one key, which is worse. Note the hash keys are fixed at
  75 characters, so only a real `wamid` can trip this.
- Only `X-Hub-Signature-256` is accepted; the legacy SHA-1 `X-Hub-Signature` is
  ignored. Deliberate, and one function to extend.
- `webhook_inbox.payload` now really does hold patient text and phone numbers.
  VS-002's retention follow-up is no longer theoretical; pair it with VS-008's
  audio retention setting.
- `DOCS_ENABLED` is a process-wide on/off switch with no authentication. If a
  staging environment ever needs the docs *and* is internet-facing, that wants an
  auth layer in front of them, not a wider default.
- Structured JSON logging with a request id (VS-001 follow-up) is now genuinely
  useful: correlating a rejected signature with a later redelivery is eyeball
  work today.
- `scripts/` is not in the Docker image and not covered by tests. Fine for one dev
  helper; if a second one appears, give it a test.
```

- [ ] **Step 4: Set VS-003 to `IN PROGRESS` in `docs/slices/README.md`**

It becomes `DONE` in Task 7, Step 6.

- [ ] **Step 5: Explain the slice function by function**

`CLAUDE.md` requires it: what each function does, why it exists, which hard rule it protects. Cover at minimum `verify_signature`, `InboundMessage` / `StatusUpdate` (what "required" means and why only those fields are), `extract_inbox_items`, `_items_from_change`, `content_hash`, `_message_key`, `_status_key`, `verify`, `_matches`, `receive` (including why the storage failure becomes a 503 rather than a re-raise), the `create_app(settings=...)` and `DOCS_ENABLED` change, and the `configure` / `no_database` / `use_database` fixtures.

- [ ] **Step 6: Checkpoint with the developer**

---

### Task 7: Live verification against Meta — **BLOCKED**

**Blocked on:** the developer's Meta developer app existing (waiting on Meta's new-account restriction). Nothing in Tasks 1–6 depends on this task, and this task changes no code — if something here fails, the fix is a new step in an earlier task.

**Do not start this task until the developer says the Meta app exists.** Everything below needs real credentials in `.env`.

**Every command in this task is PowerShell**, because the developer runs this task by hand in a PowerShell window: `$env:NAME` for environment variables, `curl.exe` never bare `curl`, and no `\` line continuations — each command is one line, however long.

- [ ] **Step 1: Fill in the real values in `.env`**

`META_APP_SECRET`: App Dashboard → Settings → Basic → App Secret (Show).
`META_VERIFY_TOKEN`: a string you invent — any long random value. It is a shared secret between the dashboard form and `.env`, not something Meta issues.

Leave `DOCS_ENABLED=false` while the tunnel is up.

Restart so the new environment is read, and confirm the startup warnings are gone:

```powershell
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose logs api | Select-String "not set"
```

Expected: no output from the last line. `Select-String` prints nothing when it matches nothing, so silence is the pass.

- [ ] **Step 2: Start the tunnel and keep it running**

```powershell
cloudflared tunnel --url http://localhost:8000
```

Copy the `https://<...>.trycloudflare.com` URL. It changes on every restart; leave this window open for the rest of the task.

- [ ] **Step 3: Verify the callback URL in the Meta dashboard**

WhatsApp → Configuration → Webhook → Edit:

- Callback URL: `https://<tunnel>/webhooks/whatsapp`
- Verify token: exactly the `META_VERIFY_TOKEN` value from `.env`

Click Verify and save. Then:

```powershell
docker compose logs api | Select-String "whatsapp handshake"
```

Expected: the dashboard accepts it, and one `whatsapp handshake verified` line.

If it fails, that same command names which check rejected it — `META_VERIFY_TOKEN is not set`, `incomplete request`, or `verify token mismatch`. No line at all means the request never arrived: the tunnel points at the wrong port, or the api container is down.

- [ ] **Step 4: Subscribe to the `messages` field**

Same page, Webhook fields → subscribe to `messages`. Without this the handshake succeeds and no event ever arrives — the single most common "the webhook doesn't work" cause.

- [ ] **Step 5: Send a real message and confirm exactly one row**

Send `Hello` from your phone to the test number, then (one line):

```powershell
docker compose exec postgres psql -U doctoleb -d doctoleb -c "select provider_event_id, status, tenant_id, created_at from webhook_inbox order by created_at desc limit 10;"
```

Expected: one `msg:wamid...` row, `status` `RECEIVED`, `tenant_id` null.

**Select ids and status only — never `payload`.** It holds the message text, and a terminal transcript is as public as a log (hard rule 8). For the same reason, nothing observed here is copied into a test fixture, the slice file, or a commit message.

Then prove hard rule 2 against the real thing: stop the api container, send a second message while it is down, start it again, and confirm Meta's redelivery leaves exactly one row.

```powershell
docker compose stop api
```

Send the second message now, while the api is down, then:

```powershell
docker compose start api
```

**Meta's redelivery can take minutes.** Its retry schedule backs off and is not published; the first retry is not immediate, and a redelivery arriving 5–15 minutes later is normal. Do not conclude anything from an empty table straight after `start` — that is the expected state. Watch for it instead, and let it run:

```powershell
docker compose logs -f api | Select-String "whatsapp webhook stored"
```

Once the line appears (Ctrl+C to stop following), check for duplicates (one line):

```powershell
docker compose exec postgres psql -U doctoleb -d doctoleb -c "select provider_event_id, count(*) from webhook_inbox group by 1 having count(*) > 1;"
```

Expected: `(0 rows)` — every `provider_event_id` appears exactly once. If the redelivery never arrives at all, that is a Meta-side retry question, not a dedupe result: note it and move on rather than reading it as a pass.

- [ ] **Step 6: Close the slice**

```powershell
docker compose exec api pytest -q
docker compose exec api ruff check .
```

Expected: `131 passed`, ruff clean. Then set `Status: DONE` in `docs/slices/VS-003.md` and `DONE` in `docs/slices/README.md`, and append to the Notes whatever the live run taught — the failure mode that cost the most time is the one worth writing down.

- [ ] **Step 7: Checkpoint with the developer**

---

## Acceptance criteria mapped to tasks

| VS-003 requirement | Where it is built | Where it is proven |
|---|---|---|
| `GET /webhooks/whatsapp` handshake (`hub.mode`, `hub.verify_token`, `hub.challenge`) | Task 4, Step 7 (`verify`) | Task 4, Step 2 (7 tests); Task 7, Step 3 (live) |
| `POST /webhooks/whatsapp` verifies `X-Hub-Signature-256` against the raw body | Task 2, Step 4 (`verify_signature`); Task 4, Step 7 (`receive` reads `request.body()`) | Task 2, Step 2 (10 tests); Task 4, Step 3 (`test_the_signature_is_checked_before_the_body_is_parsed`, `test_the_endpoint_accepts_the_exact_bytes_meta_sent`) |
| Invalid or missing signature → 401 | Task 4, Step 7 | Task 4, Step 3 (`test_a_missing_signature_header_is_unauthorised`, `test_an_invalid_signature_is_unauthorised`, `test_an_unset_app_secret_rejects_a_correctly_signed_body`) |
| Pydantic models for messages + statuses, tolerant of unknown fields | Task 3, Step 3 (`MetaWebhookEnvelope` … `ChangeValue`, plus `InboundMessage` and `StatusUpdate`) | Task 3, Step 1 (`test_the_item_models_accept_unknown_fields`, `test_a_non_text_message_type_is_accepted_with_its_media_keys_intact`, `test_shapes_we_do_not_model_produce_no_items_and_no_exception`, `test_unknown_keys_inside_a_message_survive_the_round_trip`, `test_a_field_we_do_not_handle_produces_no_items`) |
| Nothing is dropped: an item with no usable id is still stored and still deduped | Task 3, Step 3 (`content_hash`, `_message_key`, `_status_key`) | Task 3, Step 1 (`test_an_item_without_an_id_is_stored_under_a_content_hash`, `test_an_identical_redelivery_of_an_id_less_item_hashes_the_same`) |
| Insert into `webhook_inbox` | Task 4, Step 7 (via VS-002's `store_if_new`) | Task 5, Step 1 (`test_one_message_becomes_one_inbox_row_with_no_tenant`) |
| Duplicate → still 200, do nothing | Task 4, Step 7 | Task 5, Step 1 (`test_the_same_post_twice_stores_one_row_per_item`, `test_a_post_mixing_a_new_and_a_stored_message_stores_only_the_new_one`) |
| Status events handled without error | Task 3, Step 3; Task 4, Step 7 | Task 3, Step 1 (`test_a_status_is_keyed_by_wamid_and_status`); Task 5, Step 1 (`test_three_statuses_for_one_message_are_three_rows`) |
| Local public URL documented (cloudflared or ngrok) | Task 6, Step 2 | Task 7, Steps 2–3 (live) |
| Meta dashboard webhook verification succeeds | — | **Task 7, Step 3 — BLOCKED** |
| Message from my phone appears in `webhook_inbox` | — | **Task 7, Step 5 — BLOCKED** |
| Hard rule 1: the webhook only verifies, dedupes, stores, returns 200 | Task 4, Step 7 | Task 4, Step 3 (`test_an_unmodelled_payload_never_touches_the_database`); the VS-004 seam comment; no import of `app.queue` anywhere in `app/api/whatsapp.py` |
| Hard rule 2: one event delivered twice → one row | Task 4, Step 7 (per-item `ON CONFLICT DO NOTHING`) | Task 5, Step 1 (2 tests); Task 7, Step 5 (live, with a real redelivery) |
| Hard rule 4: no tenant resolution in the request | Task 4, Step 7 | Task 5, Step 1 (`tenant_id is None`, and `test_the_stored_payload_is_what_vs004_needs` keeps `phone_number_id` for VS-004) |
| Hard rule 8: no patient content in logs or fixtures | Task 3, Step 3; Task 4, Step 7 | Task 4, Step 4 (2 tests); Task 5, Step 2 (`test_a_stored_webhook_logs_event_ids_and_never_content`); Task 5, Step 1 (failure-path assertion); `tests/whatsapp_factories.py` is synthetic by construction |
| Hard rule 9: secrets from env only, empty → reject | Task 1, Steps 3–5 | Task 1, Step 1 (2 tests); Task 2, Step 2 (`test_an_unset_app_secret_...`); Task 4, Step 2 (`test_an_unset_verify_token_...`, `test_the_verify_token_comes_from_the_environment`) |
| Failure semantics: never 200 unless stored; 503 and no leaked cause when it fails | Task 4, Step 7 | Task 5, Step 1 (`test_a_storage_failure_answers_503_without_propagating_the_exception`, `test_every_row_from_one_post_lands_in_one_transaction`) |
| VS-001 follow-up: the tunnel must not expose `/docs` or `/openapi.json` | Task 1, Steps 3–5 (`DOCS_ENABLED`, default false, independent of `APP_ENV`) | Task 1, Step 1 (`test_docs_are_disabled_by_default_and_are_not_tied_to_app_env` + 3 route tests); Task 4, Step 5 (2 route-inventory tests) |
| `pytest` passes | every task | Task 5, Step 3; Task 7, Step 6 |
| `ruff check` clean | every task | Task 5, Step 3; Task 7, Step 6 |
| Slice file Status and Notes updated | Task 6, Step 3; Task 7, Step 6 | Task 7, Step 7 |
