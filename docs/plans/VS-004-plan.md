# VS-004 Worker + Send a Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Do not stop for the developer between tasks.** After each task, append a short entry to `.superpowers/sdd/VS-004-report.md` (following the shape of `VS-003-report.md`): the task, the test count, and anything surprising — a wrong assumption in this plan, a test that needed rewriting, a decision the plan did not anticipate. Task 10 is the one task that stops, because it needs the developer's phone.

**Goal:** A message stored in `webhook_inbox` by VS-003 is picked up by a background worker, attributed to a tenant, turned into a `contacts` / `conversations` / `messages` row, and answered on WhatsApp with `Received ✅` — exactly once, even when Meta delivers the webhook twice, the job runs twice, or the process dies halfway through.

**Architecture:** The webhook gains one statement at the VS-003 seam: after the commit, it enqueues one job per extracted item through a `JobQueue` interface with an arq implementation. The job argument is the `webhook_inbox` row's **UUID primary key** and nothing else — Redis never holds a payload, a phone number, a message, or a Meta-controlled identifier. The worker loads the row from Postgres, claims it with a conditional `UPDATE` that takes a time-limited lease (so neither a re-run nor a concurrent run can duplicate the work), reads `payload["kind"]`, resolves the tenant from `metadata.phone_number_id` through a resolver interface, and dispatches to a message handler or a status handler. The message handler reserves an outgoing `messages` row — `QUEUED`, linked to the inbound message by a new `reply_to_message_id` column with a unique constraint — **commits**, makes exactly one Meta send attempt, and then commits the returned `wamid` onto that row. Retries, backoff and dead-lettering live in the job envelope; the Meta client itself never retries, it only classifies.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.1.1 (async) + asyncpg, PostgreSQL 16, Redis 7 + arq 0.26, **httpx (promoted from a dev dependency to a runtime one)**, pytest + pytest-asyncio with `httpx.MockTransport` for Meta, ruff, Docker Compose.

**Spec:** `docs/slices/VS-004.md` (scope and acceptance) plus the developer's written requirements for this slice, with `CLAUDE.md` (hard rules) and `docs/architecture.md` (flow and ownership) as binding context. `docs/slices/VS-003.md` Notes and Follow-ups carry the facts this slice builds on; they are triaged below. **VS-003 is merged to `main`** — nothing in this slice changes its status, and changing its tested behaviour (its log lines, its dedupe key) is a separate piece of work, not a VS-004 task.

**Sequencing constraint:** Tasks 1–9 are fully testable with fake credentials, synthetic payloads and a mocked Meta transport, today, and none of them stops for the developer. **Task 10 is the live test with the developer's phone, it is the only task that stops, and it may leave the slice PARTIAL.** The Meta app exists, the handshake was verified and the `messages` field is subscribed — but Meta still never POSTs a real message to our callback, and `hello_world` never arrived either. Task 10 is therefore a diagnosis before it is a test, and its first three steps are the two most likely causes (an app not subscribed to the WABA; a phone not on the test number's allowed recipient list) plus a stale tunnel URL. VS-004 reaches *code complete* at Task 9 and records `PARTIAL` until a real `Hello` is answered.

---

## Conflicts, and the decisions taken

`CLAUDE.md` says: if requirements are ambiguous, ask — do not guess. These are the points where the written requirements, the slice doc and the existing code did not line up. **All of them are now decided.** Each resolution is recorded so it reads as a decision rather than a silent choice.

**C1. `500` or `503` when the enqueue fails? — decided: 503, `detail="queue unavailable"`.** Requirement 1 originally said 500. VS-003 already established 503 for the comparable failure — "the events are not durably ours" — with a documented reason (`docs/plans/VS-003-plan.md`, "The response contract"), a test pinning the code, and a deliberate `from None` so no traceback can leak patient content. Meta treats 500 and 503 identically: both are retried. One 5xx meaning in this endpoint, one reason written down once.

**C2. What the job carries — decided: the `webhook_inbox` row's UUID primary key, never `provider_event_id`.** The two candidates were the row UUID and `provider_event_id` (`msg:<wamid>` / `status:<wamid>:<status>`).

*Why the UUID, and why this is a privacy decision rather than a style one:* **a wamid is not an opaque token.** It is base64, and a decoded wamid commonly contains the patient's phone number; a status event id contains the wamid of the message **we sent to the patient**, so it identifies a patient twice over. VS-003 treated wamids as safe to log on the reasoning that they are "Meta identifiers, not patient content" — that reasoning does not survive base64-decoding one, and this slice would otherwise spread them into Redis keys, job arguments, five retries' worth of log lines, and `dead_letter_jobs`. The broader principle: **we do not build on the internal format of an identifier Meta controls.** Our own row UUID carries no information about anyone, and it is the only id in this slice that is ours.

Consequences, all of them implemented below:

- **The webhook needs the row id for duplicates too.** `store_if_new` returns `None` on conflict, and requirement 1 says a redelivered item must still be enqueued. So for those items only, the webhook looks the existing row up by `provider_event_id` on its unique index — one extra query on the duplicate path, none on the normal one. `store_if_new` itself is untouched (Task 5, Step 6).
- **Job argument: the row UUID. arq job id: `f"inbox:{row_id}"`.** The prefix keeps our job-id namespace explicit inside Redis, which also holds arq's own keys.
- **Every log line in this slice logs the row UUID as `event_id=`** — never `provider_event_id`, never a wamid, never a status id. That includes the enqueue-failure line in the webhook (which logs row ids, not the keys it just stored) and every line in the status handler.
- **`dead_letter_jobs.payload` carries the row id, not `provider_event_id`.** `source_event_id` on that table already points at the row, so triage loses nothing.
- **A test asserts that no log line and no job argument in this slice contains a wamid** (Task 5, Step 2).

*What this does not change:* `webhook_inbox.provider_event_id` is still the dedupe key, still unique, still what the webhook stores. It just stops travelling.

**C2a. VS-003's "a wamid is not patient content" note is now wrong, and VS-004 does not fix it there.** VS-003's webhook logs `provider_event_id` values on the happy path (`whatsapp webhook stored … ids=%s`). That is pre-existing behaviour in a merged slice, and narrowing it is a change to VS-003's tested log contract, not this slice's scope. It is recorded as a **Follow-up on VS-004** — "VS-003's webhook logs wamids; a decoded wamid contains a phone number, so those lines should log row ids instead" — and nothing VS-004 writes adds to the problem.

**C3. `webhook_inbox` already has the processed marker — but one column *is* needed.** Requirement 1 asks us to check whether a processed marker exists. It does: `webhook_inbox.status` is a `VARCHAR(16)` with a CHECK over `InboxStatus` = `RECEIVED | PROCESSING | PROCESSED | FAILED`, alongside `attempts` and `last_error`, written by `WebhookInboxRepository.mark()`. So no column is needed *for that*. What is needed is a **lease**, for the reason in C3a — `webhook_inbox.locked_until`, added in the Task 2 migration.

**C3a. The obvious `claim()` has a concurrency bug, and it is the bug that duplicates replies.** The natural guard is `status <> 'PROCESSED'`, which has to allow `PROCESSING` — a previous try that died between the two commits left the row `PROCESSING`, and that row must be claimable or the patient is never answered (see "Commit boundaries"). But then two *simultaneous* runs of one event both claim it, both find the same reserved reply row with no wamid, and **both send**. The reply row's unique constraint does not help: it prevents two reply *rows*, not two sends from one row. So `status` alone cannot distinguish "a dead try" from "a live try", because the difference between them is time.

*Resolved with a time-limited lease:*

- `webhook_inbox.locked_until` — nullable `timestamptz`.
- `claim()` succeeds only when `status <> 'PROCESSED' AND (locked_until IS NULL OR locked_until < now())`, and sets `locked_until = now() + job_timeout_seconds + job_lease_margin_seconds` in the same statement. One `UPDATE`, so the database arbitrates.
- `ClaimResult` gains a fourth state, **`locked`**, treated as **retryable**: another worker is on it right now, and if that worker dies its lease expires and this event becomes claimable again.
- The lease is **cleared** (`locked_until = NULL`) on every exit: when the job raises `Retry`, when it dead-letters, and when it finishes. Holding a lease past the job's own end would delay a legitimate retry by the whole lease duration for no benefit.
- **The lease must outlive the job.** A lease shorter than `job_timeout_seconds` expires while the job is still running — and worse, while it is inside the one Meta call — which reintroduces exactly the duplicate it exists to prevent. `job_lease_margin_seconds` (default 30) is added on top, and a settings test asserts the ordering (Task 1, Step 2).

*Consequence:* the lease is wall-clock, and it is PostgreSQL's clock, not a worker's — `now()` is evaluated server-side in the same statement, so a worker with a skewed clock cannot shorten or extend its own lease. A worker killed with `SIGKILL` leaves a live lease behind, and its event waits out the remainder before a retry picks it up: a bounded delay, which is the correct trade against a duplicate reply.

**C3b (amendment A1). The claim commits on its own, immediately, before any of the job's own work begins.** A claim held inside the job's transaction is rolled back with it on every retryable error, taking `attempts + 1` with it — so the dead letter under-reports — and it keeps the row write-locked for the whole transaction, so a concurrent worker blocks instead of getting `locked` at once. See "Commit boundaries".

**C3c (amendment A2). No "duplicate insert, then re-read" path may abort the transaction it is in.** In PostgreSQL a failed `INSERT` aborts the whole transaction, so the re-read an `except IntegrityError` block exists to perform is the statement that fails. Every such path here is either `ON CONFLICT DO NOTHING` or wrapped in a `SAVEPOINT`; the table in "Commit boundaries" names which is which and why.

**C4. "Store all inbound message types" is currently impossible.** `messages.modality` has a CHECK constraint allowing `TEXT` and `VOICE_NOTE` only (`ck_messages_modality_valid`). An image, document, location, sticker, contact card or interactive reply has neither. *Resolved: widen the CHECK with `OTHER`* in the Task 2 migration. `app/db/enums.py`'s own docstring prescribes exactly this — "Swapping a CHECK constraint is fully reversible … a hand-written migration plus a new value in the runtime test in `tests/db/test_constraints.py`". Audio keeps `VOICE_NOTE`, so a voice note stored here is already the right shape for VS-008 to attach a transcript to.

**C5. There is no column linking a reply to the message it answers.** Requirement 3's "unique constraint allowing one reply per inbound message" needs one. *Resolved: add `messages.reply_to_message_id` (nullable `Uuid`, self-FK, `ON DELETE CASCADE`) with `uq_messages_reply_to_message_id` over it.* Nullable plus unique is exactly right in PostgreSQL: many NULLs are permitted, so every inbound row and every future non-reply outbound row is unaffected, while two replies to one inbound message are impossible at the database level rather than by our checking first.

**C6. "status pending" is spelled `QUEUED` here.** `MessageStatus` has no `PENDING`; it has `QUEUED`, documented as "outbound, not yet handed to Meta" — the same state requirement 3 describes. *Resolved: use `QUEUED`.* No enum change, no CHECK migration for it.

**C7. `dead_letter_jobs.payload` and requirement 6 contradict each other.** The model's docstring says the column holds "the same patient content as `webhook_inbox.payload`". Requirement 6 forbids message text and phone numbers in anything stored in `dead_letter_jobs`. *Resolved in favour of requirement 6: the payload written there is a reference envelope only* — `{"inbox_row_id", "kind", "phone_number_id", "job_try"}`, the row UUID rather than `provider_event_id` per C2 — and `source_event_id` already points at the `webhook_inbox` row that holds the full event. Nothing is lost, and a table people open to triage failures stops being a second copy of patient content. `app/db/models/dead_letter.py`'s docstring is corrected in Task 6 to say so.

**C8. VS-003's seam comment says to enqueue new rows only.** It reads "VS-004 enqueues one job per id in `new_event_ids` … duplicates are already filtered out". Requirement 1 says the opposite: a redelivered event that is already in `webhook_inbox` must be enqueued again, because the first enqueue may be exactly what failed. *Resolved in favour of the requirement:* the webhook enqueues one job per **extracted item**, and the duplicate suppression moves to arq's job-id uniqueness plus the worker's claim. The seam comment is rewritten, not just deleted.

**C9. `httpx` is a dev-only dependency.** It is in `[dependency-groups].dev`, because until now only the test client used it. The Meta client needs it in the api and worker images. *Resolved: move `httpx>=0.27` into `[project].dependencies` and re-lock* (`uv lock`). It stops being listed twice; the dev group keeps pytest, pytest-asyncio and ruff. This is the slice's only dependency change.

**C10. Three Meta settings exist in `.env.example` but not in `Settings`.** `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID` and `META_API_VERSION` are listed in the example file and are not fields on `Settings`, so nothing reads them today. Task 1 adds them, empty-by-default, following VS-003's assumption A3 (the app must boot without a Meta app). `META_API_VERSION` has no known-good value anywhere in this repo, so the plan defaults it to `v21.0` and Task 10, Step 1 has the developer set whatever version the Meta dashboard shows. An unknown version is a permanent 4xx, not a retry — see A6.

**C11. The tenant id type is still open, and now something depends on it.** VS-002 left it "unconfirmed"; every table has it as `sa.Uuid` and `TenantScopedRepository` takes it in `__init__`. *Resolved: one alias, `TenantId = uuid.UUID`, in `app/tenants/resolver.py`, and every VS-004 signature uses the alias.* If the type later becomes a string or an int, this slice changes in one line plus a migration. `ConfigTenantResolver` is the only thing that parses the configured value.

**C12. The 24h free-form window is not a gap in this slice.** `docs/architecture.md` says free-form replies are only allowed within 24h of the patient's last message, and `conversations.last_inbound_at` exists for it. VS-004 only ever replies to a message it has just received, so the window is open by construction and no template path is needed. Recorded here so its absence reads as a decision rather than an oversight; the template path belongs to whichever slice sends the first unprompted message.

**C13. arq does not retry a plain exception.** Verified against the arq documentation: a job retries when it raises `arq.worker.Retry(defer=...)`; `max_tries` (default 5) caps that, and `retry_jobs` concerns re-queueing on shutdown and cancellation, not ordinary failures. So requirement 4's "retries happen only at the job level (arq), with exponential backoff" is implemented by the job envelope **explicitly raising `Retry(defer=backoff(job_try))`**, which is also the only way we get a backoff curve we control. Not a conflict — a constraint the design has to respect, stated so nobody "simplifies" it into a bare `raise`.

**C14. Hard rule 7 applies even though this reply is not from an AI.** The slice doc calls the reply fixed and temporary. Hard rule 7 is written about AI replies. *Resolved: apply it anyway* — the worker re-reads the conversation state before sending, and drops the reply (logging the conversation id only) when it is `HUMAN_ACTIVE` or `CLOSED`. It costs one query and it means VS-005 inherits the check already in place and already tested, rather than adding it to a path that never had it.

---

## Understand first

**Why the webhook must not wait for slow work.** Meta gives the webhook a small budget — a handful of seconds — and judges us only by the HTTP status. An OpenAI call takes seconds; a Meta send takes hundreds of milliseconds and occasionally many seconds; a Booking Service call is another network hop. Do any of them inside the request and three things happen at once: Meta times out and redelivers an event we are in the middle of handling, our uvicorn workers fill up with requests that are waiting on someone else's server, and a single slow dependency turns into dropped patient messages. Hard rule 1 is the shape that prevents it: the request touches only our own database, and everything else happens in another process that nobody is timing. This slice is where that second process starts doing real work, so it is also where the first genuinely slow thing in this repo appears — and it appears on the far side of the queue.

