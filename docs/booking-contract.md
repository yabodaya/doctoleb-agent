# Booking Service contract. DRAFT: agree on this with the Booking Service owner.

This repo calls the Booking Service over HTTP. Until it exists, `FakeBookingClient`
implements the same interface with seeded in-memory data, so work is not blocked.

## Auth and tenancy
- Service-to-service auth: `Authorization: Bearer <SERVICE_TOKEN>` (env var).
- Tenant: `X-Tenant-Id` header, set by our backend from the phone_number_id mapping. Never from the LLM.
- Booking-changing calls: `Idempotency-Key` header (we derive it from the WhatsApp message id).

## Endpoints (proposal)
| Method | Path | Purpose |
|---|---|---|
| GET | /tenants/by-whatsapp/{phone_number_id} | resolve tenant (OR we keep this mapping ourselves; decide) |
| GET | /clinic | name, hours, locations, policies, pricing info |
| GET | /doctors | list doctors + services |
| GET | /slots?doctor_id=&service_id=&from=&to= | available slots |
| POST | /holds | {slot_id, patient_ref} -> {hold_id, expires_at} |
| POST | /appointments | {hold_id, patient} -> {appointment_id, status} |
| POST | /appointments/{id}/reschedule | {new_hold_id} |
| POST | /appointments/{id}/cancel | {} |
| GET | /appointments?patient_ref= | patient's upcoming appointments |

## Errors (proposal)
409 SLOT_TAKEN, 410 HOLD_EXPIRED, 404 NOT_FOUND, 422 VALIDATION, 5xx -> we retry with backoff.
Body: `{ "error": { "code": "...", "message": "..." } }`

## Proposal: shapes the FakeBookingClient implements (VS-006)

**Not agreed yet.** The endpoint table above lists paths but no response bodies,
so these are this repo's proposal, implemented by `FakeBookingClient` and ready
for `HttpBookingClient` to parse. Please confirm or correct them.

```
GET /clinic   -> {name, timezone, locations[{name, address}],
                  opening_hours[{weekday 0=Mon, opens "HH:MM"|null,
                                 closes "HH:MM"|null}],
                  policies[str], pricing[str]}
GET /doctors  -> [{doctor_id, name, specialty,
                   services[{service_id, name, duration_minutes}]}]
GET /slots    -> [{slot_id, doctor_id, start, end, service_id?}]
```

- `start` and `end` are **ISO 8601 with an offset**, never naive (open question
  5). `timezone` is an IANA name, carried per clinic rather than assumed.
- `opens: null` means the clinic is closed that day; a closed day carries no
  times at all.
- `/slots` accepts `service_id`; VS-006 never sends one.
- Errors read as `{"error": {"code", "message"}}`. We keep the **code** and
  never read the `message`: it comes from your system and can quote clinic or
  patient data, and ours reaches logs and the model. 404 -> `NOT_FOUND`,
  422 -> `VALIDATION`, 5xx -> `UNAVAILABLE`.

### A note on the tenant id

`X-Tenant-Id` carries an **opaque string** (VS-006 decision D1). This repo never
parses, normalises, reformats or case-folds it: whatever you tell us to send is
sent exactly as configured, and `"Clinic-Alpha"` and `"clinic-alpha"` are two
different clinics. Because it is an HTTP header value we refuse any tenant id
containing CR/LF or other non-printable characters, and any with leading or
trailing whitespace. A UUID is fine; so is a clinic username. **Please tell us
the format you settle on** - we will not assume one.

## Open questions for the Booking Service owner
1. Who owns the tenant <-> WhatsApp number mapping?
2. How is a patient identified across services: our contact_id, phone number, or a patient id you create?
3. Does booking need doctor/staff approval (pending state) or is it confirmed immediately?
4. Hold duration?
5. Timezone handling: assume Asia/Beirut, return ISO 8601 with offset?
6. How does the dashboard read our conversations: our API, or a shared DB?

## Proposal: the write side (VS-007)

**Not agreed yet.** The endpoint table above names the write endpoints but gives
no request or response bodies, no idempotency semantics and no rule for a write
whose answer is lost. What follows is this repo's proposal, implemented by
`InMemoryBookingService` (an in-memory stand-in) and ready for
`HttpBookingClient` to speak. Please confirm or correct each point. No line above
this section has changed.

### 1. Idempotency

`Idempotency-Key` is **64 lowercase hex characters**. We derive it as
`sha256_hex("doctoleb/booking-idempotency/v1" \n <our webhook_inbox row UUID> \n
<tool name> \n <canonical JSON of the request body>)`, where the canonical JSON
has sorted keys, no whitespace and every value NFC-normalised.

