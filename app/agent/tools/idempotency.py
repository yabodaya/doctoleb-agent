"""Idempotency keys for booking-changing calls (hard rule 6, plan section 5.2).

**The problem this solves.** A network call can fail in a way that hides whether
it worked: the request reached the Booking Service, the booking was made, and the
answer was lost on the way back. Retrying blindly books twice. Not retrying may
leave the patient with nothing.

An idempotency key fixes it from the server side. The client sends a key with the
request, and the server remembers "this key -> the answer I gave". A second
request with the same key and the same body gets the *same answer* back, without a
second booking.

**So the key must be identical every time we retry the same intent, and different
for a different intent.** That is the whole design:

- it is derived from things that do not change between retries - our
  `webhook_inbox` row for the source message (hard rule 2 dedupes every duplicate
  delivery to exactly one row), the tool name, and the exact body we send;
- it is derived from nothing that does change - no timestamp, no random value, no
  attempt counter, and not the model's wording (only `slot_id` and `full_name`
  come from the model, and both are validated and normalised before they get
  here);
- the tenant is NOT in it: it travels as the `X-Tenant-Id` header, and keys are
  scoped per tenant at the service.

**Never a wamid.** `docs/booking-contract.md` originally said "we derive it from
the WhatsApp message id". A wamid decodes to the patient's phone number, and the
key reaches the Booking Service's logs, so we use our own row's UUID instead. It
is 1:1 with the source message, so hard rule 6 holds in substance. Requiring a
`uuid.UUID` is what makes passing a wamid a `TypeError` rather than a silent leak.

This module is PURE: `hashlib`, `json`, `unicodedata`, `uuid`. It reads no clock,
opens no connection and imports nothing from the rest of the app, so it is safe
inside `app/agent/` (hard rule 3).
"""

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from uuid import UUID

# Pinned by a test. Changing it changes every key, so a deploy that changed it
# while a retry was in flight would send a NEW key for the same intent and the
# service would apply the change twice. The version is what makes a deliberate
# change possible; the test is what makes an accidental one fail.
KEY_PREFIX = "doctoleb/booking-idempotency/v1"

# Only these four change anything at the Booking Service, so only these four have
# a key. Asking for a key for a read is a bug in our code, not a value to invent.
WRITE_TOOLS = frozenset(
    {
        "hold_appointment_slot",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    }
)


def canonical_json(request: Mapping[str, str]) -> str:
    """The request body, in one spelling: sorted keys, no whitespace, NFC values.

    Three choices, each for a reason:

    - **sorted keys**, because our body is built by Python code whose insertion
      order can change between two versions of the same function, and a retry
      after a deploy must not look like a new request;
    - **no whitespace**, so the rendering is the shortest one that round-trips;
    - **NFC on every value**, because the patient's name comes from the model and a
      re-run can emit the same name in a different Unicode normal form. Without
      this, the same intent would carry a different key and the service would
      answer `IDEMPOTENCY_CONFLICT` - an unknown outcome a human would have to
      check, for nothing.

    `ensure_ascii=False` keeps Arabic and accented names as themselves rather than
    as `\\uXXXX` escapes, so the bytes we hash are the bytes we send.

    Every value must already be a `str`. A number or a nested object would hash
    differently here than at a service that serialised it its own way, so it
    raises `TypeError` rather than guessing.
    """
    normalised = {}
    for key, value in request.items():
        if not isinstance(value, str):
            raise TypeError("every value in an idempotency request must be a str")
        normalised[key] = unicodedata.normalize("NFC", value)
    return json.dumps(normalised, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def idempotency_key(inbox_event_id: UUID, tool_name: str, request: Mapping[str, str]) -> str:
    """`sha256_hex(KEY_PREFIX \\n inbox_event_id \\n tool_name \\n canonical_json(request))`.

    64 lowercase hex characters: it fits a `VARCHAR(64)` column and an HTTP header
    with no encoding, and it reveals nothing. It is a one-way hash over a random
    UUID, which is why it can be stored in `booking_actions` and put in a dead
    letter for a human to quote back to the Booking Service (hard rule 8).

    `request` is exactly the body our code sends, minus the tenant: the validated,
    normalised arguments plus the ids our code injected (`patient_ref`, and
    `hold_id` or `appointment_id` read from `booking_actions`). Because the key
    covers the whole body, "same key, different body" can only come from a bug in
    this repo - and the service reports that as `IDEMPOTENCY_CONFLICT`, so we would
    notice.

    Raises `TypeError` for anything but a `uuid.UUID` (see the module docstring:
    that is the wamid guard) and `ValueError` for a tool that changes nothing.
    Neither message quotes its input.
    """
    if not isinstance(inbox_event_id, UUID):
        raise TypeError("inbox_event_id must be a uuid.UUID, never a provider message id")
    if tool_name not in WRITE_TOOLS:
        raise ValueError("no idempotency key exists for a tool that changes nothing")

    material = "\n".join((KEY_PREFIX, str(inbox_event_id), tool_name, canonical_json(request)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


__all__ = ["KEY_PREFIX", "WRITE_TOOLS", "canonical_json", "idempotency_key"]