**At-least-once delivery, twice over.** There are now *two* at-least-once channels, and each needs its own idempotency. Meta may deliver the same webhook more than once (VS-003 handled that with the unique `provider_event_id`). And a queue is at-least-once by nature: a job can be enqueued twice, a worker can die after doing half the work and the job can be retried, and a retry after a timeout cannot know whether the timed-out call actually happened. "Exactly once" is never a property of a delivery mechanism — it is something you build on top of an at-least-once one, out of a unique key and an idempotent handler. This slice has four such keys: `webhook_inbox.provider_event_id` (Meta's duplicate), the arq job id (the double enqueue), `webhook_inbox.status` with `locked_until` (the re-run and the *simultaneous* run, which are not the same problem), and `messages.reply_to_message_id` (the duplicate reply row). Note how none of them is a check the code performs — every one is a constraint or a conditional `UPDATE`, because a check-then-act has a window and a key does not. Wherever there is no key to lean on, there is a gap — and there is exactly one left, written down in "The duplicate-reply gap" below rather than hidden.

**Why the send is the hard part.** Storing a message twice is prevented by a constraint. Sending a WhatsApp message twice cannot be, because the send is not in our database and Meta's send endpoint has **no idempotency key** — there is no header we can pass that makes "send this message" safe to repeat. So the only thing we control is how narrow the window is between "Meta accepted this" and "we have written down that Meta accepted it". Everything about the reply path — reserve the row first, commit, then send, then record the wamid, and never send again once a wamid is recorded — exists to make that window as small as a crash can fit into, and to make the *second* crash harmless.

---

## Global Constraints

- Python 3.12 only. Dependency manager is **uv**. This slice moves `httpx` from the dev group to `[project].dependencies` (C9) and adds nothing else; `uv.lock` is re-locked with `uv lock` and committed. The Dockerfile and its uv pin (`ghcr.io/astral-sh/uv:0.12.19`) are untouched.
- **Hard rule 1 stays true.** The only line added to `app/api/whatsapp.py` is the enqueue loop at the marked seam, after the commit. The endpoint still makes no OpenAI call, no Meta call, no tenant resolution and no media download. `app/api/whatsapp.py` imports from `app.queue` and from nothing else new.
- **Hard rule 2 is the whole point of the slice.** One event delivered twice produces one stored inbound message and one reply. Three keys enforce it (above); no code path checks-then-acts where a constraint can decide instead.
- **Hard rule 3 is untouched.** No LLM in this slice. No tool, no prompt, no OpenAI import.
- **Hard rule 4 is implemented here for the first time.** `tenant_id` comes from `payload["metadata"]["phone_number_id"]` through `TenantResolver`, never from anywhere else. There is no tool, no request parameter and no job argument that carries a tenant.
- **Hard rule 5's shape appears here.** No booking in this slice, but its logic — never tell the patient something succeeded until the other side said so — is exactly the reply rule: the reply row becomes `SENT` only after Meta returned a wamid, and a send that failed permanently leaves a `FAILED` row and a dead letter, not a cheerful reply.
- **Hard rule 6 does not apply yet.** No booking-changing call is made. The idempotency key derived from the source message ID has its analogue here (the arq job id and the reply link), and VS-007 inherits the pattern.
- **Hard rule 7 is implemented here** (C14): the conversation state is re-read from the database immediately before the send, and a `HUMAN_ACTIVE` or `CLOSED` conversation drops the reply, logged by conversation id only.
- **Hard rule 8 gets three new leak surfaces, all closed by construction.** (a) **Redis**: the job argument is one `webhook_inbox` row UUID and the job result is a short outcome code — no payload, no `wa_id`, no text, and no Meta-controlled identifier (C2: a decoded wamid contains a phone number). (b) **Meta error bodies**: Meta's `error.message` can quote a recipient's phone number, so no Meta-supplied string is ever logged or stored; the recorded reason is built from numeric codes (`http_429`, `code_131026`) and passed through a scrubber as belt and braces. (c) **`dead_letter_jobs`**: a reference envelope, never the event (C7). `phone_number_id` *is* loggable — it is Meta's numeric identifier for the clinic's own WhatsApp account, not a phone number and not a patient; `display_phone_number`, `wa_id`, `from`, `recipient_id`, **and every wamid or `provider_event_id`** are not.
- **Every log line in this slice identifies an event by the row UUID**, written as `event_id=<uuid>` as the first field. One spelling, so a `grep` for a wamid across `app/worker/`, `app/queue/` and the seam finds nothing (C2).
- **Hard rule 9: no credential in source, and none in a log.** `META_ACCESS_TOKEN` is read from `Settings`, sent as `Authorization: Bearer …`, and asserted absent from every log line and every dead letter. Tests use the obvious fakes in `tests/whatsapp_factories.py`.
- **Hard rule 10 is not in scope.** Every message gets the same fixed ack. Medical-content triage is VS-010's.
- **Hard rule 11 is the other half of the slice.** One send attempt per job try, a timeout on it, bounded retries with exponential backoff at the job level, and `dead_letter_jobs` when the tries run out or the error is permanent.
- **No real network in any test.** Meta is an `httpx.MockTransport`. There is no test anywhere in this slice that would pass or fail differently depending on whether a machine has internet.
- Linting: `ruff check .` clean, `ruff format .` leaves no diff. Line length 100, target `py312`.
- **`pytest` must still pass with no Postgres and no Redis running.** Database tests are marked `@pytest.mark.db`. Everything about classification, backoff, resolution, ordering and the queue interface is provable with nothing running.
- Stay inside VS-004 scope. **Out of scope:** OpenAI and any generated reply (VS-005), the Agent Core and tools (VS-006), booking (VS-007), media download and transcription (VS-008), TTS (VS-009), the handoff *workflow* (VS-010). Anything that looks necessary but is out of scope goes under "Follow-ups" in `docs/slices/VS-004.md`, not into the code.
- Every commit message ends with the trailer:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

---

## The job contract

One table, one row per outcome, every row tested.

| Situation | Outcome | `webhook_inbox.status` | Reply sent? | Dead letter? |
|---|---|---|---|---|
| Row already `PROCESSED` | `skipped` | unchanged | no | no |
| Row held by a live lease (another worker) | retryable | unchanged | no | after max tries |
| Row missing entirely | retryable | — | no | after max tries |
| `payload["kind"]` absent or unknown | permanent | `FAILED` | no | immediately |
| `metadata.phone_number_id` absent | permanent | `FAILED` | no | immediately |
| `phone_number_id` not in the tenant map | permanent | `FAILED` | no | immediately |
| Message item fails `InboundMessage` | permanent | `FAILED` | no | immediately |
| Message of a type not in `WHATSAPP_REPLY_TO_TYPES` | `stored_no_reply` | `PROCESSED` | no | no |
| Conversation is `HUMAN_ACTIVE` or `CLOSED` | `dropped_not_ai_active` | `PROCESSED` | no | no |
| `get_or_create_open` raises `IntegrityError` | retryable | unchanged | no | after max tries |
| Meta returned a wamid | `replied` | `PROCESSED` | yes, once | no |
| Reply row already carries a wamid | `already_replied` | `PROCESSED` | **no** | no |
| Meta 5xx, 429, timeout, or a transport error | retryable | unchanged | no | after max tries |
| Meta 4xx other than 429 | permanent | `FAILED` | no | immediately |
| Meta 2xx with no `messages[0].id` | `sent_without_id` | `PROCESSED` | yes, once | no |
| Status for a wamid we hold | `status_advanced` / `status_ignored` | `PROCESSED` | n/a | no |
| Status for a wamid we do not hold yet | retryable | unchanged | n/a | after max tries |
| Status word we do not model | `status_ignored` | `PROCESSED` | n/a | no |

**"retryable" always means the same thing:** roll back, release the lease, and if `ctx["job_try"] < JOB_MAX_TRIES` raise `arq.worker.Retry(defer=backoff(job_try))`; otherwise write a dead letter and return. **"permanent" always means the same thing:** roll back, release the lease, write a dead letter now, mark the inbox row `FAILED`, and return without raising — a retry cannot change the answer, and raising would only make arq log a traceback.

The one exception to releasing the lease is the `locked` row: it belongs to another worker, so this job never clears it.

---

## Commit boundaries

This is the part of the slice that correctness actually rests on. A message job has **exactly three commits**: the claim on its own, then T1, then T2 — and what sits between the last two is the one Meta call.

```
  T0  claim the inbox row          UPDATE ... WHERE status <> 'PROCESSED'
                                     AND (locked_until IS NULL OR locked_until < now())
                                   SET PROCESSING, attempts + 1,
                                       locked_until = now() + timeout + margin
      ---------------------------------------------------------------- COMMIT
  T1  attach the resolved tenant
      upsert contact + identity
      get-or-create open conversation
      store the inbound message    (INBOUND, RECEIVED, provider_message_id = wamid)
      re-read conversation state   (hard rule 7)
      reserve the reply row        (OUTBOUND, QUEUED, reply_to_message_id = inbound.id)
      ---------------------------------------------------------------- COMMIT
      send to Meta                 ONE attempt, with a timeout
      ----------------------------------------------------------------
  T2  reply row: provider_message_id = wamid, status = SENT, sent_at = now()
      inbox row:  status = PROCESSED, locked_until = NULL
      ---------------------------------------------------------------- COMMIT
```

**Why the claim commits on its own, before T1 begins (amendment A1).** Left inside T1, the claim is rolled back with T1 on any retryable error — and two separate things break. First, `attempts + 1` is undone, so a job that failed five times reports one attempt in its dead letter and the retry curve becomes invisible. Second, the row stays write-locked by the uncommitted `UPDATE` for the whole of T1, so a concurrent worker **blocks** on it — for a tenant lookup, a contact upsert and a conversation read — instead of being told `locked` immediately and deferring. The lease only does its job if the fact of the lease is visible to other transactions, and in PostgreSQL that means committed.

*Consequence:* a job that dies between T0 and T1 leaves a claimed row with an incremented attempt count and no work done. That is exactly right — the lease expires, the next try reclaims it, and the attempt count is the truth.

Why the reply row is committed **before** the send: if it were written in the same transaction as the wamid, a crash during the send would leave no trace that a send was ever attempted, and the retry would have nothing to recognise. Committed first, the row is a durable "a reply to this message is in flight", and it is the thing the unique constraint uses to stop a second job from starting a second reply.

Why the inbox row is marked `PROCESSED` only in T2: mark it earlier and a crash during the send makes the retry skip the send entirely — a patient message silently unanswered. The inbox row stays `PROCESSING` across the Meta call on purpose, which is also why `claim()` tests `status <> 'PROCESSED'` and not `status = 'RECEIVED'`.

And that is precisely why the lease exists (C3a): `PROCESSING` has to stay claimable for the dead try, so something other than `status` must distinguish a dead try from a live one. `locked_until` is that something, and it spans the Meta call — which is the interval a second concurrent run would otherwise duplicate.

A status job has the claim commit and one more, and no Meta call.

**No "duplicate insert, then re-read" may abort the transaction it is in (amendment A2).** In PostgreSQL a failed `INSERT` aborts the *whole* transaction, so catching an `IntegrityError` and carrying on is a trap: the very next statement — including the re-read the `except` block was written to allow — raises `InFailedSqlTransaction`. Every such path in this slice is therefore one of two shapes, and `tests/db/test_repositories.py` proves the session survives each:

| Path | Shape | Why |
|---|---|---|
| contact + identity | `ON CONFLICT DO NOTHING` | VS-002 already; nothing fails, so nothing aborts |
| reply row | `ON CONFLICT DO NOTHING` | Task 2's `reserve_reply`; same reason, and no exception quotes the reply text |
| inbound message | `SAVEPOINT` (`session.begin_nested()`) | `MessageRepository.add` must keep raising `DuplicateRecordError` — it is a real signal, and the job re-reads the existing row on it. The savepoint rolls back the failed `INSERT` only |
| open conversation | `SAVEPOINT` | `get_or_create_open` deliberately keeps propagating `IntegrityError` (the job retries on it), but the session it propagates through has to still work |

---

## The duplicate-reply gap

**Stated plainly, because it is real and it is being accepted, not solved.**

If the process dies — or the container is killed, or the Meta call times out on our side after Meta already accepted it — **after** Meta accepted the send and **before** T2 commits the wamid, then the retry finds a reply row with no wamid, cannot tell that the send succeeded, and sends again. The patient sees `Received ✅` twice.

Why it cannot be closed here:

- **Meta's send endpoint has no idempotency key.** There is no header, no client-supplied id, nothing to pass that makes a repeated send a no-op. The wamid is assigned by Meta and only comes back in the response we lost.
- **A timeout is genuinely ambiguous.** A timeout on our side tells us nothing about whether the request arrived. Classifying timeouts as permanent (never retry) would trade a rare duplicate for a routine silent loss, which is worse.
- **The alternative ordering is worse.** Marking the reply row `SENT` *before* calling Meta would make a duplicate impossible and make silent loss ordinary: every crash during a send would leave a message the patient never received and the system believing it was sent.

So the deliberate choice is: **at-least-once delivery of the reply, with the window as narrow as two adjacent statements**. One extra `Received ✅` after a crash is visible, harmless and self-explaining; a missing reply is invisible. Recorded as a Follow-up on VS-004 with the two things that would narrow it further if it ever bites: reading back Meta's message list for the conversation before resending, and a short-lived "send in flight" marker with its own timestamp so a retry can at least wait out Meta's own processing window.

**`sent_without_id` is the same gap from the other side.** A 2xx whose body we cannot read a wamid out of means Meta accepted the message and we have no id for it. The plan treats it as success — the row is `SENT`, nothing is resent — and logs a warning. The cost is that no status callback will ever match that row. Retrying instead would guarantee the duplicate we are trying to avoid.

---

## Idempotency: the four keys

| What could happen twice | The key that stops it | Where |
|---|---|---|
| Meta delivers the same webhook twice | `webhook_inbox.provider_event_id` unique | VS-003, unchanged |
| The same event is enqueued twice | the arq job id = `inbox:<row uuid>` | Task 5 |
| A job that already ran runs again | `webhook_inbox.status = 'PROCESSED'`, tested by `claim()` | Task 2, Task 6 |
| Two jobs process one event **concurrently** | `webhook_inbox.locked_until` — the lease in the same conditional `UPDATE`. `status` alone cannot do this: it must let a *dead* `PROCESSING` try be reclaimed, so only time separates the dead try from the live one (C3a) | Task 2, Task 6 |
| A worker dies holding a lease | the lease expires (`job_timeout + margin`) and the event becomes claimable | Task 2 |
| The same inbound message is stored twice | `messages.provider_message_id` unique | VS-002, unchanged |
| Two replies to one inbound message | `uq_messages_reply_to_message_id` | Task 2 |
| Two contacts for one phone number | `uq_contact_identities_identity` | VS-002, unchanged |
| Two open conversations for one contact | `uq_conversations_open` partial unique | VS-002, unchanged |

**The arq job id is a short-lived key, not a permanent one.** `enqueue_job(..., _job_id=x)` returns `None` when a job with that id is queued, running, or has a kept result — and arq keeps results for a limited time (`keep_result`, one hour by default). Once that expires, the same id enqueues and runs again. That is precisely why requirement 1 also asks for the worker-side check: arq's dedup handles the burst (Meta redelivering within minutes), and `webhook_inbox.status` handles forever.

**The job id is `f"inbox:{row_id}"`, not the bare UUID.** Redis holds arq's own keys alongside ours; an explicit namespace means a key we see in `redis-cli` says what it is. It is also stable across redeliveries for free — the row UUID does not change when Meta redelivers, because `store_if_new` did not create a new row.

---

## Status ordering

Requirement 5: statuses only move forward. One rank table, in `app/db/enums.py`, next to the enum it ranks:

| `MessageStatus` | rank | reached by |
|---|---|---|
| `RECEIVED` | 0 | inbound only, never advanced |
| `QUEUED` | 0 | a reserved reply row |
| `SENT` | 1 | Meta returned a wamid, or a `sent` callback |
| `FAILED` | 2 | a `failed` callback |
| `DELIVERED` | 3 | a `delivered` callback |
| `READ` | 4 | a `read` callback |

The update is `UPDATE messages SET status = :new WHERE provider_message_id = … AND tenant_id = … AND :new_rank > <CASE over current status>`. Strictly greater, so a redelivered `delivered` after `read` changes nothing and a repeated `read` changes nothing, with no read-then-write race.

`FAILED` ranks above `SENT` and below `DELIVERED` on purpose: a send that Meta later reports as failed must overwrite `SENT`, while a message that was actually delivered did not fail. `DELIVERED` after `FAILED` would overwrite — it cannot happen, and if Meta ever does it, "delivered" is the more useful truth.

---

## Assumptions

Listed, not decided silently. Each has a named consequence.

**A1. The tenant map is configuration, and a broken map is a loud runtime failure rather than a boot failure.** `WHATSAPP_TENANT_MAP` is a `str` on `Settings`, holding JSON like `{"<phone_number_id>": "<tenant uuid>"}`, parsed once by `ConfigTenantResolver.from_settings()`. It is deliberately **not** typed as `dict[str, str]`: pydantic-settings JSON-decodes complex fields, and `.env.example` ships keys with empty values, so `WHATSAPP_TENANT_MAP=` would raise at import and stop the app from booting from its own example file — the exact failure VS-003's A3 was written to avoid. Instead, an empty or malformed map resolves nothing: every event dead-letters with `unknown_phone_number` or `bad_tenant_map`, which is loud in a table built for exactly that, while `/health` stays up. *Consequence:* a typo in the map is not caught at startup. A startup warning naming the entry count (never the values) makes it visible; a validating `scripts/` helper is a follow-up.

**A2. `DEV_TENANT_ID` + `META_PHONE_NUMBER_ID` are honoured as a one-entry fallback map.** Both keys already exist in `.env.example` and nothing reads them. When `WHATSAPP_TENANT_MAP` is blank and both of those are set, `from_settings()` builds the single-entry map from them. *Why:* the developer has one number and one clinic, and the live test in Task 10 should not need hand-written JSON. *Consequence:* two ways to configure one thing, so the resolver logs which one it used (source name only), and the explicit map always wins.

**A3. The tenant id is a UUID** (C11). `TenantId = uuid.UUID`, one alias, one parse site. *Consequence:* a configured value that is not a UUID is a `bad_tenant_map` permanent failure, not a crash.

**A4. The reply is a fixed constant in code, not a setting.** `ACK_TEXT = "Received ✅"` lives in the job module. It is temporary by the slice's own words and VS-005 deletes it. *Consequence:* changing it is a code change. A setting would be a knob nobody will ever turn, on a value that is about to disappear.

**A5. Which types get a reply is a setting.** `WHATSAPP_REPLY_TO_TYPES`, a comma-separated `str`, default `text`, parsed to a `frozenset`. *Why a comma-separated string and not a list:* the same pydantic-settings JSON trap as A1. *Consequence:* an image gets stored and silently not answered, which is correct for this slice and is what `stored_no_reply` records.

**A6. An unknown or retired `META_API_VERSION` is a permanent error, and that is right.** Meta answers 400 for a bad version, and no number of retries fixes a wrong string in `.env`. *Consequence:* if the developer's `.env` has the wrong version, every reply dead-letters immediately with `http_400 code_…` and the reason is visible in one query rather than after five retries. Task 10, Step 1 sets it from the dashboard.

**A7. The send goes to the `phone_number_id` the message arrived on**, read from `payload["metadata"]["phone_number_id"]` — not from `META_PHONE_NUMBER_ID`. *Why:* with more than one clinic, the setting is simply the wrong number, and a reply sent from another clinic's number is worse than no reply. *Consequence:* `META_PHONE_NUMBER_ID` is only ever used for the A2 fallback map.

**A8. One `httpx.AsyncClient` per worker process, built in `on_startup`, closed in `on_shutdown`.** *Why:* connection reuse, and because a client per job leaks sockets under load. *Consequence:* the client is in `ctx`, so every job test builds its own with a `MockTransport` and there is no global to patch.

**A9. Backoff is `min(base * 2 ** (job_try - 1), cap)`**, with `JOB_BACKOFF_BASE_SECONDS=5.0` and `JOB_BACKOFF_MAX_SECONDS=300.0`. With `JOB_MAX_TRIES=5` the deferrals are 5s, 10s, 20s, 40s, and the fifth failure dead-letters — about 75 seconds of patience. *No jitter:* our jobs are keyed per message and do not stampede a shared resource; a thundering herd needs many jobs failing on the same tick, which Meta's per-number rate limit would cause and which the 429 path plus the cap already blunts. Noted as a follow-up rather than added.

**A10. `job_try` comes from `ctx["job_try"]` and defaults to 1** when absent, so a job called directly from a test behaves like a first try.

**A11. A status for an unknown wamid retries and then dead-letters.** It is the only way to handle requirement 5's "the status webhook can arrive before the worker saved the wamid". *Consequence:* a status for a message **we never sent** — one a staff member sent from the Meta Business app, or `sent_without_id` from the gap above — retries through the whole curve and then lands in `dead_letter_jobs`. Noisy but honest, and the dead letter says `status_before_wamid`. If it turns out to be common, the fix is to recognise "a wamid this tenant has never seen at all" as ignorable, which needs a sent-wamid record we do not have; follow-up.

**A12. A status word we do not model is ignored, not dead-lettered.** Meta adds status values (`deleted`, `warning`) without notice, and a 500-equivalent for each of them would fill the dead letter table with things nobody will act on. *Consequence:* the row is `PROCESSED` with outcome `status_ignored` and a log line naming the word.

**A13. `webhook_inbox` gets no new index in this slice.** VS-002's follow-up ("no index on `webhook_inbox.status`") stays open: the worker's only lookup is by `provider_event_id`, which already has a unique index, so a status index would serve ops queries we have not written. Re-stated as a follow-up with that reasoning rather than added speculatively.

**A14. A hash-keyed inbox row (`msg:sha256:…`) dead-letters as `unmodelled_message`.** VS-003 stores items it could not read rather than dropping them; this slice cannot process them, because there is no wamid to make the inbound message idempotent on. *Consequence:* they land in `dead_letter_jobs` immediately, which is what VS-003's A1 predicted ("VS-004 dead-letters what it cannot interpret").

**A15. `ping` stays registered on the worker.** `tests/test_worker.py` asserts it, and it is still the cheapest proof that the worker consumes from Redis.

**A16. The claim lease is `job_timeout_seconds + job_lease_margin_seconds`, measured by PostgreSQL's clock** (C3a). `now()` is evaluated server-side inside the claiming `UPDATE`, so no worker can shorten or extend its own lease by having a wrong clock, and two workers do not need to agree on the time — only the database does. *Consequence:* a worker killed with `SIGKILL` leaves a live lease, and its event waits out the remainder (default 90s) before a retry can claim it. A bounded delay in exchange for a duplicate reply that cannot happen is the right way round. A worker that is *cancelled* cleanly releases the lease on its way out.

**A17. The lease is released on every exit, including a retry.** Keeping it until expiry after the job has already decided to retry would add the whole lease duration to a backoff curve that is already deliberate (A9). *Consequence:* the release is in the same `except` blocks as the retry and the dead letter, so a new exit path added later without a release is a bug the concurrency test will catch rather than a silent stall.

---

## VS-003 follow-ups: what is pulled in, and what is not

| Follow-up | Verdict |
|---|---|
| `ConversationRepository.get_or_create_open`'s `IntegrityError` must be treated as retryable by VS-004's caller (VS-002, restated in VS-003) | **Pulled in (Task 7).** Requirement 2 asks for it explicitly. Caught in the message handler and re-raised as `RetryableJobError("conversation_race")`; the original exception is never chained, so the constraint name is the most that is ever logged. |
| `payload["kind"]` must be read, never string-split out of `provider_event_id` | **Pulled in (Task 6).** A test asserts `app/worker/` contains no `split(":")` over a `provider_event_id`, and the dispatcher reads `payload["kind"]`. |
| The dedupe key is derived from full model validation; it should use `id`/`status` alone | Not in scope. It is VS-003's `_message_key`, in the webhook path, and changing it now would move the dedupe key format mid-slice. Stays a VS-003 follow-up. VS-004 is unaffected: it reads `payload["kind"]` and the raw `item`, never the key. |
| A message containing U+0000 fails every delivery (jsonb cannot store it) | Not in scope, and untouched: the failure is still in the webhook, before this slice's code runs. Restated on VS-004 because VS-004 adds the third option that follow-up names — dead-lettering one case — and now has a dead-letter path to do it with. |
| `/health/ready` reachable through the tunnel; unbounded body read before signature check; no rate limit or source-IP allowlist | Not in scope. All three are webhook-hardening, none is touched by this slice, all stay open on VS-003. |
| `webhook_inbox.payload` retains patient text indefinitely | Not in scope, and **VS-004 makes it worse in one place and better in another**: `messages.text` becomes a second copy of patient text (unavoidable — it is the conversation), while `dead_letter_jobs` deliberately becomes *not* a third copy (C7). Restated on VS-004, still paired with VS-008's audio retention. |
| Structured JSON logging with a request id | Not in scope, and now more tempting than ever: correlating a webhook, an enqueue, five job tries and a dead letter is eyeball work. Every log line in this slice therefore carries `event_id=` as its first field, which is the poor version of the same thing. Stays a follow-up. |
| An item keyed by content hash whose redelivery differs by one field lands twice | Not in scope; A14 says what VS-004 does with such rows. |
| `provider_event_id` longer than 255 characters becomes a 503 | Not in scope. Unchanged: this slice adds no longer key. Note the arq job id inherits the same length. |
| `DOCS_ENABLED` has no auth; only SHA-256 signatures accepted; `scripts/` untested | Not in scope. Nothing here touches them. |
| No index on `webhook_inbox.status` (VS-002) | Not in scope — see A13, with the reasoning now that there is a reader. |
| VS-003 Task 7's live verification — the handshake succeeded and the `messages` field is subscribed, but no real message has ever reached the callback | **Inherited as a diagnosis, not as a blocker.** It is why VS-004's Task 10 leads with `GET /{WABA_ID}/subscribed_apps` and the test number's allowed recipient list rather than with sending `Hello`. VS-003's own status is not reopened: it is merged to `main`. |
| VS-003's happy-path log line prints `provider_event_id` values, i.e. wamids | **Restated as a VS-004 follow-up (C2a), deliberately not fixed here.** A decoded wamid contains a phone number, so those lines should log row ids — but they are a merged slice's tested log contract, and narrowing them is its own change. VS-004 adds nothing to the problem: every line it writes uses the row UUID. |

---

## Review Focus

Ten things the slice implies but does not spell out. Each has a test in the task that owns the code.

1. **The webhook must still be fast, and it must still not call Meta.** The enqueue is the only new `await`, and it is after the commit. Tests: a mocked Meta transport that sleeps 2 seconds is never touched by the request, and the request returns in well under a second (Task 9); `app/api/whatsapp.py` imports nothing from `app.channels.whatsapp.client` or `app.worker` (Task 5).
2. **An enqueue failure must not answer 200.** A 200 with no job means the event sits in `webhook_inbox` forever and the patient is never answered — the exact silent loss VS-003's 503 exists to prevent, one layer further in. Test: a queue that raises answers 5xx, and a redelivery of the same event enqueues a job (Task 5).
3. **A redelivered event must still be enqueued** (C8). The tempting reading — "it is already stored, so it is already handled" — loses the message whenever the *first* enqueue was what failed. Test: a POST whose items are all duplicates still enqueues one job per item (Task 5).
4. **Two runs of one job must produce one reply — sequentially *and* concurrently, which need different mechanisms.** Three of them have to work together: `claim()`'s `PROCESSED` check for the ordinary "already done" case, the **lease** for the simultaneous case (the reply row's unique constraint does not help there — it stops two reply *rows*, not two sends from one row: C3a), and the reply row's wamid for the case where the first run died mid-flight. Tests: calling the job twice sends once; calling it twice **concurrently, from two independent sessions,** sends once; a live lease is not claimable; an expired lease is; a job whose reply row already has a wamid sends nothing (Tasks 2, 6 and 7).
5. **The reply row must be committed before the send.** If the commit boundary moves, everything above still passes while the crash window silently widens from two statements to the whole job. Test: a Meta transport that raises after recording the call proves the reply row is already durable when the send happens, and the retry finds it (Task 7).
6. **Classification must live in one place, and the client must attempt exactly once.** Two failure shapes: a retry loop inside the client (then backoff, `max_tries` and the dead letter all lie), and classification re-derived at the call site (then 429 is retryable in one branch and permanent in another). Tests: the client makes exactly one request for a 500; every status code maps through one function; the job module contains no status-code literals (Tasks 4 and 6).
7. **Statuses must not move backwards, and must not need a read-then-write.** Test: `read` then `delivered` leaves `READ`; the guard is in the `WHERE` clause, proven by two concurrent updates (Task 8).
8. **A status that arrives before its wamid must retry, not vanish and not dead-letter on the first try.** This is Meta's ordinary out-of-order delivery, not an error. Tests: the status job for an unknown wamid raises `Retry`, and the same job succeeds once the wamid is stored (Task 8).
9. **Nothing patient-identifying may reach Redis, a log, or `dead_letter_jobs` — and a wamid counts as patient-identifying** (C2: base64-decode one and the phone number is in it). Tests: the enqueued job's arguments are exactly one row UUID; **no log line and no job argument anywhere in this slice contains a wamid**; a Meta error body containing a phone number produces a reason with none of its digits; `dead_letter_jobs.payload` keys are a fixed safe set carrying the row id; the access token appears in no log line; `caplog` assertions on every failure path (Tasks 4, 5, 6, 7, 8).
10. **Hard rule 7 must be checked against the database, not against a value read earlier in the job.** A conversation that a human took over while the job was resolving a tenant must still stop the reply. Test: flipping the state to `HUMAN_ACTIVE` between the store and the send drops the reply and stores no reply row (Task 7).

