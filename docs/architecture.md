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
          Agent Core: process_turn(...) -> OpenAI with tools
            tool calls -> BookingClient -> Booking Service
          re-check conversation state
          send reply via Meta, store outgoing message
```

## Voice note flow (later)
```
audio message -> media id -> fetch media URL -> download -> transcribe
             -> same process_turn(modality=VOICE_NOTE) -> text reply (optional TTS -> OGG/Opus)
```

## Agent Core contract
```python
process_turn(tenant_id, contact_id, conversation_id, modality, input_text) -> AgentResult
```
AgentResult holds the reply text, tool calls made, and whether handoff was requested.
It knows nothing about WhatsApp, so later calls/voice reuse it unchanged.

## Conversation states
AI_ACTIVE -> HUMAN_REQUESTED -> HUMAN_ACTIVE -> CLOSED

## WhatsApp constraints to remember
- Free-form replies are only allowed within 24h of the patient's last message.
  Outside that window (e.g. reminders), you must use pre-approved message templates.
- Meta may deliver the same webhook more than once, and out of order.
