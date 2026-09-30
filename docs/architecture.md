# Architecture (this repo's part)

Full product: see Doctoleb Technical Handoff PDF. This repo covers the WhatsApp channel + Agent Core.

## Ownership
| Area | Owner |
|---|---|
| WhatsApp webhook, sending, media | this repo |
| Contacts, conversations, messages, webhook inbox, handoff state | this repo (our DB) |
| Agent Core (OpenAI, prompts, tools) | this repo |
| Clinics, doctors, services, schedules, appointments, holds | Booking Service (separate) |
| Staff dashboard / inbox UI | separate (reads our conversation data via API; TBD) |

## Text message flow
```
Meta -> POST /webhooks/whatsapp
          verify X-Hub-Signature-256 (HMAC-SHA256 with app secret)
          dedupe by message id (webhook_inbox unique constraint)
          store raw event, enqueue job, return 200
worker job:
    T1    resolve tenant from phone_number_id
          upsert contact + conversation, store message
          if conversation not AI_ACTIVE -> supersede PENDING booking actions, stop
          load the recent history
          booking state: expire_pending(the INJECTED clock), then state_for() ->
            the confirmation gate's verdict, computed in SQL
          patient reference: the contact's stored WhatsApp number
          COMMIT and CLOSE the transaction
          Agent Core: process_turn(...) -> the tool loop, under ONE deadline
            <=6 model calls, each offered the EIGHT tools
            <=1 booking change per patient message
            tool calls -> registry (Pydantic validation) -> BookingClient /
              PatientBookingClient, every write carrying an idempotency key
          the reply guard: a claim the turn's own outcome does not support ->
            PERMANENT agent_unconfirmed_claim, and the fallback is sent instead
    T1r   ONLY on the retry path, and only when the turn made a booking change:
          lock the conversation row, record the run and the outcome, COMMIT, retry
    T1b   re-check conversation state (the authoritative hard rule 7 read;
            FOR UPDATE, and FIRST, when the turn carries a booking outcome)
          reserve the reply row WITH the generated text + the receipt line
          record agent_runs + tool_executions (SAVEPOINT)
          record booking_actions (SAVEPOINT), then any booking dead letters
          COMMIT
          send the STORED text via Meta
    T2    save the wamid, SENT and sent_at
```

`sent_at` is what the confirmation gate reads: a change may only be executed while
answering a message stored AFTER a reply of ours was really sent.

## Voice note flow (later)
```
audio message -> media id -> fetch media URL -> download -> transcribe
             -> same process_turn(modality=VOICE_NOTE) -> text reply (optional TTS -> OGG/Opus)
```

## Agent Core contract
```python
process_turn(turn: Turn, chat: ChatClient, runtime: AgentRuntime) -> AgentResult
```

`runtime` arrived in VS-006: it carries the `BookingClient`, the clock, the turn
budget and the tool registry. Bundling them keeps this signature from growing a
parameter per slice. VS-007 adds `patient_bookings`, the `PatientBookingClient` for
everything done on behalf of one patient; the worker passes the same object as
`booking`. A turn without one has no patient side at all, and a booking tool then
crashes the turn rather than acting for a guessed identity.

`Turn` carries the five fields this contract always named — `tenant_id`,
`contact_id`, `conversation_id`, `modality`, `input_text` — plus `history`, the
earlier messages of the conversation as plain data.

VS-007 adds four more, all loaded in T1 and none of them ever sent to the model:
`inbox_event_id` (the idempotency key's source), `inbound_message_id` (which message
the gate is answering), `booking_state` (the conversation's latest prepared change,
with the gate's verdict already computed) and `patient_reference` — the patient's
WhatsApp number, which is how the Booking Service asked to identify them. It is
`repr=False`: it reaches the service in a header and the one-way idempotency hash,
and nothing else of ours.

**`process_turn` does no database access.** The caller loads the history and
commits before the model is called, because `messages`' insert row-locks the
conversation and a staff takeover must never wait for OpenAI. `app/agent/`
imports no session, no repository and no model, and a test enforces it.

`chat` is a `ChatClient` (`app/integrations/openai/interface.py`): one attempt,
never a retry, and a classified result rather than an exception. The OpenAI SDK
lives behind it in exactly one module.

AgentResult holds the reply text, the outcome and reason, the prompt version,
the token counts, the number of model calls, and **the tool-call records** - as
plain data, for the JOB to persist in T1b. `app/agent/` cannot open a
transaction, which is why it returns them rather than writing them. The handoff
flag arrives in VS-010. It knows nothing about WhatsApp, so later calls/voice
reuse it unchanged.

VS-007 adds `booking_outcome`: **at most one per turn**, the change the turn made,
again as plain data. It carries the receipt line the job appends to the reply, the
idempotency key the job stores, and the ids the job writes to `booking_actions` —
all of which `app/agent/` can produce and none of which it can persist.

## The tool loop (VS-006, extended in VS-007)

The model REQUESTS, our code EXECUTES. Each model call is given all eight tools -
four read-only, four that change something at the Booking Service; the model may
answer with text or ask for tools, and it never runs anything itself. Everything
it asks for is untrusted input, exactly like a patient's message.

**Three things the CODE enforces, not the prompt** (VS-007):

1. a change is *prepared* while answering one message and can only be *executed*
   while answering a later one, after a reply of ours was really sent in between;
2. at most one change per patient message;
3. a reply may claim a booking, change or cancellation only when the turn's own
   tool result says so - checked by scanning the model's words BEFORE the receipt
   is appended, so our own symbols are never mistaken for the model's.

The receipt line (`✅ Dr. … · 2026-09-30 14:00 · #K7Q2M9`) is built only from the
Booking Service's answer, and the prompt forbids the model to write those symbols.

```
MAX_MODEL_CALLS                  6    model calls per turn (a constant, not a setting)
MAX_BOOKING_CHANGES_PER_TURN     1    one change per patient message (VS-007)
MIN_SECONDS_FOR_A_BOOKING_CHANGE 8.0  no change STARTS with less turn left
AGENT_TURN_TIMEOUT_SECONDS       45   ONE deadline around the whole loop
OPENAI_TIMEOUT_SECONDS           30   one model call, INSIDE that budget
```

`tenant_id` reaches the tools through `ToolContext`, built by our code. It is in
no schema, no argument, no result and no error (hard rule 4), and a `tenant_id`
the model invents is refused by `extra="forbid"` and reported back. VS-007 adds a
`PatientContext` beside it, carrying who the patient is and what they have
prepared. The patient reference - their WhatsApp number - is in no schema, result,
log line, table or dead letter either.

Every tool call the model asks for gets a `tool` message - OpenAI requires one
per id - and a `tool_executions` row, executed or not.

## Conversation states
AI_ACTIVE -> HUMAN_REQUESTED -> HUMAN_ACTIVE -> CLOSED

## WhatsApp constraints to remember
- Free-form replies are only allowed within 24h of the patient's last message.
  Outside that window (e.g. reminders), you must use pre-approved message templates.
- Meta may deliver the same webhook more than once, and out of order.