---

## Running the tests

```bash
uv run pytest                                    # nothing running: db tests skip
docker compose up -d postgres redis && uv run pytest
docker compose exec api pytest                   # the run acceptance is judged on
```

Baseline before this slice, measured on 2026-09-28: **132 passed with Postgres up** (43 of those skip when it is not, leaving 89 passed). Confirm this number at Task 1, Step 1 before trusting the per-task counts below — they are targets for "did I write the tests this task calls for", not contractual.

Redis is **not** required by any test in this slice. The queue tests use a fake and an injected arq pool; there is no test that talks to a real Redis, so the `redis` service is needed for `docker compose up` and for Task 10, not for `pytest`.

New test directories `tests/worker/`, `tests/queue/` and `tests/tenants/` each get an `__init__.py`. `tests/worker/conftest.py` re-exports the database fixtures the same way `tests/api/conftest.py` does, and for the same reason — plus `second_session_factory`, which the concurrency tests need and which the rollback-wrapped `db_session` cannot provide.

---

## Reporting instead of checkpoints

**No task stops for the developer except Task 10**, which needs their phone. Each task ends by appending an entry to `.superpowers/sdd/VS-004-report.md` — same file for the whole slice, in the shape of `VS-003-report.md`:

- the task, and the test count after it (against the number this plan predicted, so drift is visible while it is still small);
- anything this plan got wrong — a wrong assumption, a test that had to be rewritten, an interface that did not survive contact with the code;
- anything decided that the plan did not anticipate, and why.

A task whose report entry says only "done, tests pass" has not been reported on. The useful entries are the ones naming a surprise, because that is what the next slice reads.

---

### Task 1: Settings, and httpx as a runtime dependency

Everything else stands on these. No behaviour yet.

**Files:**
- Modify: `pyproject.toml` (move `httpx` into `[project].dependencies`), `uv.lock` (re-lock)
- Modify: `app/config.py` (the Meta send settings, the tenant map, the reply-type filter, the retry knobs)
- Modify: `app/main.py` (extend the startup warning to the new secrets)
- Modify: `.env.example` (add the new keys with comments; **every existing key stays exactly as it is** — nothing renamed, reordered or removed)
- Test: `tests/test_config.py` (+8)

**Interfaces:**
- Produces on `Settings`: `meta_access_token: str = ""`, `meta_phone_number_id: str = ""`, `meta_api_version: str = "v21.0"`, `meta_api_base_url: str = "https://graph.facebook.com"`, `meta_send_timeout_seconds: float = 10.0`, `whatsapp_tenant_map: str = ""`, `dev_tenant_id: str = ""`, `whatsapp_reply_to_types: str = "text"`, `job_max_tries: int = 5`, `job_backoff_base_seconds: float = 5.0`, `job_backoff_max_seconds: float = 300.0`, `job_timeout_seconds: float = 60.0`, `job_lease_margin_seconds: float = 30.0`, derived `claim_lease_seconds` and `reply_to_types` properties, and a `_blank_means_unset` validator for the two settings that have real defaults.

