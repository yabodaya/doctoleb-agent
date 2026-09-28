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
          Agent Core: process_turn(...) -> OpenAI with tools
            tool calls -> BookingClient -> Booking Service
          re-check conversation state (the authoritative hard rule 7 read)
          reserve the reply row WITH the generated text, commit
          send the STORED text via Meta, then save the wamid
```

## Voice note flow (later)
```
audio message -> media id -> fetch media URL -> download -> transcribe
             -> same process_turn(modality=VOICE_NOTE) -> text reply (optional TTS -> OGG/Opus)
```

## Agent Core contract
```python
process_turn(turn: Turn, chat: ChatClient) -> AgentResult
```
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

AgentResult holds the reply text, the outcome and reason, the prompt version and
the token counts. Tool calls arrive in VS-006 and the handoff flag in VS-010.
It knows nothing about WhatsApp, so later calls/voice reuse it unchanged.

## Conversation states
AI_ACTIVE -> HUMAN_REQUESTED -> HUMAN_ACTIVE -> CLOSED

## WhatsApp constraints to remember
- Free-form replies are only allowed within 24h of the patient's last message.
  Outside that window (e.g. reminders), you must use pre-approved message templates.
- Meta may deliver the same webhook more than once, and out of order.
