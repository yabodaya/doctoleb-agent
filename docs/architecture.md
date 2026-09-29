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
          resolve tenant from phone_number_id
          upsert contact + conversation, store message
          if conversation not AI_ACTIVE -> stop
          load the recent history, COMMIT and CLOSE the transaction
          Agent Core: process_turn(...) -> the tool loop, under ONE deadline
            <=4 model calls, each offered the three read-only tools
            tool calls -> registry (Pydantic validation) -> BookingClient
          re-check conversation state (the authoritative hard rule 7 read)
          reserve the reply row WITH the generated text
          record agent_runs + tool_executions (SAVEPOINT), commit
          send the STORED text via Meta, then save the wamid
```

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
parameter per slice.
`Turn` carries the five fields this contract always named — `tenant_id`,
`contact_id`, `conversation_id`, `modality`, `input_text` — plus `history`, the
earlier messages of the conversation as plain data.

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

## The tool loop (VS-006)

The model REQUESTS, our code EXECUTES. Each model call is given three read-only
tools; the model may answer with text or ask for tools, and it never runs
anything itself. Everything it asks for is untrusted input, exactly like a
patient's message.

```
MAX_MODEL_CALLS            4      model calls per turn (a constant, not a setting)
AGENT_TURN_TIMEOUT_SECONDS 45     ONE deadline around the whole loop
OPENAI_TIMEOUT_SECONDS     30     one model call, INSIDE that budget
```

`tenant_id` reaches the tools through `ToolContext`, built by our code. It is in
no schema, no argument, no result and no error (hard rule 4), and a `tenant_id`
the model invents is refused by `extra="forbid"` and reported back.

Every tool call the model asks for gets a `tool` message - OpenAI requires one
per id - and a `tool_executions` row, executed or not.

## Conversation states
AI_ACTIVE -> HUMAN_REQUESTED -> HUMAN_ACTIVE -> CLOSED

## WhatsApp constraints to remember
- Free-form replies are only allowed within 24h of the patient's last message.
  Outside that window (e.g. reminders), you must use pre-approved message templates.
- Meta may deliver the same webhook more than once, and out of order.