**Expected tests after this task: 141** (actual: 142 — one extra test, see Step 2's blank-version entry)**.**

- [ ] **Step 1: Confirm the baseline**

```bash
uv run pytest -q
```

Write the number down. Every later "expected tests" line is relative to it.

- [ ] **Step 2: Write the failing settings tests**

In `tests/test_config.py`:

- `test_the_meta_send_settings_default_to_empty_or_safe_values` — `meta_access_token == ""`, `meta_phone_number_id == ""`, a non-empty `meta_api_version`, and a `meta_api_base_url` on `graph.facebook.com`. Empty credentials by default, VS-003's A3: the app must boot without a Meta app.
- `test_a_blank_meta_api_version_falls_back_to_the_default` — **not in the plan's first draft, and needed.** `.env.example` ships `META_API_VERSION=` with no value, so a `.env` copied from it sets the variable to the empty string, which pydantic accepts as a perfectly good `str`. The send URL would then be built with a missing path segment and every reply would 404 in a way that looks like a bug in the client. A `mode="before"` validator on `meta_api_version` and `meta_api_base_url` treats a blank string as "unset" and restores the field default. Deliberately **not** applied to the credentials: an empty `META_ACCESS_TOKEN` must stay empty, because "not configured" has to mean "every send fails visibly", never "fall back to something".
- `test_the_tenant_map_is_a_plain_string_and_defaults_to_empty` — asserts the *type* is `str`, with a comment naming A1: a `dict` field would make `WHATSAPP_TENANT_MAP=` in `.env.example` raise at import.
- `test_an_empty_tenant_map_does_not_stop_the_app_from_starting` — `Settings(_env_file=None, …, whatsapp_tenant_map="")` builds.
- `test_a_malformed_tenant_map_does_not_stop_the_app_from_starting` — `whatsapp_tenant_map="{not json"` builds; the failure belongs to the resolver, not to boot.
- `test_the_reply_type_filter_defaults_to_text_only`
- `test_the_retry_knobs_have_the_documented_defaults` — `job_max_tries == 5`, base 5.0, cap 300.0.
- `test_the_job_timeout_exceeds_the_send_timeout` — a real constraint: an arq `job_timeout` below the httpx timeout would cancel the job mid-send and turn every slow send into an ambiguous one.
- `test_the_claim_lease_outlives_the_job_timeout` — `claim_lease_seconds > job_timeout_seconds`, by the margin. **C3a's failure mode, pinned in a test:** a lease that expires while the job is still inside the Meta call lets a second worker claim the event and send the same reply, which is the exact duplicate the lease exists to prevent. The test also asserts the margin is positive, so setting `JOB_LEASE_MARGIN_SECONDS=0` cannot quietly re-open it.
- `test_every_new_key_is_present_in_env_example` — parses `.env.example` and asserts each new `UPPER_CASE` key appears. The file is the only documentation of what to set.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/test_config.py -v
```

Expected: `AttributeError` on each new field.

- [ ] **Step 4: Move `httpx` to the runtime dependencies**

In `pyproject.toml`, add `"httpx>=0.27",` to `[project].dependencies` (after `asyncpg`) and **remove** it from `[dependency-groups].dev` — it is no longer a test-only tool, and listing it twice invites the two specifiers to drift. Then:

```bash
uv lock
uv sync
```

Commit `uv.lock`. Comment the change in `pyproject.toml` at the new line:

```toml
    # Runtime, not dev: app/channels/whatsapp/client.py calls the Meta Cloud API
    # from the api image and the worker image. It was dev-only while the test
    # client was the only user.
    "httpx>=0.27",
```

- [ ] **Step 5: Add the settings to `app/config.py`**

After the existing Meta block:

```python
    # The send side of the Cloud API. Same empty-by-default reasoning as the two
    # keys above: an empty token means every send fails with a permanent,
    # visible error, which is the correct behaviour for "not configured".
    #
    # meta_phone_number_id is NOT what we send from (plan assumption A7). The
    # reply goes out on the phone_number_id the message ARRIVED on, read from
    # the stored payload, because with more than one clinic this setting is
    # simply the wrong number. It exists only for the single-tenant fallback
    # map in ConfigTenantResolver.from_settings().
    meta_access_token: str = ""
    meta_phone_number_id: str = ""
    # No known-good value lives anywhere in this repo; set it to whatever the
    # Meta dashboard shows. A retired version is a permanent 4xx, not a retry.
    meta_api_version: str = "v21.0"
    # Overridable so a local fake can stand in for Meta. Tests use
    # httpx.MockTransport instead and never touch the network.
    meta_api_base_url: str = "https://graph.facebook.com"
    # Hard rule 11: every external call has a timeout. One attempt per job try,
    # so this is the whole budget for one attempt.
    meta_send_timeout_seconds: float = 10.0

    # phone_number_id -> tenant uuid, as JSON: {"100000000000001": "…uuid…"}.
    # Deliberately a str and not a dict[str, str] (plan assumption A1):
    # pydantic-settings JSON-decodes complex fields, and .env.example ships keys
    # with empty values, so `WHATSAPP_TENANT_MAP=` would raise at import and stop
    # the app booting from its own example file. ConfigTenantResolver parses it,
    # and a broken map becomes a loud dead letter instead of a dead process.
    whatsapp_tenant_map: str = ""
    # The single-tenant fallback: used with meta_phone_number_id when the map
    # above is blank, so local work needs no hand-written JSON.
    dev_tenant_id: str = ""

    # Which inbound message types get a reply. Comma-separated for the same
    # reason the map is a str. Everything is STORED; this only gates replying.
    whatsapp_reply_to_types: str = "text"

    # Hard rule 11: bounded retries with backoff, then a dead letter.
    # Deferrals with these values are 5s, 10s, 20s, 40s, then the 5th failure
    # dead-letters. The job raises arq's Retry itself — arq does not retry a
    # plain exception (plan conflict note C13).
    job_max_tries: int = 5
    job_backoff_base_seconds: float = 5.0
    job_backoff_max_seconds: float = 300.0
    # Must stay above meta_send_timeout_seconds, or arq cancels a job mid-send
    # and every slow send becomes an ambiguous one.
    job_timeout_seconds: float = 60.0
    # Added to job_timeout_seconds to get the claim lease (plan note C3a). The
    # lease MUST outlive the job: if it expires while the job is still inside
    # the Meta call, a second worker claims the same event and sends the same
    # reply. 30s of slack covers a job that arq is in the middle of cancelling.
    job_lease_margin_seconds: float = 30.0

    @property
    def claim_lease_seconds(self) -> float:
        """How long a worker owns a webhook_inbox row once it has claimed it.

        Derived rather than configured, so the invariant "the lease outlives the
        job" cannot be broken by setting one of two independent knobs.
        """
        return self.job_timeout_seconds + self.job_lease_margin_seconds
```

- [ ] **Step 6: Extend the startup warning in `app/main.py`**

VS-003 warns when `META_APP_SECRET` or `META_VERIFY_TOKEN` is unset. Add `META_ACCESS_TOKEN` and "no tenant mapping configured" to the same warning, names only, never values.

- [ ] **Step 7: Add the new keys to `.env.example`**

Under the existing Meta block (existing keys untouched):

```bash
# graph.facebook.com by default. Point it at a local fake if you have one.
META_API_BASE_URL=
# One attempt per job try, in seconds.
META_SEND_TIMEOUT_SECONDS=

# Which WhatsApp number belongs to which clinic, as JSON:
#   WHATSAPP_TENANT_MAP={"100000000000001":"00000000-0000-0000-0000-000000000000"}
# Leave blank and DEV_TENANT_ID + META_PHONE_NUMBER_ID are used as a one-entry
# map instead. An unknown phone_number_id is a permanent failure: the event is
# dead-lettered, never guessed at (hard rule 4).
WHATSAPP_TENANT_MAP=
# Inbound message types that get a reply, comma-separated. Everything is stored
# regardless; this only gates replying. Default: text
WHATSAPP_REPLY_TO_TYPES=

# Worker retries (hard rule 11). Deferrals are base * 2^(try-1), capped.
JOB_MAX_TRIES=
JOB_BACKOFF_BASE_SECONDS=
JOB_BACKOFF_MAX_SECONDS=
JOB_TIMEOUT_SECONDS=
# Added to JOB_TIMEOUT_SECONDS to get the claim lease. Must be > 0: a lease that
# expires mid-send lets a second worker send the same reply.
JOB_LEASE_MARGIN_SECONDS=
```

- [ ] **Step 8: Run the tests, lint, format**

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format .
```

- [ ] **Step 9: Append the task entry to the report**

---

### Task 2: The schema this slice needs, and the repository methods that use it

One migration: the reply link, the claim lease, the widened modality CHECK, and nothing else. Plus the repository methods that make idempotency a database property rather than a sequence of checks.

**Files:**
- Modify: `app/db/enums.py` (`MessageModality.OTHER`, `STATUS_RANK`)
- Modify: `app/db/models/message.py` (`reply_to_message_id`, the unique constraint, the self-FK)
- Modify: `app/db/models/webhook_inbox.py` (`locked_until`)
- Create: `migrations/versions/<rev>_vs004_reply_link_inbox_lease_and_modality.py`
- Modify: `app/db/repositories/webhook_inbox.py` (`get_by_event_id`, `claim`, `release`, `ClaimResult`)
- Modify: `app/db/repositories/messages.py` (`reserve_reply`, `get_reply_to`, `attach_provider_id`, `advance_status`)
- Modify: `app/db/repositories/messages.py` and `app/db/repositories/conversations.py` (amendment A2 savepoints)
- Test: `tests/db/test_constraints.py` (+4), `tests/db/test_models.py` (+2), `tests/db/test_migrations.py` (+1), `tests/db/test_repositories.py` (+19), `tests/db/test_base.py` (the pinned modality list gains `OTHER`)

**Interfaces:**
- `MessageModality.OTHER = "OTHER"`; `STATUS_RANK: dict[MessageStatus, int]`
- `Message.reply_to_message_id: uuid.UUID | None`
- `WebhookInbox.locked_until: datetime | None`
- `WebhookInboxRepository.get_by_event_id(provider_event_id) -> WebhookInbox | None`
- `WebhookInboxRepository.claim(row_id: uuid.UUID, lease_seconds: float) -> ClaimResult` where `ClaimResult` is a frozen dataclass of `(state: Literal["claimed", "already_processed", "locked", "missing"], row: WebhookInbox | None)`
- `WebhookInboxRepository.release(row_id: uuid.UUID) -> None` — clears `locked_until`
- `MessageRepository.reserve_reply(conversation_id, reply_to_message_id, text) -> Message` (returns the existing row when one is already there)
- `MessageRepository.get_reply_to(reply_to_message_id) -> Message | None`
- `MessageRepository.attach_provider_id(message_id, provider_message_id) -> None`
- `MessageRepository.advance_status(provider_message_id, status) -> bool`

**Expected tests after this task: 161** (actual: 168 — the amendments brought their own tests)**.**

`claim()` takes the **row UUID**, not `provider_event_id` (C2). `get_by_event_id` still exists, and has exactly one caller: the webhook's duplicate path, which needs the row id of an item `store_if_new` refused to insert.

- [ ] **Step 1: Write the failing constraint and model tests**

`tests/db/test_constraints.py`:
- `test_other_is_an_accepted_modality` — the runtime half of widening the CHECK, as `app/db/enums.py` prescribes.
- `test_an_unknown_modality_is_still_rejected` — proves the CHECK was widened, not dropped.
- `test_one_inbound_message_can_have_only_one_reply` — two outbound rows with the same `reply_to_message_id` → `IntegrityError` naming `uq_messages_reply_to_message_id`.
- `test_many_messages_can_have_a_null_reply_link` — the point of a nullable unique column; without this test a later "tidy-up" could make the column `NOT NULL` and break every inbound insert.

`tests/db/test_models.py`:
- `test_the_reply_link_points_at_a_message` — the FK is to `messages.id`.
- `test_status_rank_covers_every_message_status` — iterate `MessageStatus`; a new status without a rank must fail here rather than silently rank 0 and allow a backwards move.

- [ ] **Step 2: Write the failing repository tests**

`tests/db/test_repositories.py`, all `@pytest.mark.db`:
- `test_claiming_a_received_row_returns_it_and_marks_it_processing`
- `test_claiming_increments_attempts` — the attempt count is what the dead letter records.
- `test_claiming_a_processed_row_reports_already_processed` — the worker-side idempotency gate.
- `test_claiming_a_processing_row_with_no_lease_succeeds` — a crashed previous try must be reclaimable; this is why the guard is `<> 'PROCESSED'` and not `= 'RECEIVED'`.
- `test_claiming_a_missing_row_reports_missing`
- `test_claiming_sets_a_lease_in_the_future` — asserts `locked_until > now()` and that it was computed by the **database**, not by Python: set the value through `claim()` and compare it against `select now()` in the same connection, so a skewed worker clock cannot decide how long it owns a row (A16).
- `test_a_row_with_a_live_lease_is_not_claimable` — the state is `locked`, and `attempts` is **not** incremented: another worker's try is not this job's try.
- `test_a_row_with_an_expired_lease_is_claimable` — set `locked_until` to a past value; the claim succeeds. Without this, one `SIGKILL` would strand a patient's message forever.
- `test_releasing_clears_the_lease_without_touching_the_status` — the release runs on the retry path, where the row must stay `PROCESSING`.
- `test_marking_a_row_processed_also_clears_the_lease` — `mark()` clears `locked_until` in the same `UPDATE`. Every caller of it is a worker finishing with the row, and a lease left on a finished row is pure delay for whoever touches it next.
- `test_get_by_event_id_finds_the_row_the_webhook_could_not_insert` — the webhook's duplicate path (C2).
- `test_the_session_still_works_after_a_duplicate_message_insert` — **amendment A2**, standalone: catch `DuplicateRecordError`, then re-read the existing row on the same session. Without the savepoint in `MessageRepository.add` the re-read raises `InFailedSqlTransaction`, and VS-004's job is built on exactly that re-read.
- `test_the_session_still_works_after_a_conversation_race` — amendment A2 for the path that deliberately keeps raising.
- `test_two_concurrent_claims_of_one_row_yield_one_claimed_and_one_locked` — uses `second_session_factory` (VS-002 added it for exactly this) so the two claims are on genuinely independent connections and cannot see each other's uncommitted rows. **This is C3a's bug, proven fixed at the layer that fixes it.**
- `test_reserving_a_reply_twice_returns_the_same_row` — the second call returns the first row, and no `IntegrityError` escapes (the conflicting values would be patient content).
- `test_attaching_a_provider_id_marks_the_reply_sent_with_a_timestamp`
- `test_a_status_only_moves_forward` — `SENT` → `DELIVERED` → `READ` advances; `READ` → `DELIVERED` returns `False` and leaves `READ`.
- `test_advancing_a_status_for_another_tenant_changes_nothing` — the tenant filter is in the statement, not in the caller.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
docker compose up -d postgres
uv run pytest tests/db -v
```

- [ ] **Step 4: Widen `MessageModality` and add the rank table**

`app/db/enums.py`:

```python
class MessageModality(StrEnum):
    TEXT = "TEXT"
    VOICE_NOTE = "VOICE_NOTE"
    # Anything else Meta can send: image, document, location, sticker, contact,
    # interactive reply. Requirement "store all inbound message types" needs a
    # value for them, and inventing one per type would be a CHECK migration per
    # Meta feature. The raw type stays readable in webhook_inbox.payload.
    OTHER = "OTHER"
```

```python
# How far along a message is. A status only ever moves to a HIGHER rank
# (requirement 5), enforced in the UPDATE's WHERE clause so there is no
# read-then-write race.
#
# FAILED sits above SENT and below DELIVERED on purpose: a send Meta later
# reports as failed must overwrite SENT, while a message that was delivered did
# not fail. RECEIVED and QUEUED share rank 0 - neither is ever advanced by a
# status callback; RECEIVED is inbound-only and QUEUED becomes SENT by the send
# itself.
STATUS_RANK: dict[MessageStatus, int] = {
    MessageStatus.RECEIVED: 0,
    MessageStatus.QUEUED: 0,
    MessageStatus.SENT: 1,
    MessageStatus.FAILED: 2,
    MessageStatus.DELIVERED: 3,
    MessageStatus.READ: 4,
}
```

- [ ] **Step 4b: Add the lease column to `WebhookInbox`**

`app/db/models/webhook_inbox.py`:

```python
    # A worker's time-limited claim on this row (plan note C3a).
    #
    # `status` alone cannot serialise two concurrent runs of one event. It must
    # let a PROCESSING row be reclaimed - a try that died between the job's two
    # commits left it PROCESSING, and refusing that row would mean the patient's
    # message is never answered - so PROCESSING cannot also mean "someone is
    # working on it". The only thing that separates a dead try from a live one is
    # time, which is what this column holds.
    #
    # Set to now() + job_timeout + margin by claim(), always by PostgreSQL's
    # clock, and cleared on every exit from the job.
    locked_until: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
```

- [ ] **Step 5: Add the reply link to the model**

`app/db/models/message.py`, in `__table_args__`:

```python
        # One reply per inbound message, decided by the database (requirement 3).
        # Nullable AND unique is exactly right here: PostgreSQL permits many
        # NULLs under a unique constraint, so every inbound row and every future
        # non-reply outbound row is unaffected, while two jobs cannot both
        # create a reply to the same message. This is what makes a retried job
        # safe without the job checking first.
        sa.UniqueConstraint("reply_to_message_id", name="uq_messages_reply_to_message_id"),
```

and as a column:

```python
    # The inbound message this outbound message answers. NULL on every inbound
    # row, and on any outbound message that is not a reply.
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
    )