This **supersedes the parenthesis "we derive it from the WhatsApp message id"**
in *Auth and tenancy* above: we never use a wamid, because a wamid decodes to the
patient's phone number and the key reaches your logs. Our `webhook_inbox` row is
1:1 with the source message, so the key is still derived from the source message
in substance, and it is identical across every retry and every duplicate delivery
of that message.

We ask that keys be:

- scoped **per tenant**;
- kept for **at least 24 hours**;
- **same key, same body** -> the stored answer, whether that was a success or a
  business error;
- **same key, different body** -> `422 IDEMPOTENCY_CONFLICT`;
- **a retry while the original is still processing** -> `409 REQUEST_IN_PROGRESS`.

(This follows the IETF draft `draft-ietf-httpapi-idempotency-key-header`.)

### 2. Natural idempotency per target

Keys alone are not enough: a retried turn is regenerated by a language model, so
the patient's name may be spelled differently and the key changes. We therefore
ask for idempotency **per target** as well. For the same patient:

1. holding a slot the patient already holds returns **that hold**;
2. booking a hold the patient already converted returns **that appointment**;
3. rescheduling onto a hold already applied to that appointment returns **that
   appointment**;
4. cancelling an already-cancelled appointment returns **it, cancelled**.

A *different* patient still gets `SLOT_TAKEN` or `404`.

### 3. Patient reference

An opaque string of ours. Today it is our **contact row UUID**: stable per clinic
and per phone number, and not personal data by itself. We do **not** send a phone
number. This is contract open question 2: please tell us whether you need one,
and what for.

### 4. The five calls

| Call | Request | Response |
|---|---|---|
| `GET /appointments?patient_ref=` | the ref | `[{appointment_id, reference, doctor_id, doctor_name, start, end, status}]`, upcoming only, soonest first |
| `POST /holds` | `{slot_id, patient_ref}` | `{hold_id, slot_id, doctor_id, doctor_name, start, end, expires_at}` |
| `POST /appointments` | `{hold_id, patient: {ref, name}}` | one appointment, as above |
| `POST /appointments/{id}/reschedule` | `{new_hold_id, patient_ref}` | the appointment, same `appointment_id` and same `reference` |
| `POST /appointments/{id}/cancel` | `{patient_ref}` | the appointment with `status: "CANCELLED"` |

`start`, `end` and `expires_at` are **ISO 8601 with an offset**, never naive.
`reference` is a short code we show the patient; `doctor_name` is on every
response so that a confirmation line needs no second read. `name` is the
patient's own answer, sent only on `POST /appointments`; we never store it.

### 5. Holds

We assume a **10-minute TTL** (open question 4). We also assume: **one active
hold per patient**, a new one releasing the previous; and that a hold converts
into **at most one** appointment. Please correct any of these.

### 6. Ownership

Another patient's hold or appointment must be **`404`, not `403`**, so that its
existence is not confirmed to someone who guessed an id.

### 7. Errors

| Status | `code` | Meaning |
|---|---|---|
| 404 | `NOT_FOUND` | no such doctor, slot, hold or appointment **for this tenant and patient** |
| 422 | `VALIDATION` | values refused |
| 409 | `SLOT_TAKEN` | another patient holds or booked that slot |
| 410 | `HOLD_EXPIRED` | the hold ran out or was released |
| 422 | `IDEMPOTENCY_CONFLICT` | this key was used before with a different body |
| 409 | `REQUEST_IN_PROGRESS` | a retry arrived while the original was still running |
| 5xx | — | see point 8 |

Same body shape as above, `{"error": {"code", "message"}}`. We keep the **code**
and never read the `message`.

### 8. How we classify a failure

- **Reads** (`GET /clinic`, `/doctors`, `/slots`, `/appointments`): a timeout, a
  lost connection or a 5xx is `UNAVAILABLE`. Nothing changed, so we tell the
  patient we could not check.
- **Writes**: a timeout, a lost connection, `409 REQUEST_IN_PROGRESS` and
  `422 IDEMPOTENCY_CONFLICT` are **`UNKNOWN_OUTCOME`**. So is any 5xx, unless you
  guarantee that no work was done. On an unknown outcome we tell the patient
  neither that it worked nor that it failed, and the clinic team is alerted to
  check. **Please tell us which 5xx responses are safe to treat as "nothing
  happened".**

### 9. Approval

Open question 3. We carry a `status` of `CONFIRMED`, `PENDING_APPROVAL` or
`CANCELLED`, and we **never call a `PENDING_APPROVAL` appointment "booked"** to
the patient.

### 10. Path ids

Ids we put in a path are percent-encoded by our client (VS-011), and ids we issue
never contain `/`, `?`, `#` or `%`. Please keep yours to
`[A-Za-z0-9._:+=~-]` so that a path never needs interpreting.
