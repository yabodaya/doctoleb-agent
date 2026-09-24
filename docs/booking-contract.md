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

## Open questions for the Booking Service owner
1. Who owns the tenant <-> WhatsApp number mapping?
2. How is a patient identified across services: our contact_id, phone number, or a patient id you create?
3. Does booking need doctor/staff approval (pending state) or is it confirmed immediately?
4. Hold duration?
5. Timezone handling: assume Asia/Beirut, return ISO 8601 with offset?
6. How does the dashboard read our conversations: our API, or a shared DB?