```

- [ ] **Step 6: Write the migration by hand**

```bash
docker compose exec api alembic revision -m "vs004 reply link inbox lease and modality"
```

Autogenerate will find the column and the unique constraint but **not** the CHECK change — `app/db/enums.py` says so ("Alembic's autogenerate never compares CHECK constraints"). Write all of it by hand, and make `downgrade()` real:

```python
def upgrade() -> None:
    # The claim lease (plan note C3a). No index: every read of it is by
    # webhook_inbox.id or provider_event_id, both already unique-indexed.
    op.add_column(
        "webhook_inbox", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("messages", sa.Column("reply_to_message_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_messages_reply_to_message_id_messages",
        "messages", "messages", ["reply_to_message_id"], ["id"], ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_messages_reply_to_message_id", "messages", ["reply_to_message_id"]
    )
    # Widen the modality vocabulary (MessageModality.OTHER). A swapped CHECK is
    # fully reversible, which is why this project uses VARCHAR + CHECK instead of
    # a native ENUM: ALTER TYPE ... ADD VALUE has no downgrade.
    op.drop_constraint("ck_messages_modality_valid", "messages", type_="check")
    op.create_check_constraint(
        "modality_valid", "messages", "modality IN ('TEXT', 'VOICE_NOTE', 'OTHER')"
    )
```

`downgrade()` reverses all five, and restores the two-value CHECK. Note in a comment that the downgrade fails if any row is already `OTHER` — which is correct: silently rewriting patient data to make a downgrade succeed would be worse than a loud failure.

Then, in `tests/db/test_migrations.py`, extend the existing round-trip test to cover this revision (`upgrade head` → `downgrade -1` → `upgrade head`).

- [ ] **Step 7: Add `get_by_event_id`, `claim` and `release` to `WebhookInboxRepository`**

```python
@dataclass(frozen=True)
class ClaimResult:
    """What claiming an inbox row found.

    Four states, not a bool, because the caller treats each differently:
    `already_processed` is a success (someone else finished the work), `locked`
    and `missing` are retryable for different reasons, and only `claimed`
    carries a row.
    """

    state: Literal["claimed", "already_processed", "locked", "missing"]
    row: WebhookInbox | None = None
```

```python
    async def claim(self, row_id: uuid.UUID, lease_seconds: float) -> ClaimResult:
        """Take a time-limited lease on an event, or say why we could not.

        One conditional UPDATE, not SELECT-then-UPDATE: two workers can run the
        same job at the same instant (an at-least-once queue guarantees nothing
        else), and both would see the same row. The database decides, exactly as
        store_if_new lets it decide the webhook's race.

        Two guards, and both are load-bearing:

        `status <> 'PROCESSED'` and NOT `status = 'RECEIVED'`. A previous try that
        died between the job's two commits left the row PROCESSING, and that row
        MUST be reclaimable or the patient's message is never answered.
        PROCESSED is the only status that means "do not touch".

        `locked_until IS NULL OR locked_until < now()`. This is the part status
        cannot do (plan note C3a): because PROCESSING has to stay claimable for a
        DEAD try, it cannot also mean "a LIVE try owns this" - and a second
        concurrent run that claims a live PROCESSING row finds the same reserved
        reply row with no wamid and sends the same reply. The reply row's unique
        constraint does not help: it prevents two reply ROWS, not two sends from
        one row. Only time separates the dead try from the live one.

        now() is PostgreSQL's, evaluated server-side in this statement, so a
        worker with a skewed clock cannot grant itself a longer lease (A16).

        attempts is incremented only on a successful claim: another worker's
        attempt is not this job's attempt, and a dead letter's count must be the
        number of times WE tried.
        """
        lease = sa.func.now() + sa.cast(
            sa.func.make_interval(0, 0, 0, 0, 0, 0, lease_seconds), sa.Interval
        )
        statement = (
            sa.update(WebhookInbox)
            .where(
                WebhookInbox.id == row_id,
                WebhookInbox.status != InboxStatus.PROCESSED.value,
                sa.or_(
                    WebhookInbox.locked_until.is_(None),
                    WebhookInbox.locked_until < sa.func.now(),
                ),
            )
            .values(
                status=InboxStatus.PROCESSING.value,
                attempts=WebhookInbox.attempts + 1,
                locked_until=lease,
            )
            .returning(WebhookInbox)
        )
        claimed = (await self._session.execute(statement)).scalar_one_or_none()
        if claimed is not None:
            return ClaimResult("claimed", claimed)
        # Nothing updated. Three reasons, and the caller needs to tell them
        # apart, so read the row once - only on this unusual path.
        existing = await self.get(row_id)
        if existing is None:
            return ClaimResult("missing")
        if existing.status == InboxStatus.PROCESSED.value:
            return ClaimResult("already_processed")
        return ClaimResult("locked")
```

`release(row_id)` is a one-line `UPDATE … SET locked_until = NULL`, deliberately **not** touching `status`: it runs on the retry path, where the row must stay `PROCESSING`.

`get(row_id)` is a plain primary-key read, added alongside. `get_by_event_id(provider_event_id)` is the lookup on the unique index, with **one caller only** — the webhook's duplicate path (Task 5, Step 6).

The interval expression is worth a comment in the code: `make_interval` with a float seconds argument keeps the lease arithmetic in SQL, so the whole claim stays one statement and one clock. A Python-side `timedelta` would work too, but it would be the worker's clock deciding when its own lease ends (A16).

- [ ] **Step 8: Add the four methods to `MessageRepository`**

`reserve_reply` uses `pg_insert(...).on_conflict_do_nothing(constraint="uq_messages_reply_to_message_id").returning(...)` and falls back to `get_reply_to` when nothing came back — the same shape, and the same reason, as `ContactRepository.get_or_create_by_identity`: catching the `IntegrityError` instead would put a row's values on the stack, and those values are the reply text and the conversation ids (hard rule 8).

```python
    async def reserve_reply(
        self, conversation_id: uuid.UUID, reply_to_message_id: uuid.UUID, text: str
    ) -> Message:
        """Claim the right to reply to one inbound message.

        Returns the reserved row, or the row somebody else already reserved.
        The caller then looks at its provider_message_id: set means the reply is
        already out and must not be sent again (requirement 3).

        Written and COMMITTED before the Meta call, so that a crash during the
        send leaves a durable record that a reply is in flight. See "Commit
        boundaries" in docs/plans/VS-004-plan.md.
        """
```

`attach_provider_id(message_id, provider_message_id)` sets `provider_message_id`, `status=SENT` and `sent_at=now()` in one `UPDATE`.

`advance_status(provider_message_id, status) -> bool` builds the rank guard with `sa.case` over `STATUS_RANK` and returns `result.rowcount == 1`:

```python
    async def advance_status(self, provider_message_id: str, status: MessageStatus) -> bool:
        """Move a message forward, never backwards (requirement 5).

        The rank comparison is in the WHERE clause, not in Python, so two status
        callbacks arriving at once cannot interleave a read and a write and lose
        one. Returns True when the row moved; False means either "already at or
        past this status" (fine) or "no such message for this tenant" - the
        caller has already established which, because it looked the message up
        first to decide whether the status was simply early.
        """
```

- [ ] **Step 9: Apply the migration, run the tests, lint, format**

```bash
docker compose exec api alembic upgrade head
uv run pytest -q
uv run ruff check . && uv run ruff format .
```

- [ ] **Step 10: Append the task entry to the report**

---

### Task 3: The tenant resolver

Hard rule 4, in one small module with one parse site, so that the type question VS-002 left open (C11) has exactly one place to be answered later.

**Files:**
- Create: `app/tenants/__init__.py`, `app/tenants/resolver.py`
- Create: `tests/tenants/__init__.py`, `tests/tenants/test_resolver.py` (10 new)

**Interfaces:**
- `TenantId = uuid.UUID`
- `class UnknownPhoneNumberError(Exception)` — carries the `phone_number_id` only
- `class TenantMapError(Exception)` — the configured map could not be read
- `@runtime_checkable class TenantResolver(Protocol): def resolve(self, phone_number_id: str) -> TenantId: ...`
- `class ConfigTenantResolver` with `__init__(mapping: Mapping[str, TenantId])`, `from_settings(settings) -> ConfigTenantResolver`, and `resolve()`

**Expected tests after this task: 171** (actual: 182 — four more resolver tests than listed, and Task 2's overshoot carried forward)**.**

- [ ] **Step 1: Write the failing resolver tests**

- `test_a_known_phone_number_id_resolves_to_its_tenant`
- `test_an_unknown_phone_number_id_raises_rather_than_guessing` — hard rule 4: there is no default tenant. Guessing would put one clinic's patient in another clinic's inbox.
- `test_an_empty_phone_number_id_raises`
- `test_the_map_is_read_from_json_in_settings`
- `test_a_malformed_map_raises_tenant_map_error_at_construction_not_at_import`
- `test_a_map_entry_whose_value_is_not_a_uuid_raises_tenant_map_error`
- `test_the_dev_fallback_builds_a_one_entry_map` — A2: `DEV_TENANT_ID` + `META_PHONE_NUMBER_ID` with a blank map.
- `test_an_explicit_map_wins_over_the_dev_fallback`
- `test_no_map_and_no_fallback_resolves_nothing` — an empty resolver, and every event dead-letters. Not a crash (A1).
- `test_the_resolver_never_logs_a_tenant_map_value` — `caplog`: the startup line names the entry count and the config source, never a uuid or a number.
- `test_a_map_that_is_not_an_object_raises` — valid JSON of the wrong shape (`["a","b"]`) is a different failure from invalid JSON, and `.items()` on a list would be an `AttributeError` escaping as a crash rather than a `TenantMapError`.
- `test_a_dev_tenant_id_without_a_phone_number_id_resolves_nothing` — half the A2 fallback is not the fallback; guessing which number the lone tenant belongs to is the same mistake as a default tenant.
- `test_a_broken_map_does_not_name_the_offending_value` — the error carries `bad_tenant_map` and nothing else.
- `test_the_config_resolver_satisfies_the_protocol` — `runtime_checkable`, so a worker-test fake and the real resolver cannot drift.

- [ ] **Step 2: Run the tests to verify they fail**

- [ ] **Step 3: Write `app/tenants/resolver.py`**

Key comments to carry in the module:

```python
# The one place this repo names the tenant id type. VS-002 left it unconfirmed
# (plan conflict note C11) and every table has it as sa.Uuid; if it ever becomes
# something else, this alias and one migration are the change, not every
# signature in app/worker/.
TenantId = uuid.UUID
```

```python
class TenantResolver(Protocol):
    """phone_number_id -> tenant. A Protocol, not a base class, so a test can
    pass a lambda-sized fake and the worker never imports the config path.

    Hard rule 4: this is the ONLY way a tenant_id enters the system. It is never
    an argument the LLM can supply, never a webhook field, never a job argument.
    """
```

`ConfigTenantResolver.resolve` raises `UnknownPhoneNumberError(phone_number_id)` on a miss. `from_settings` parses JSON, converts values with `uuid.UUID(...)`, raises `TenantMapError("bad_tenant_map")` on either failure — a short code, never the offending value — and logs one line: `tenant map loaded source=%s entries=%d`.

- [ ] **Step 4: Run the tests, lint, format**

- [ ] **Step 5: Append the task entry to the report**

---

### Task 4: The Meta client, and result classification in one place

One attempt, one timeout, one classifier. Requirement 4's "keep this classification in one place" is the whole design of this module: the job asks *what kind of result* it got and never looks at a status code.

**Files:**
- Create: `app/channels/whatsapp/client.py`
- Create: `app/channels/whatsapp/redact.py`
- Create: `tests/channels/test_meta_client.py` (12 new), `tests/channels/test_redact.py` (4 new)

**Interfaces:**
- `class SendOutcome(StrEnum): SUCCESS | RETRYABLE | PERMANENT`
- `@dataclass(frozen=True) class SendResult: outcome: SendOutcome; provider_message_id: str | None; reason: str`
- `classify_status(status_code: int) -> SendOutcome`
- `classify_exception(error: Exception) -> SendOutcome | None` (`None` = not ours, let it escape)
- `class MetaClient: __init__(http: httpx.AsyncClient, settings: Settings); async def send_text(self, phone_number_id: str, to: str, text: str) -> SendResult`
- `app/channels/whatsapp/redact.py`: `scrub(value: str) -> str`, `error_reason(status_code: int, body: bytes | None) -> str`

**Expected tests after this task: 187** (actual: 216 — the status classifications are parametrised, so each code is its own test)**.**

- [ ] **Step 1: Write the failing redaction tests**

- `test_a_long_digit_run_is_redacted` — `scrub` replaces runs of 7 or more digits with `[redacted]`, so a phone number in any string cannot survive. Short numbers (status codes, error codes) pass through.
- `test_an_error_reason_is_built_from_codes_only` — a real-shaped Meta error body (`{"error": {"message": "…+96170123456…", "type": "OAuthException", "code": 131026, "error_subcode": 123}}`) produces `http_400 code_131026 subcode_123` and nothing else.
- `test_an_error_reason_never_contains_the_message_field` — the specific leak requirement 6 names: Meta's `error.message` can quote the recipient's number.
- `test_an_unparseable_error_body_still_produces_a_reason` — `http_502`, no exception. A sanitiser that raises on junk is a sanitiser that gets bypassed.
- `test_short_numbers_survive` and `test_a_seven_digit_error_subcode_survives_intact` — **the plan's first draft had this wrong.** It said to pass the assembled reason through `scrub` "as belt and braces". Meta's `error_subcode` values are seven digits (`2494010` is "recipient not in the allowed list", the most likely failure on a test number), so the digit run this module redacts would eat the single most useful code in the reason. `error_reason` therefore does **not** scrub: every part of it is built from an `int` that passed an `isinstance` check, and the module's actual job is making sure no Meta-supplied *string* ever gets that far. `scrub` stays exported and tested for the case it is genuinely for.

- [ ] **Step 2: Write the failing client tests**

All with `httpx.MockTransport` — no network, ever.

- `test_a_successful_send_returns_the_wamid` — 200 with `{"messages": [{"id": "wamid.…"}]}`.
- `test_the_send_url_uses_the_phone_number_id_it_was_given` — A7: the arriving number, not the setting.
- `test_the_request_carries_the_bearer_token_and_the_whatsapp_shape` — `messaging_product: "whatsapp"`, `type: "text"`, `text.body`, `to`.
- `test_the_client_makes_exactly_one_attempt_for_a_500` — counts requests through the transport. The single most important test in the module: a retry loop here would make backoff, `max_tries` and the dead letter all lie (requirement 4).
- `test_a_500_is_retryable`, `test_a_429_is_retryable`, `test_a_timeout_is_retryable`, `test_a_transport_error_is_retryable`
- `test_a_400_is_permanent`, `test_a_401_is_permanent` — a bad token is not a thing retries fix.
- `test_a_2xx_with_no_message_id_is_success_with_no_wamid` — the `sent_without_id` branch; asserts the outcome is `SUCCESS`, because retrying would duplicate the message Meta already accepted.
- `test_no_log_line_contains_the_access_token_the_recipient_or_the_text` — `caplog` over a failing send: neither the token, nor `to`, nor the message body appears; `phone_number_id` does, and is what makes the line useful.
- `test_an_error_that_is_not_ours_escapes` — `classify_exception` returns `None` for a `ValueError`, so a bug in our own serialisation raises instead of being retried five times and dead-lettered as if Meta had been unreachable.
- `test_a_2xx_with_an_unreadable_body_is_success_with_no_wamid` — the other half of `accepted_without_id`.
- `test_the_worker_package_contains_no_http_status_literals` — Review Focus 6 asserted against the source, because a second opinion creeping into a handler (`if response.status_code == 429`) is invisible to any behavioural test until the two disagree.

- [ ] **Step 3: Run the tests to verify they fail**

- [ ] **Step 4: Write `app/channels/whatsapp/redact.py`**

```python
_DIGIT_RUN = re.compile(r"\d{7,}")


def scrub(value: str) -> str:
    """Replace long digit runs, so no phone number survives in a string we keep.

    Hard rule 8, applied at the boundary rather than trusted upstream. Meta's
    error.message quotes the recipient's number often enough that "we only ever
    store codes" needs a second line of defence: one future call site that
    formats a Meta string into a reason would otherwise leak silently.

    Seven digits, not ten: E.164 numbers appear with and without country codes
    and with punctuation stripped. A genuine seven-digit identifier being
    redacted costs a log line's readability; a number surviving costs a rule.
    """
```

`error_reason(status_code, body)` returns `http_<status>` plus `code_<n>` and `subcode_<n>` when they are integers in `body["error"]`, and never reads `error.message` or `error.error_user_msg` at all. It does **not** apply `scrub` — see the test entry above.

- [ ] **Step 5: Write `app/channels/whatsapp/client.py`**

```python
def classify_status(status_code: int) -> SendOutcome:
    """The single place an HTTP status becomes a retry decision (requirement 4).

    Retryable: 5xx (Meta had a problem), 429 (Meta asked us to slow down).
    Permanent: every other 4xx - a bad token, a bad number, a bad template, a
    retired API version. None of those change because we ask again, and retrying
    them burns the rate limit that the 429 path needs.

    Kept here, and nowhere else, so that "is a 429 retryable?" has one answer in
    this codebase. A test asserts app/worker/ contains no status-code literals.
    """
    if status_code >= 500 or status_code == 429:
        return SendOutcome.RETRYABLE
    if status_code >= 400:
        return SendOutcome.PERMANENT
    return SendOutcome.SUCCESS
```

`classify_exception` maps `httpx.TimeoutException` and `httpx.TransportError` to `RETRYABLE` and returns `None` for anything else, so a genuine bug escapes instead of being silently retried five times.

`MetaClient.send_text`:

```python
    async def send_text(self, phone_number_id: str, to: str, text: str) -> SendResult:
        """One attempt. Never a retry (requirement 4, hard rule 11).

        Retries live in the job envelope, where the backoff, the try count and
        the dead letter are. A loop in here would be invisible to all three: five
        internal attempts inside five job tries is twenty-five sends, and the
        "attempts" column would say 5.

        Never raises for a Meta-side failure: it returns a classified SendResult,
        because "what kind of failure was this" is a decision with one home and
        an exception type is a worse way to carry it. A bug in OUR code still
        raises and still escapes.
        """
```

The URL is `f"{base_url}/{api_version}/{phone_number_id}/messages"`. Every log line here carries `phone_number_id` and the reason code; never `to`, never `text`, never the token.

- [ ] **Step 6: Run the tests, lint, format**

- [ ] **Step 7: Append the task entry to the report**

---

### Task 5: The enqueue interface, the arq queue, and the webhook seam

**Files:**
- Create: `app/queue/interface.py`, `app/queue/arq_queue.py`
- Modify: `app/queue/__init__.py` (re-export), `app/main.py` (close the pool on shutdown)
- Modify: `app/api/whatsapp.py` (the seam — the only change to this file in the slice)
- Create: `tests/queue/__init__.py`, `tests/queue/test_arq_queue.py` (6 new)
- Create: `tests/api/test_whatsapp_enqueue.py` (7 new)

**Interfaces:**
- `class JobQueue(Protocol): async def enqueue_inbox_event(self, row_id: uuid.UUID) -> None: ...`
- `class EnqueueError(Exception)`
- `INBOX_JOB_NAME = "process_inbox_event"`, `def inbox_job_id(row_id: uuid.UUID) -> str` returning `f"inbox:{row_id}"`
- `class ArqJobQueue(JobQueue)`, `async def get_job_queue() -> JobQueue` (the FastAPI dependency), `async def close_job_queue() -> None`
- `class FakeJobQueue(JobQueue)` in `tests/queue/fakes.py` — records row ids, or raises on demand

**Expected tests after this task: 204** (actual: 234)**.**

- [ ] **Step 1: Write the failing queue tests**

- `test_the_job_id_is_the_row_id_under_an_inbox_prefix` — `_job_id == f"inbox:{row_id}"` (requirement 1: enqueueing twice is harmless; the prefix namespaces our keys inside a Redis that also holds arq's own).
- `test_enqueueing_passes_the_row_id_and_nothing_else` — the job args are exactly `(str(row_id),)`. Requirement 1 and hard rule 8: no payload, no phone number, no text.
- `test_no_job_argument_or_job_id_contains_a_wamid` — **C2 made executable.** Enqueue for a row whose `provider_event_id` is `msg:<wamid>`, and assert the wamid appears in neither the job id nor the arguments. A wamid is base64 and decodes to include the patient's phone number, so it is patient content, and Redis is one more place it must not reach.
- `test_a_duplicate_job_id_is_not_an_error` — arq returns `None` when the id is already queued; that is the dedup working, not a failure.
- `test_a_redis_failure_becomes_an_enqueue_error` — so the endpoint never has to know what a redis exception looks like.
- `test_the_pool_is_built_from_redis_url` — hard rule 9.
- `test_the_queue_satisfies_the_protocol` — `isinstance(ArqJobQueue(...), JobQueue)` with a `runtime_checkable` Protocol, so the fake and the real one cannot drift.

- [ ] **Step 2: Write the failing webhook tests**

`tests/api/test_whatsapp_enqueue.py`, using `FakeJobQueue` through a dependency override:

- `test_one_message_enqueues_one_job_carrying_its_row_id` — the enqueued id matches the `webhook_inbox.id` that was just written.
- `test_three_messages_and_two_statuses_enqueue_five_jobs`
- `test_a_redelivered_event_that_is_already_stored_is_enqueued_again` — **C8**, and requirement 1's explicit instruction: POST the same body twice, assert one inbox row and **two** enqueue calls. The second call must carry the **same row id as the first** — which is the whole point of the duplicate-path lookup: the row already exists, so its id is the one already stored, and arq's job id therefore matches and suppresses the repeat.
- `test_the_row_id_for_a_duplicate_is_looked_up_not_invented` — the duplicate path's lookup, isolated: POST twice, assert the two enqueued ids are equal. A `uuid4()` fallback here would silently defeat both arq's dedup and the lease.
- `test_the_duplicate_lookup_runs_only_for_duplicates` — a POST whose items are all new issues no `get_by_event_id`. One query on the unusual path, none on the normal one.
- `test_nothing_is_enqueued_when_there_is_nothing_to_store` — an unmodelled payload still answers 200 and touches neither the database nor the queue.
- `test_an_enqueue_failure_answers_503_and_does_not_lose_the_rows` — the rows stay committed (they are, and the redelivery will re-enqueue), and 503 is retryable so Meta comes back. **C1.**
- `test_the_enqueue_failure_log_carries_row_ids_and_no_wamid` — **C2**: the failure line names row UUIDs, not the `provider_event_id` values the request just stored.
- `test_the_enqueue_happens_after_the_commit` — asserted as an **ordering**, by spying on the session's `commit`, not by reading the row from a second connection. The plan's first draft said to do the latter, and it cannot work: `use_database` wraps the test in one transaction that is rolled back, with `join_transaction_mode="create_savepoint"`, so the endpoint's commit releases a savepoint and is *by design* invisible to any other connection. A visibility check there fails against correct code.
- `test_no_log_line_from_the_enqueue_path_contains_patient_content` — `caplog`, against `PATIENT_TEXT`, `phone()` **and `wamid()`**.

- [ ] **Step 3: Run the tests to verify they fail**

- [ ] **Step 4: Write `app/queue/interface.py`**

```python
@runtime_checkable
class JobQueue(Protocol):
    """Enqueue, behind an interface (CLAUDE.md: the queue may move to SQS later).

    One method, one argument, and that argument is OUR row id - never Meta's
    event id (plan note C2). The narrowness is the interface's whole value: SQS,
    arq and a test double can all satisfy it, and hard rule 8 is satisfied by the
    signature itself. There is no parameter a payload could be smuggled through,
    and no parameter that can carry a wamid.
    """

    async def enqueue_inbox_event(self, row_id: uuid.UUID) -> None: ...
```

- [ ] **Step 5: Write `app/queue/arq_queue.py`**

`ArqJobQueue` holds a lazily created `arq.create_pool(RedisSettings.from_dsn(settings.redis_url))` — arq needs its own pool, separate from `app/queue/redis.py`'s client (which readiness uses); both read `REDIS_URL`. `enqueue_inbox_event` wraps `enqueue_job` and translates `redis.RedisError` / `OSError` into `EnqueueError`, logging the class name only.

```python
        job = await pool.enqueue_job(
            INBOX_JOB_NAME, str(row_id), _job_id=inbox_job_id(row_id)
        )
        if job is None:
            # arq returns None when a job with this id is queued, running, or
            # has a kept result: the same event is already on its way. A success,
            # and the reason the webhook can re-enqueue a redelivery blindly.
            #
            # It is a SHORT-LIVED key - results expire - which is why the worker
            # also checks webhook_inbox.status. See "Idempotency: the four keys".
            logger.debug("inbox job already queued event_id=%s", row_id)
```

The row id is passed as `str(row_id)` rather than the `UUID` object: arq serialises job arguments with pickle by default, and a plain string is the least surprising thing to find in Redis and the easiest to keep stable if the serialiser is ever changed. The job parses it back with `uuid.UUID(...)`.

`app/main.py`'s shutdown calls `close_job_queue()` next to `close_redis()` and `dispose_engine()`.

- [ ] **Step 6: Replace the VS-004 seam in `app/api/whatsapp.py`**

Add `queue: Annotated[JobQueue, Depends(get_job_queue)]` to `receive()`. The storage loop changes to collect **row ids**, not event ids: `store_if_new` already returns the row it inserted, so the normal path costs nothing extra, and only a duplicate needs a lookup.

```python
    row_ids: list[uuid.UUID] = []
    try:
        for item in items:
            row = await inbox.store_if_new(item.provider_event_id, item.payload)
            if row is not None:
                row_ids.append(row.id)
            else:
                # Already stored - a Meta redelivery. We still need this row's id,
                # because it is still going to be enqueued (see the seam below),
                # and store_if_new returns None on conflict. One SELECT on the
                # unique index, on the duplicate path only.
                #
                # NOT a freshly generated id: the whole point of enqueueing by row
                # id is that the id is stable across redeliveries, which is what
                # lets arq's job id suppress the repeat and the lease serialise
                # two workers.
                existing = await inbox.get_by_event_id(item.provider_event_id)
                if existing is not None:
                    row_ids.append(existing.id)
        await session.commit()
```

The existing "stored" log line **keeps printing `provider_event_id` values, unchanged.** That was reconsidered during execution and left alone deliberately: `tests/api/test_webhook_logging.py::test_a_stored_webhook_logs_event_ids_and_never_content` asserts those wamids are present, so narrowing the line means changing a merged slice's tested log contract — which is exactly what C2a says is *not* this slice's work. `new=` is now counted explicitly rather than derived from the id list. Every line VS-004 *adds* uses the row id, so the slice adds nothing to the problem, and the line carries a comment saying so.

Then, at the seam:

```python
    # --- enqueue (VS-004) --------------------------------------------------
    # After the commit, always: a job that started before it would find no row.
    #
    # One job per EXTRACTED item, not per newly stored item. A redelivery whose
    # row already exists is enqueued again on purpose: the reason Meta is
    # redelivering may be that our first enqueue is exactly what failed, and
    # "already stored" would then mean "never answered". Three things make the
    # repeat harmless - the row id is the same one as last time, arq refuses a
    # job id it already holds, and the worker skips an event already PROCESSED.
    #
    # The argument is OUR webhook_inbox row id and nothing else (hard rule 8,
    # plan note C2). Never the provider_event_id: a wamid is base64 and decodes
    # to include the patient's phone number, and a status event id carries the
    # wamid of the message we sent TO the patient. Redis, the job arguments, the
    # retry log lines and the dead letters all stay free of it.
    try:
        for row_id in row_ids:
            await queue.enqueue_inbox_event(row_id)
    except EnqueueError as error:
        logger.error(
            "whatsapp webhook enqueue failed error=%s event_ids=%s",
            type(error).__name__,
            ",".join(str(row_id) for row_id in row_ids),
        )
        # Not 200. The rows are committed, but nothing will ever process them,
        # and 200 tells Meta to forget the event - the same silent loss the
        # storage path's 503 exists to prevent, one layer further in. Meta
        # redelivers, the rows dedupe, their ids are looked up again, and the
        # enqueue is tried again.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="queue unavailable"
        ) from None
    # ----------------------------------------------------------------------
```

Hard rule 1 still holds: this awaits a Redis `RPUSH`-equivalent and nothing else. The one added `SELECT` is inside the existing transaction, on a unique index, on the duplicate path only.

- [ ] **Step 7: Run the tests, lint, format**

- [ ] **Step 8: Append the task entry to the report**

---

### Task 6: The job envelope — claim, dispatch, retry, dead letter

The machinery, with both handlers stubbed. Doing it before the handlers means retry and dead-letter behaviour is tested against a stub that fails on demand, not inferred from a real path.

**Files:**
- Create: `app/worker/errors.py`, `app/worker/retrying.py`, `app/worker/jobs/__init__.py`, `app/worker/jobs/inbox.py`
- Modify: `app/worker/main.py` (register the job, build the context, settings-driven `max_tries`)
- Modify: `app/db/models/dead_letter.py` (docstring only — C7)
- Create: `tests/worker/__init__.py`, `tests/worker/conftest.py`, `tests/worker/test_backoff.py` (4 new), `tests/worker/test_inbox_job.py` (8 new)

**Interfaces:**
- `class RetryableJobError(Exception)` / `class PermanentJobError(Exception)`, each `__init__(reason: str)` with a short safe code
- `def backoff_seconds(job_try: int, settings: Settings) -> float`
- `async def process_inbox_event(ctx: dict[str, Any], row_id: str) -> str`
- `def dead_letter_payload(row_id, kind, phone_number_id, job_try) -> dict[str, Any]`
- `WorkerSettings.functions` gains `func(process_inbox_event, max_tries=…, timeout=…)`

**Expected tests after this task: 221** (actual: 257)**.**

- [ ] **Step 1: Write the failing backoff tests**

- `test_the_backoff_curve_is_five_ten_twenty_forty` — with the documented defaults, pinning A9's series so a later change to `job_backoff_base_seconds` is a visible decision.
- `test_the_backoff_is_capped`
- `test_the_first_try_waits_the_base_delay` — `job_try=1` is the first *failure*, so it waits `base`, not `0`.
- `test_a_zero_or_negative_job_try_is_treated_as_the_first` — defensive: `ctx` comes from arq, and a 0 would otherwise produce a half-second retry storm.

- [ ] **Step 2: Write the failing envelope tests**

`tests/worker/test_inbox_job.py`, with both handlers monkeypatched to stubs:

- `test_a_processed_event_is_skipped_without_calling_a_handler` — requirement 1's worker-side idempotency.
- `test_a_locked_event_is_retried_without_calling_a_handler` — a live lease means another worker owns it; this job defers rather than duplicating its work (C3a).
- `test_a_locked_event_does_not_have_its_lease_cleared` — the one exit path that must **not** release: the lease belongs to the other worker, and clearing it would hand the row straight back out.
- `test_a_missing_row_is_retried` — raises `arq.worker.Retry`.
- `test_an_unknown_kind_dead_letters_immediately` — permanent; no retry.
- `test_the_kind_is_read_from_the_payload_and_never_split_from_the_event_id` — VS-003's note made executable: the job is given a payload whose `kind` is `status` while its `provider_event_id` starts `msg:`, and the status handler is the one that runs. Plus a source-level assertion that `app/worker/` contains no `split(":")`. Doubly safe now, since the job never receives a `provider_event_id` to split.
- `test_a_retryable_failure_defers_with_the_backoff_curve` — asserts the `Retry.defer_score`.
- `test_a_retryable_failure_releases_the_lease` — otherwise the deferred retry would arrive to find its own stale lease and defer again, turning one backoff curve into `max_tries` lease timeouts (A17).
- `test_a_successful_job_leaves_no_lease_behind`
- `test_the_last_try_dead_letters_instead_of_retrying` — `job_try == job_max_tries`: one `dead_letter_jobs` row, `attempts` matching, no `Retry` raised, and no lease left behind.
- `test_a_retryable_failure_still_leaves_attempts_incremented` — **amendment A1's own test.** Folded into the job's transaction, the claim (and `attempts + 1` with it) is undone by every retryable failure, so a job that failed five times reports one attempt and the retry curve is invisible to whoever is triaging.
- `test_a_payload_with_no_phone_number_id_dead_letters` and `test_an_unmapped_phone_number_id_dead_letters` — the two permanent tenant failures, separately, because they have different causes and different fixes.
- `test_the_dead_letter_has_no_tenant_when_resolution_is_what_failed` — VS-002 made `dead_letter_jobs.tenant_id` nullable for exactly this, and nothing had exercised it.
- `test_a_permanent_failure_releases_the_lease_and_marks_the_row_failed`
- `test_the_worker_package_never_splits_a_provider_event_id` — source-level, with comments and string literals stripped by `tokenize`. A plain substring scan finds the docstring that *explains* the rule and fails the file for being documented.
- `test_a_dead_letter_payload_carries_no_patient_content` — the payload keys are exactly `{"inbox_row_id", "kind", "phone_number_id", "job_try"}`, and neither `PATIENT_TEXT`, `PROFILE_NAME`, `phone()` **nor `wamid()`** appears anywhere in the serialised row. **C7 and C2.**
- `test_no_log_line_from_the_envelope_contains_a_wamid` — every line uses `event_id=<row uuid>` (C2), including the retry and dead-letter lines that a real outage would repeat five times.
- `test_a_dead_lettered_event_leaves_the_inbox_row_failed_with_a_reason_code` — `last_error` is a short code, never an exception message.

- [ ] **Step 3: Run the tests to verify they fail**

- [ ] **Step 4: Write `app/worker/errors.py` and `app/worker/retrying.py`**

```python
class JobError(Exception):
    """Base. Carries a short reason CODE and nothing else.

    Hard rule 8: these strings reach logs and dead_letter_jobs.error. A formatted
    exception would carry whatever it was formatted from - a payload excerpt, a
    phone number, a Meta error message. So every raise site passes a code from a
    small vocabulary: unknown_kind, unknown_phone_number, bad_tenant_map,
    unmodelled_message, conversation_race, status_before_wamid, http_429, …
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RetryableJobError(JobError):
    """Try again later: the same input can succeed on a later attempt."""


class PermanentJobError(JobError):
    """Do not try again: no number of attempts changes the answer."""
```

- [ ] **Step 5: Write `app/worker/jobs/inbox.py`'s envelope**

```python
async def process_inbox_event(ctx: dict[str, Any], row_id: str) -> str:
    """Process one webhook_inbox row. The only job VS-004 adds.

    Takes OUR row id, never a payload and never Meta's event id (hard rule 8,
    requirement 1, plan note C2): a wamid is base64 and decodes to include the
    patient's phone number, so it must not reach Redis, the job arguments, or the
    five log lines a retrying job writes. The row - and with it the payload, the
    wamid and the phone number - is loaded from Postgres, where it already is.

    Idempotent four ways over (see "Idempotency: the four keys"): arq refuses a
    duplicate job id, claim() refuses an event already PROCESSED, claim()'s lease
    refuses an event another worker is working on right now, and the reply's
    unique constraint refuses a second reply row. Returns a short outcome code -
    arq stores a job result, so the result must be as safe to keep as a log line.
    """
    job_try = max(int(ctx.get("job_try", 1)), 1)
    settings = ctx["settings"]
    sessionmaker = ctx["sessionmaker"]
    event_id = uuid.UUID(row_id)

    kind: str | None = None
    phone_number_id: str | None = None
    holds_lease = False
    try:
        async with sessionmaker() as session:
            inbox = WebhookInboxRepository(session)
            claim = await inbox.claim(event_id, settings.claim_lease_seconds)
            if claim.state == "already_processed":
                logger.info("inbox event already processed event_id=%s", event_id)
                return "skipped"
            if claim.state == "locked":
                # Another worker holds a live lease. Defer rather than duplicate
                # its work - and do NOT release: the lease is not ours. If that
                # worker dies, its lease expires and this event becomes
                # claimable again (plan note C3a).
                logger.info("inbox event locked by another worker event_id=%s", event_id)
                raise RetryableJobError("event_locked")
            if claim.state == "missing":
                # The webhook commits before it enqueues, so this should not
                # happen. Retryable rather than permanent: a retry costs four
                # deferrals and a dead letter either way, while calling it
                # permanent on the first try would discard an event that a
                # visibility oddity had merely hidden.
                raise RetryableJobError("inbox_row_missing")
            holds_lease = True
            row = claim.row
            payload = row.payload
            kind = payload.get("kind")
            phone_number_id = (payload.get("metadata") or {}).get("phone_number_id")
            ...
            # resolve the tenant, attach it, dispatch on `kind`, then in T2:
            # status = PROCESSED and locked_until = NULL in one UPDATE, commit
    except RetryableJobError as error:
        ...
    except PermanentJobError as error:
        ...
```

`holds_lease` is what keeps the `locked` case from clearing somebody else's lease — the one exit path that must not release. Every other exit does, in the `except` blocks below.

The two `except` blocks are the whole retry policy, in one place:

```python
    except RetryableJobError as error:
        # Release the lease on the way out (A17). Deferring while still holding it
        # would make the retry arrive to find its own stale lease, report `locked`
        # against itself, and defer again - one backoff curve turned into
        # max_tries lease timeouts. Skipped when the lease is not ours.
        if holds_lease:
            await _release(sessionmaker, event_id)
        if job_try < settings.job_max_tries:
            defer = backoff_seconds(job_try, settings)
            logger.warning(
                "inbox event retrying event_id=%s reason=%s try=%d defer=%.1f",
                event_id, error.reason, job_try, defer,
            )
            # arq does NOT retry a plain exception (plan conflict note C13):
            # Retry is how a retry happens at all, and raising it ourselves is
            # also what gives us a backoff curve we control.
            raise Retry(defer=defer) from None
        await _dead_letter(event_id, kind, phone_number_id, job_try, error.reason, ...)
        return "dead_lettered"
    except PermanentJobError as error:
        if holds_lease:
            await _release(sessionmaker, event_id)
        # No retry, and no re-raise: raising would make arq log a traceback for a
        # decision we have already recorded properly.
        await _dead_letter(event_id, kind, phone_number_id, job_try, error.reason, ...)
        return "dead_lettered"
```

`_release` and `_dead_letter` each open a **fresh session** — the failed one is unusable after a rollback. `_dead_letter` writes the `dead_letter_jobs` row through VS-002's repository, marks the inbox row `FAILED` with the reason code, and clears the lease in the same `UPDATE`.

```python
def dead_letter_payload(
    row_id: uuid.UUID, kind: str | None, phone_number_id: str | None, job_try: int
) -> dict[str, Any]:
    """A REFERENCE to the event, never the event (requirement 6, note C7).

    dead_letter_jobs.payload is NOT NULL, and VS-002's docstring assumed it would
    hold a copy of webhook_inbox.payload. Requirement 6 forbids that: this is a
    table people open to triage failures, and it must not be a second copy of a
    patient's message. source_event_id points at the inbox row that has the full
    event, so nothing is lost and the sensitive copy stays in one place with one
    retention policy.

    It carries our row id, NOT provider_event_id (plan note C2): a wamid decodes
    to include a phone number, and a triage table is read casually.
    """
    return {
        "inbox_row_id": str(row_id),
        "kind": kind,
        "phone_number_id": phone_number_id,
        "job_try": job_try,
    }
```

`_dead_letter` passes `source_event_id=str(row_id)` too. That column is documented as holding "the `webhook_inbox.provider_event_id` this job came from" — update its comment in `app/db/models/dead_letter.py` in the same step: it now holds the row id, for the same reason, and the row id is the better join key anyway.

- [ ] **Step 6: Correct the `dead_letter_jobs` docstring**

`app/db/models/dead_letter.py` currently says the `payload` column holds "the same patient content as `webhook_inbox.payload`". Replace with the reference-envelope contract and a pointer to this plan. A comment that describes the opposite of what the code does is worse than no comment.

- [ ] **Step 7: Wire the worker in `app/worker/main.py`**

`on_startup` builds the context once per process: `settings`, `sessionmaker` (`get_sessionmaker()`), `http` (one `httpx.AsyncClient`, A8), `meta` (`MetaClient`), `resolver` (`ConfigTenantResolver.from_settings`). `on_shutdown` closes the client and disposes the engine. `functions = [ping, func(process_inbox_event, max_tries=settings.job_max_tries, timeout=settings.job_timeout_seconds)]`, and `max_tries` on `WorkerSettings` too, so a future job inherits the same ceiling.

Comment why the resolver is built at startup: a broken map then produces one startup failure per worker rather than a parse per job, and A1's "loud dead letter" still happens because `resolve()` is what raises.

- [ ] **Step 8: Run the tests, lint, format**

- [ ] **Step 9: Append the task entry to the report**

---

### Task 7: Inbound messages — store everything, reply once

**Files:**
- Modify: `app/worker/jobs/inbox.py` (`handle_message`, `ACK_TEXT`, `_modality_for`, `_display_name_for`)
- Create: `tests/worker/test_inbox_message.py` (23 new)

**Interfaces:**
- `ACK_TEXT = "Received ✅"`
- `async def handle_message(session, meta, settings, tenant_id, row, payload) -> str`

**Expected tests after this task: 244.**

- [ ] **Step 1: Write the failing message tests**

Storage:
- `test_a_text_message_creates_a_contact_a_conversation_and_an_inbound_message`
- `test_the_profile_name_is_taken_from_contacts_by_wa_id` — the name is in `payload["contacts"]`, not on the message (VS-003's payload shape).
- `test_the_inbound_message_carries_the_wamid_as_its_provider_message_id` — the key that makes a re-run safe.
- `test_a_second_message_from_the_same_patient_reuses_the_contact_and_conversation`
- `test_an_audio_message_is_stored_as_a_voice_note_with_no_text` — VS-008 attaches the transcript to this row.
- `test_an_image_message_is_stored_with_modality_other` — requirement 2's "store all inbound message types", and C4's reason for widening the CHECK.
- `test_an_image_message_gets_no_reply` — default `WHATSAPP_REPLY_TO_TYPES=text`; outcome `stored_no_reply`.
- `test_the_reply_type_filter_is_read_from_settings` — adding `image` makes it replied to.
- `test_a_message_that_fails_its_model_dead_letters` — A14, hash-keyed rows included.
- `test_a_conversation_race_is_retryable` — `get_or_create_open` raising `IntegrityError` becomes `RetryableJobError("conversation_race")`, never a dead letter (VS-002's follow-up, pulled in).

Replying exactly once:
- `test_a_text_message_sends_one_reply_and_stores_it_as_sent_with_a_wamid`
- `test_the_reply_row_is_linked_to_the_inbound_message`
- `test_running_the_job_twice_sends_one_reply` — the acceptance criterion; the second run returns `skipped` at the claim.
- `test_two_concurrent_runs_of_one_job_send_exactly_one_reply` — **C3a, end to end at the job level.** Two `process_inbox_event` calls on genuinely independent sessions (`second_session_factory`), started with `asyncio.gather`, against a Meta transport that counts requests and holds the first one open long enough for the second run to get past its claim if it can. Assert: exactly one Meta request, exactly one reply row, and the loser returned `dead_lettered`-free with a `Retry`. Without the lease this test fails with two requests — it is the regression test for the bug the plan's first draft had.
- `test_a_job_whose_reply_row_already_has_a_wamid_sends_nothing` — the crash-after-T2-partial case: force the reply row to `SENT` with a wamid, leave the inbox row `PROCESSING`, re-run, assert zero Meta requests and outcome `already_replied`. **Requirement 3, verbatim.**
- `test_the_reply_row_is_committed_before_the_send` — a transport that, when called, opens an independent session and asserts the reply row is already visible. This is the commit boundary made executable (Review Focus 5).
- `test_a_retryable_send_leaves_the_reply_row_queued_and_retries` — the row stays `QUEUED` with no wamid, so the next try recognises it.
- `test_a_permanent_send_marks_the_reply_failed_and_dead_letters` — hard rule 5's shape: nothing is claimed to have been sent.
- `test_the_reply_goes_to_the_phone_number_id_the_message_arrived_on` — A7.

Hard rule 7 and privacy:
- `test_a_human_active_conversation_drops_the_reply` — no Meta request, no reply row, outcome `dropped_not_ai_active`, and the log line carries the conversation id only.
- `test_a_closed_conversation_drops_the_reply`
- `test_the_state_is_re_read_immediately_before_the_send` — flip the state to `HUMAN_ACTIVE` after the inbound message is stored; the reply is still dropped (Review Focus 10).
- `test_no_log_line_from_the_message_path_contains_patient_content` — `caplog` against `PATIENT_TEXT`, `PROFILE_NAME`, `phone()` and `wamid()` (C2).

- [ ] **Step 2: Run the tests to verify they fail**

- [ ] **Step 3: Write `handle_message`**

Order, and why, as comments in the code (mirroring "Commit boundaries"):

```python
async def handle_message(...) -> str:
    """Store the inbound message, then answer it exactly once.

    Requirement 2: EVERY inbound type is stored; only the types in
    WHATSAPP_REPLY_TO_TYPES get a reply. An image nobody answers is still a
    message the clinic must be able to see.
    """
    # 1. The patient and the thread. Both are get-or-create through VS-002's
    #    repositories, which resolve their races in the database rather than
    #    raising exceptions that quote a phone number (hard rule 8).
    # 2. The inbound message, keyed by wamid. DuplicateRecordError here is not a
    #    failure - it means a previous try already stored it - so we re-read it
    #    and carry on to the reply step. This is what makes the whole job
    #    re-runnable.
    # 3. Hard rule 7, re-read from the database: HUMAN_ACTIVE or CLOSED drops
    #    the reply. Logged by conversation id only.
    # 4. Reserve the reply row (QUEUED, linked to the inbound message) and COMMIT
    #    before touching Meta. A reply row that already carries a wamid means a
    #    previous try succeeded: return without sending (requirement 3).
    # 5. One send attempt. The client classifies; this function only reacts.
    # 6. On success, write the wamid onto the reserved row, mark the inbox row
    #    PROCESSED and clear its lease, in one transaction. The window between
    #    5 and 6 is the documented duplicate-reply gap - see the plan.
    #
    # Every log line here uses event_id=<webhook_inbox row uuid> (plan note C2).
    # The wamid is stored in messages.provider_message_id and never logged: it is
    # base64 and decodes to include the patient's phone number.
```

`_modality_for(message_type)`: `"text" → TEXT`, `"audio" → VOICE_NOTE`, everything else `→ OTHER`, with a comment that `audio` is mapped here rather than in VS-008 so a voice note already sitting in `messages` needs no backfill.

- [ ] **Step 4: Run the tests, lint, format**

- [ ] **Step 5: Append the task entry to the report**

---

### Task 8: Status callbacks — forward only, and the one that arrives too early

**Files:**
- Modify: `app/worker/jobs/inbox.py` (`handle_status`, `_status_for`)
- Create: `tests/worker/test_inbox_status.py` (13 new)

**Interfaces:**
- `async def handle_status(session, tenant_id, row, payload) -> str`
- `_STATUS_WORDS: dict[str, MessageStatus]` — `sent`, `delivered`, `read`, `failed`

**Expected tests after this task: 256.**

- [ ] **Step 1: Write the failing status tests**

- `test_a_sent_callback_marks_the_reply_sent`
- `test_delivered_and_read_advance_in_order`
- `test_a_delivered_callback_after_read_changes_nothing` — requirement 5's "never read → delivered", the case Meta's out-of-order delivery produces routinely.
- `test_a_repeated_read_callback_changes_nothing`
- `test_a_failed_callback_overwrites_sent` — the `FAILED` rank decision.
- `test_a_failed_callback_does_not_overwrite_delivered` — the other half of it.
- `test_a_status_for_an_unknown_wamid_is_retryable` — requirement 5's status-before-wamid case: `RetryableJobError("status_before_wamid")`, not a dead letter on the first try.
- `test_the_same_status_job_succeeds_once_the_wamid_is_stored` — proves the retry is not merely tolerated but useful.
- `test_a_status_for_an_unknown_wamid_dead_letters_after_max_tries` — A11's consequence, stated in a test so it is not a surprise in production.
- `test_a_status_word_we_do_not_model_is_ignored_not_dead_lettered` — A12; the row still reaches `PROCESSED`.
- `test_a_status_for_another_tenants_message_does_not_advance_it` — `MessageRepository` is tenant-scoped; this proves the scoping is not bypassed.
- `test_a_failed_callback_logs_the_meta_error_code_and_nothing_else` — `item["errors"]` through `scrub`; no `error.message`, no `recipient_id`.
- `test_no_status_log_line_contains_the_wamid_it_is_about` — **C2, and the status handler is where it is most tempting**: the obvious debug line is "status X for wamid Y", and that wamid is the id of a message *we sent to the patient*. The handler logs `event_id=<row uuid> status=<word> moved=<bool>` instead, which is enough to trace a status through a log without naming anyone.

- [ ] **Step 2: Run the tests to verify they fail**

- [ ] **Step 3: Write `handle_status`**

```python
async def handle_status(...) -> str:
    """Advance one outbound message's delivery status.

    Look the message up FIRST, then advance. The lookup is not redundant with
    advance_status's return value: they answer different questions.
    "No such wamid" means the status arrived before the worker saved it - Meta's
    ordinary out-of-order delivery - and is retryable. "Found, but did not move"
    means the status was old or repeated, and is a success. One boolean cannot
    distinguish those, and treating them alike would either lose a real status or
    dead-letter a duplicate one.

    The wamid is read out of the payload and never logged (plan note C2): here it
    is the id of a message WE sent TO the patient, so it identifies them twice
    over. Log lines carry event_id=<webhook_inbox row uuid>, the status word, and
    whether the row moved.
    """
```

An unmodelled status word logs and returns `status_ignored`. A `failed` status logs `error_reason`-style codes from `item["errors"]` and stores nothing beyond `status = FAILED` — there is no error column on `messages`, and adding one is a follow-up, not this slice.

- [ ] **Step 4: Run the tests, lint, format**

- [ ] **Step 5: Append the task entry to the report**

---

### Task 9: End-to-end proofs, the README, and the slice write-up

The acceptance criteria that span components, then the documentation `CLAUDE.md` requires.

**Files:**
- Create: `tests/worker/test_end_to_end.py` (7 new)
- Modify: `tests/api/test_route_exposure.py` (+1: the route inventory is unchanged by this slice)
- Modify: `README.md` (running the worker, watching a job, reading a dead letter)
- Modify: `docs/slices/VS-004.md` (Status, Notes, Follow-ups), `docs/slices/README.md`

**Expected tests after this task: 263.**

- [ ] **Step 1: Write the end-to-end tests**

Webhook in, reply out, with a mocked Meta and a real database:

- `test_a_webhook_delivery_becomes_one_stored_message_and_one_reply` — the slice's headline, minus the phone: POST a signed body, drain the fake queue by calling the job with each enqueued id, assert one inbound row, one outbound row with a wamid, one Meta request.
- `test_the_same_webhook_delivered_twice_produces_one_message_and_one_reply` — requirement 7's dedup case, end to end: two POSTs, every enqueued id processed, one of each.
- `test_the_webhook_answers_fast_when_the_meta_send_is_slow` — the Meta transport sleeps 2 seconds; the POST is timed and asserted well under a second, and the transport is asserted **never called** during the request. The slice's second acceptance criterion, and Review Focus 1.
- `test_a_status_delivery_after_the_reply_advances_it_to_read` — message job, then `sent`/`delivered`/`read` jobs, ending at `READ`.
- `test_a_status_delivered_before_the_reply_job_ran_retries_then_succeeds` — the ordering acceptance case, in the order Meta can actually produce.
- `test_a_meta_outage_retries_and_then_dead_letters_without_replying` — 500 on every attempt through `job_max_tries`: one `dead_letter_jobs` row, zero outbound messages with a wamid, and the patient told nothing untrue (hard rule 5's shape). Also asserts no lease is left behind, so the row is not stuck.
- `test_nothing_in_redis_or_the_logs_from_a_full_run_contains_a_wamid` — **C2, once over the whole path.** Drive a delivery end to end with `caplog` at `DEBUG` and a recording queue, then assert that no captured log line, no job id and no job argument contains any `wamid()` value from the payload. One test that fails if any future log line in this slice reaches for the obvious identifier.

- [ ] **Step 2: Run the full suite both ways**

```bash
uv run pytest -q                       # with Postgres down: db tests skip
docker compose up -d postgres redis && uv run pytest -q
docker compose exec api pytest -q
uv run ruff check . && uv run ruff format .
```

- [ ] **Step 3: Add the worker section to `README.md`**

What to write: how to start the worker (`docker compose up`), how to watch a job (`docker compose logs -f worker | Select-String "inbox event"`), how to read the outcome of an event, and how to read a dead letter — with the same warning VS-003 attached to `webhook_inbox`, now one item longer: **select row ids, statuses and reason codes; never `payload`, never `text`, and never `provider_event_id`.** A terminal transcript is as public as a log (hard rule 8), and a `provider_event_id` is a wamid (C2).

Take the psql user and database name from `docker-compose.yml` / `.env` (`POSTGRES_USER`, `POSTGRES_DB`) rather than pasting them from another document — the compose file defaults them, so a changed `.env` would make every documented command fail with an authentication error that looks like something else.

- [ ] **Step 4: Write the Notes and Follow-ups into `docs/slices/VS-004.md`**

Set `Status: PARTIAL` with one line saying why (Task 10 is unfinished: Meta is not delivering to our callback). Notes must cover, at minimum:

- The two commit boundaries and why the reply row is committed before the send.
- The duplicate-reply gap, in full, including why Meta gives us no idempotency key and why the alternative ordering is worse.
- The four idempotency mechanisms, and that the arq job id is a short-lived one.
- **Why a wamid is patient content** (C2): base64, decodes to include the phone number, and a status event id carries the wamid of our message *to* the patient. Hence: the job carries our row id, `event_id=` is always a row UUID, and nothing in `app/worker/` or `app/queue/` ever logs a `provider_event_id`.
- Why the webhook looks up the row id for a duplicate instead of skipping it (C8 plus C2).
- `claim()`'s two guards: `<> 'PROCESSED'` rather than `= 'RECEIVED'`, **and** the lease — with the concrete bug the lease fixes (C3a: two concurrent runs, one reply row, two sends), why the reply row's unique constraint does not fix it, and why the lease must outlive `job_timeout_seconds`.
- That the lease is released on every exit except `locked`, and what goes wrong if it is not (A17).
- The status rank table, and why `FAILED` sits between `SENT` and `DELIVERED`.
- Why classification lives in the client and retries live in the job.
- That arq does not retry plain exceptions (C13).
- Why `dead_letter_jobs.payload` is a reference and not the event (C7).
- `OTHER` as a modality, and the CHECK-swap migration that added it.
- Whatever the live test in Task 10 teaches, appended there.

Follow-ups must include, at minimum: **VS-003's webhook still logs wamids on its happy path** (C2a — a merged slice's tested log contract, so it is a change of its own); the duplicate-reply gap's two possible narrowings; a worker killed with `SIGKILL` stranding its event for the remainder of the lease (A16); jitter on the backoff (A9); a status for a wamid we never sent, retrying five times (A11); no error column on `messages` for a `failed` callback's code; a validating helper for `WHATSAPP_TENANT_MAP` (A1); the `webhook_inbox.status` index, still deliberately absent (A13); retention now covering `messages.text` as well as `webhook_inbox.payload`; and structured logging with a correlation id, which five job tries make more valuable than ever.

- [ ] **Step 5: Explain the slice function by function**

`CLAUDE.md` requires it: what each function does, why it exists, which hard rule it protects. Cover at minimum `claim` (its four-state result, both guards, and the lease), `release`, `get_by_event_id` and its single caller, `reserve_reply`, `attach_provider_id`, `advance_status` (and why the rank guard is in SQL), `ConfigTenantResolver.resolve` and `from_settings`, `classify_status`, `classify_exception`, `MetaClient.send_text` (and why it never retries), `scrub` and `error_reason`, `JobQueue` and `ArqJobQueue.enqueue_inbox_event` (and why the job id is our row id and not Meta's), the seam in `receive` including the duplicate-path lookup, `process_inbox_event` with its two `except` blocks and `holds_lease`, `backoff_seconds`, `dead_letter_payload`, `handle_message` step by step against the commit boundaries, `_modality_for`, and `handle_status` with its look-up-then-advance reasoning.

- [ ] **Step 6: Set VS-004 to `PARTIAL` in `docs/slices/README.md`**

VS-004 only. VS-003 is merged to `main` and this slice does not change its status.

- [ ] **Step 7: Append the task entry to the report**

---

### Task 10: Live test with the developer's phone — **the one task that stops for the developer**

**The state of play, corrected.** The Meta developer app **exists**. The webhook handshake **was verified**. The `messages` field **is subscribed**. What does not work is the thing all of that was supposed to produce: **Meta never POSTs a real message to our callback URL.** `hello_world` never arrived either, which is a useful clue — it points away from our code (a payload we never see cannot be a signature or parsing problem) and towards the app/number configuration on Meta's side.

So this task is not blocked on access. It is a diagnosis, and Steps 2 and 3 are the two most likely causes, in order:

1. **The app is not actually subscribed to the WABA.** The dashboard's "Webhook fields" toggle and the app's subscription to the *business account* are two different things, and the toggle can look right while `GET /{WABA_ID}/subscribed_apps` returns an empty list. This is the single most common cause of "verified, subscribed, and silent".
2. **The phone is not on the test number's allowed recipient list.** A test number only talks to numbers explicitly added to it, in both directions. `hello_world` not arriving is exactly what this looks like.

**Every command in this task is PowerShell**: `$env:NAME`, `curl.exe` never bare `curl`, one line per command however long.

- [ ] **Step 1: Fill in the send credentials**

VS-003 needed only the app secret — receiving does not need a token. **Sending does.**

`META_ACCESS_TOKEN`: WhatsApp → API Setup → temporary access token (24h) or a permanent System User token.
`META_PHONE_NUMBER_ID`: the same page, under the test number.
`META_API_VERSION`: the version the dashboard shows (A6 — a retired one is a permanent 4xx).
`WHATSAPP_TENANT_MAP` or `DEV_TENANT_ID`: a tenant uuid for that number. Generate one with `python -c "import uuid; print(uuid.uuid4())"`.

```powershell
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose logs api | Select-String "not set"
docker compose logs worker | Select-String "tenant map loaded"
```

Expected: nothing from the third line, and one `tenant map loaded source=… entries=1` from the fourth.

- [ ] **Step 2: Check that the app is subscribed to the WABA — the most likely cause**

In Graph API Explorer, with the app selected and a token that has `whatsapp_business_management`:

```
GET /{WABA_ID}/subscribed_apps
```

**If `data` is empty, that is the bug.** The dashboard's webhook fields were set on the app, but the app was never subscribed to the business account, so Meta has nowhere to send a message event and reports no error anywhere. Fix it:

```
POST /{WABA_ID}/subscribed_apps
```

Then `GET` it again and confirm the app now appears. The WABA id is on WhatsApp → API Setup (it is also the `entry[].id` VS-003 stores as `entry_id`, if any event ever arrived).

- [ ] **Step 3: Check the allowed recipient list — the second most likely cause**

WhatsApp → API Setup → "To" → Manage phone number list. The developer's own phone must be there and **verified** by the code Meta sends. A test number refuses traffic with numbers that are not on this list, in both directions, which is exactly why `hello_world` never arrived.

While on that page, send `hello_world` from the dashboard. If it arrives on the phone now, the recipient list was the problem and the inbound path is worth retrying immediately.

- [ ] **Step 4: Bring up the tunnel and re-confirm the callback**

The handshake was verified before, but a `trycloudflare` URL changes on every restart, so the saved callback URL is almost certainly stale — a stale URL is itself a complete explanation for silence.

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
```

`127.0.0.1`, not `localhost`: on Windows `localhost` resolves to `::1` first and nothing listens there, because compose publishes on `127.0.0.1` only (VS-003's Notes measured what that costs). Leave the window open for the rest of the task.

Then re-save the callback URL as `https://<tunnel>/webhooks/whatsapp` with the same `META_VERIFY_TOKEN`, confirm `whatsapp handshake verified` in the api log, and re-check that `messages` is still subscribed under Webhook fields.

- [ ] **Step 5: Send `Hello` and watch the whole path**

```powershell
docker compose logs -f worker | Select-String "inbox event"
```

Expected on the phone: `Received ✅`. Expected in the log: a claim, then a `replied` outcome under an `event_id=<uuid>`, then `status_advanced` lines for `sent`, `delivered` and (if you open the chat) `read`. **If nothing arrives at all, go back to Steps 2–4** — no log line means no POST, and no POST is a Meta-side configuration answer, not a code one.

**Before running any psql command, read the credentials out of the project rather than pasting them:** `POSTGRES_USER` and `POSTGRES_DB` are defaulted in `docker-compose.yml` and may be overridden in `.env`. Check both, and substitute the real values below — a wrong user produces an authentication error that looks like a database problem.

```powershell
Select-String -Path docker-compose.yml, .env -Pattern "POSTGRES_USER|POSTGRES_DB"
```

Then, ids and statuses only — **never `payload`, never `text`, and never `provider_event_id`** (hard rule 8, and C2: a `provider_event_id` is a wamid, which decodes to include a phone number; a terminal transcript is as public as a log):

```powershell
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select id, kind_from_payload, status, attempts, locked_until is null as free, tenant_id is not null as has_tenant from (select id, payload->>'kind' as kind_from_payload, status, attempts, locked_until, tenant_id, created_at from webhook_inbox) t order by created_at desc limit 10;"
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select direction, modality, status, provider_message_id is not null as has_wamid, reply_to_message_id is not null as is_reply from messages order by created_at desc limit 10;"
```

Expected: the message event `PROCESSED`, with a tenant and `free` true (the lease was released); one `INBOUND`/`RECEIVED` row and one `OUTBOUND` row that is a reply, has a wamid, and has advanced past `SENT`.

- [ ] **Step 6: Prove the duplicate cases against the real thing**

**Redelivery.** Stop the api, send a second message while it is down, start it again. Meta's retry schedule backs off and is not published — **a redelivery arriving 5–15 minutes later is normal**, and an empty table straight after `start` is the expected state, not a result.

```powershell
docker compose stop api
# send the message now
docker compose start api
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select count(*) - count(distinct provider_event_id) as duplicate_event_rows from webhook_inbox;"
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select reply_to_message_id, count(*) from messages where reply_to_message_id is not null group by 1 having count(*) > 1;"
```

Expected: `0` from the first (counted rather than listed, so no wamid is printed) and `(0 rows)` from the second, plus exactly one `Received ✅` on the phone per message sent.

**Worker restart mid-flight.** Send a message and restart the worker while the job is running (`docker compose restart worker`). Expected: the reply still arrives, and the second query above still returns `(0 rows)`. This is the one place the duplicate-reply gap can show itself — if a second `Received ✅` appears, that is the documented gap, not a new bug: record it in the Notes with what you did, and do not "fix" it here.

- [ ] **Step 7: Prove the failure path once, deliberately**

Set `META_ACCESS_TOKEN` to a deliberately wrong value, restart the worker, send a message.

```powershell
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "select job_name, source_event_id, error, attempts, created_at from dead_letter_jobs order by created_at desc limit 5;"
```

Expected: **one** row, immediately — a 401 is permanent, so `attempts` is 1 and there was no 75-second retry curve — with `error` a short code and no phone number anywhere in the row. Then put the real token back and confirm the next message is answered.

- [ ] **Step 8: Close the slice, or record honestly why it is still PARTIAL**

```powershell
docker compose exec api pytest -q
docker compose exec api ruff check .
```

If Step 5 produced a reply on the phone: set `Status: DONE` in `docs/slices/VS-004.md` and `docs/slices/README.md`, and append to the Notes whatever the live run taught — above all **which of Steps 2, 3 and 4 was the cause of the silence**, since that is the finding this whole task exists to produce.

If it did not: leave `PARTIAL`, and write down what each of Steps 2–4 actually returned. "Still no POST" is a much weaker note than "`subscribed_apps` was empty and populating it did not help either" — the next attempt starts from whichever one you record.

`docs/slices/VS-003.md` is not touched either way: VS-003 is merged to `main`.

- [ ] **Step 9: Append the task entry to the report, and stop for the developer**

---

## Acceptance criteria mapped to tasks

| VS-004 requirement | Where it is built | Where it is proven |
|---|---|---|
| Enqueue behind an interface, with an arq implementation | Task 5, Steps 4–5 | Task 5, Step 1 (`test_the_queue_satisfies_the_protocol` + 5) |
| Enqueued at the VS-004 seam, after the commit, one job per extracted item | Task 5, Step 6 | Task 5, Step 2 (`test_the_enqueue_happens_after_the_commit`, `test_three_messages_and_two_statuses_enqueue_five_jobs`) |
| The job carries only the `webhook_inbox` row id; no payload and no wamid in Redis (C2) | Task 5, Steps 4–6 | Task 5, Step 1 (`test_enqueueing_passes_the_row_id_and_nothing_else`, `test_no_job_argument_or_job_id_contains_a_wamid`); Task 9, Step 1 (`test_nothing_in_redis_or_the_logs_from_a_full_run_contains_a_wamid`) |
| The row id is the arq job id (`inbox:<uuid>`), stable across redeliveries, so a double enqueue is harmless | Task 5, Steps 5–6 | Task 5, Step 1 (`test_the_job_id_is_the_row_id_under_an_inbox_prefix`, `test_a_duplicate_job_id_is_not_an_error`); Task 5, Step 2 (`test_the_row_id_for_a_duplicate_is_looked_up_not_invented`) |
| Enqueue failure → 503 `queue unavailable` (C1); a redelivery re-enqueues | Task 5, Step 6 | Task 5, Step 2 (`test_an_enqueue_failure_answers_503_…`, `test_a_redelivered_event_that_is_already_stored_is_enqueued_again`, `test_the_enqueue_failure_log_carries_row_ids_and_no_wamid`) |
| The worker is idempotent; already-processed events are skipped | Task 2, Step 7 (`claim`); Task 6, Step 5 | Task 2, Step 2 (10 claim tests); Task 6, Step 2; Task 7, Step 1 (`test_running_the_job_twice_sends_one_reply`) |
| **Two concurrent runs of one event cannot both send** (C3a) | Task 2, Steps 4b, 6–7 (`locked_until`, the lease in `claim`); Task 6, Step 5 (`holds_lease`, `release`) | Task 2, Step 2 (`test_two_concurrent_claims_…`, `test_a_row_with_a_live_lease_is_not_claimable`, `test_a_row_with_an_expired_lease_is_claimable`); Task 6, Step 2 (`test_a_locked_event_is_retried_…`, `test_a_retryable_failure_releases_the_lease`); Task 7, Step 1 (`test_two_concurrent_runs_of_one_job_send_exactly_one_reply`) |
| The lease outlives the job that holds it | Task 1, Step 5 (`claim_lease_seconds`) | Task 1, Step 2 (`test_the_claim_lease_outlives_the_job_timeout`) |
| `payload["kind"]` is read, never split from the event id | Task 6, Step 5 | Task 6, Step 2 (`test_the_kind_is_read_from_the_payload_and_never_split_from_the_event_id`) |
| Tenant resolved from `phone_number_id` behind one interface; type changeable in one place | Task 3 | Task 3, Step 1 (10 tests) |
| Upsert contact/conversation, store the inbound message | Task 7, Step 3 | Task 7, Step 1 (`test_a_text_message_creates_…`, `test_a_second_message_…_reuses_…`) |
| `get_or_create_open`'s `IntegrityError` is retryable | Task 7, Step 3 | Task 7, Step 1 (`test_a_conversation_race_is_retryable`) |
| All inbound types stored; replies only to configured types | Task 2, Step 4 (`OTHER`); Task 7, Step 3 | Task 7, Step 1 (`test_an_image_message_is_stored_with_modality_other`, `test_an_image_message_gets_no_reply`, `test_the_reply_type_filter_is_read_from_settings`) |
| Reply row inserted `QUEUED` first, linked, unique per inbound message | Task 2, Steps 5–6, 8; Task 7, Step 3 | Task 2, Step 1 (`test_one_inbound_message_can_have_only_one_reply`); Task 7, Step 1 (`test_the_reply_row_is_linked_…`, `test_the_reply_row_is_committed_before_the_send`) |
| Then send, then save the wamid; never send again if a wamid is present | Task 7, Step 3 | Task 7, Step 1 (`test_a_text_message_sends_one_reply_…`, `test_a_job_whose_reply_row_already_has_a_wamid_sends_nothing`) |
| The remaining duplicate gap is documented | "The duplicate-reply gap"; Task 9, Step 4 | Task 9, Step 4 (slice Notes); Task 10, Step 6 (live, if it shows) |
| Meta client: send text, one attempt, timeout, classification in one place | Task 4, Steps 4–5 | Task 4, Step 2 (`test_the_client_makes_exactly_one_attempt_for_a_500` + 11) |
| Retries only at the job level, exponential backoff from settings | Task 1, Step 5; Task 6, Steps 4–5 | Task 6, Step 1 (4 backoff tests); Task 6, Step 2 (`test_a_retryable_failure_defers_with_the_backoff_curve`) |
| Dead letter after the last try, or immediately on a permanent error | Task 6, Step 5 | Task 6, Step 2 (`test_the_last_try_dead_letters_…`, `test_an_unknown_kind_dead_letters_immediately`) |
| Permanent errors include an unknown `phone_number_id` and a malformed payload | Task 3, Step 3; Task 6, Step 5; Task 7, Step 3 | Task 3, Step 1 (`test_an_unknown_phone_number_id_raises_…`); Task 6, Step 2; Task 7, Step 1 (`test_a_message_that_fails_its_model_dead_letters`) |
| Statuses: `sent`, `delivered`, `read`, `failed`, forward only | Task 2, Steps 4, 8; Task 8, Step 3 | Task 2, Step 2 (`test_a_status_only_moves_forward`); Task 8, Step 1 (6 ordering tests) |
| A status for a wamid not stored yet is retryable | Task 8, Step 3 | Task 8, Step 1 (`test_a_status_for_an_unknown_wamid_is_retryable`, `test_the_same_status_job_succeeds_once_the_wamid_is_stored`) |
| No text, phone numbers, wamids or tokens in logs, dead letters or job arguments | Task 4, Step 4; Task 5, Steps 5–6; Task 6, Step 5 | Task 4, Steps 1–2 (`test_an_error_reason_never_contains_the_message_field`, `test_no_log_line_contains_the_access_token_…`); Task 5, Step 2; Task 6, Step 2 (`test_a_dead_letter_payload_carries_no_patient_content`, `test_no_log_line_from_the_envelope_contains_a_wamid`); Task 7, Step 1; Task 8, Step 1 (`test_no_status_log_line_contains_the_wamid_it_is_about`) |
| Meta error bodies sanitised before logging or storing | Task 4, Step 4 | Task 4, Step 1 (4 redaction tests); Task 8, Step 1 (`test_a_failed_callback_logs_the_meta_error_code_and_nothing_else`) |
| Retry behaviour: 5xx/429/timeout retried, 4xx not, dead letter after max tries | Task 4, Step 5; Task 6, Step 5 | Task 4, Step 2 (6 classification tests); Task 9, Step 1 (`test_a_meta_outage_retries_and_then_dead_letters_without_replying`) |
| The webhook responds fast even when the Meta send is slow | Task 5, Step 6 (nothing slow in the request) | Task 9, Step 1 (`test_the_webhook_answers_fast_when_the_meta_send_is_slow`) |
| Dedup: one webhook delivered twice → one message, one reply | Tasks 2, 5, 7 | Task 9, Step 1 (`test_the_same_webhook_delivered_twice_…`); Task 10, Step 6 (live) |
| The job run twice → one reply | Task 2, Step 7; Task 7, Step 3 | Task 7, Step 1 (`test_running_the_job_twice_sends_one_reply`) |
| `"Hello"` from my phone → `"Received ✅"` back | — | **Task 10, Step 5 — needs Meta to start delivering; Steps 2–4 diagnose why it does not** |
| Hard rule 1: nothing slow in the webhook | Task 5, Step 6 | Task 9, Step 1; Task 5, Step 2 (no import of the Meta client or the worker in `app/api/whatsapp.py`) |
| Hard rule 4: tenant from `phone_number_id` only | Task 3 | Task 3, Step 1; Task 7, Step 1 |
| Hard rule 7: state re-read before the send | Task 7, Step 3 | Task 7, Step 1 (3 state tests, including `test_the_state_is_re_read_immediately_before_the_send`) |
| Hard rule 11: timeouts, bounded retries, dead-letter table | Tasks 1, 4, 6 | Task 1, Step 2 (`test_the_job_timeout_exceeds_the_send_timeout`); Tasks 4, 6, 9 |
| `pytest` passes, `ruff check` clean | every task | Task 9, Step 2; Task 10, Step 8 |
| Slice Status and Notes updated | Task 9, Steps 4 and 6 | Task 10, Step 8 |
| Each task reported, no mid-slice checkpoints | every task's last step | `.superpowers/sdd/VS-004-report.md` |
