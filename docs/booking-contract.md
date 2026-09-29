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
