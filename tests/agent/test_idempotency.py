"""Idempotency keys for booking-changing calls (VS-007, plan section 5.2).

Hard rule 6: every booking-changing call sends an idempotency key derived from
the source message id. The key's whole job is to survive a retry: a network call
can fail in a way that hides whether it worked, and the only safe retry is one the
Booking Service can recognise as the same request.

So the properties that matter are not "it is a hash". They are:

- the SAME intent retried gives the SAME key, whatever order our dicts happen to
  be in and however the patient's name is spelled in Unicode;
- a DIFFERENT intent gives a different key, field by field;
- the key reveals nothing, so it can be stored and shown to a human;
- it cannot be built from a wamid, which decodes to the patient's phone number.

Pure: no database, no network, no clock.
"""

import json
import re
import unicodedata
import uuid

import pytest

from app.agent.tools.idempotency import (
    KEY_PREFIX,
    WRITE_TOOLS,
    canonical_json,
    idempotency_key,
)

# A fixed inbox row uuid, so every key in this file is reproducible.
INBOX = uuid.UUID("11111111-2222-4333-8444-555555555555")
OTHER_INBOX = uuid.UUID("99999999-8888-4777-8666-555555555555")
PATIENT = "00000000-0000-4000-8000-000000000001"

HOLD_REQUEST = {"patient_ref": PATIENT, "slot_id": "slot_abcdef"}
BOOK_REQUEST = {"full_name": "Rami Khoury", "hold_id": "hold_1", "patient_ref": PATIENT}

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_a_key_is_sixty_four_lowercase_hex_characters():
    """The shape the contract proposal promises: 64 lowercase hex characters, so
    it fits a `VARCHAR(64)` column and an HTTP header without encoding."""
    key = idempotency_key(INBOX, "hold_appointment_slot", HOLD_REQUEST)

    assert HEX64.match(key), key
    assert len(key) == 64


def test_the_same_intent_gives_the_same_key_whatever_the_field_order():
    """The retry property, and why `canonical_json` sorts.

    Our request dict is built by Python code whose insertion order can change
    between two versions of the same function. If that changed the key, a retry
    after a deploy would look like a new request and book twice.
    """
    forwards = idempotency_key(INBOX, "book_appointment", BOOK_REQUEST)
    backwards = idempotency_key(INBOX, "book_appointment", dict(reversed(BOOK_REQUEST.items())))

    assert forwards == backwards


@pytest.mark.parametrize(
    ("inbox", "tool", "request_body"),
    [
        (OTHER_INBOX, "book_appointment", BOOK_REQUEST),
        (INBOX, "cancel_appointment", BOOK_REQUEST),
        (INBOX, "book_appointment", {**BOOK_REQUEST, "full_name": "Rami Khouri"}),
        (INBOX, "book_appointment", {**BOOK_REQUEST, "hold_id": "hold_2"}),
        (INBOX, "book_appointment", {**BOOK_REQUEST, "patient_ref": "another-contact"}),
    ],
    ids=["inbox row", "tool", "full_name", "hold_id", "patient_ref"],
)
def test_the_key_changes_with_the_inbox_row_the_tool_and_every_field(inbox, tool, request_body):
    """A different intent must be a different key, or the service would replay an
    answer to a request nobody made."""
    baseline = idempotency_key(INBOX, "book_appointment", BOOK_REQUEST)

    assert idempotency_key(inbox, tool, request_body) != baseline


def test_dropping_a_field_changes_the_key():
    """The key covers the WHOLE body (V1), so a missing field is a different
    request rather than the same one with a default."""
    full = idempotency_key(INBOX, "book_appointment", BOOK_REQUEST)
    without_name = idempotency_key(
        INBOX, "book_appointment", {k: v for k, v in BOOK_REQUEST.items() if k != "full_name"}
    )

    assert full != without_name


def test_the_key_reveals_neither_the_inbox_row_nor_any_value():
    """Why the key is safe to store in `booking_actions` and to put in a dead
    letter a human reads: it is a one-way hash over a random uuid."""
    key = idempotency_key(
        INBOX,
        "book_appointment",
        {"full_name": "SENTINEL-NAME", "hold_id": "SENTINEL-HOLD", "patient_ref": "SENTINEL-REF"},
    )

    assert "SENTINEL" not in key
    assert str(INBOX) not in key
    assert INBOX.hex not in key
    # Not even a fragment: the hash is the only thing in it.
    assert INBOX.hex[:8] not in key


def test_a_key_cannot_be_made_from_a_string_id():
    """A wamid is a string, and a wamid decodes to the patient's phone number
    (plan conflict C1). Requiring a `uuid.UUID` makes passing one a TypeError
    rather than a silent privacy leak into the Booking Service's logs."""
    wamid = "wamid.HBgLOTYxNzAxMjM0NTYVAgASGBQzQTAwMDAwMDAwMDAwMDAwMDAwMAA="

    with pytest.raises(TypeError):
        idempotency_key(wamid, "book_appointment", BOOK_REQUEST)
    with pytest.raises(TypeError):
        idempotency_key(str(INBOX), "book_appointment", BOOK_REQUEST)


def test_the_error_for_a_string_id_does_not_echo_it():
    """The refusal must not write the wamid down either."""
    with pytest.raises(TypeError) as raised:
        idempotency_key("wamid.SENTINEL", "book_appointment", BOOK_REQUEST)

    assert "SENTINEL" not in str(raised.value)


def test_only_the_four_changing_tools_have_keys():
    """A key on a read would be meaningless, and asking for one is a bug worth a
    loud failure rather than a wasted hash."""
    assert WRITE_TOOLS == {
        "hold_appointment_slot",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    }

    for tool in sorted(WRITE_TOOLS):
        assert HEX64.match(idempotency_key(INBOX, tool, HOLD_REQUEST))

    for tool in ("search_available_slots", "list_doctors", "list_my_appointments", ""):
        with pytest.raises(ValueError):
            idempotency_key(INBOX, tool, HOLD_REQUEST)


def test_nfc_and_nfd_spellings_give_the_same_key():
    """The patient's name comes from the model, and a re-run can emit the same
    name in a different Unicode normal form.

    Without NFC normalisation that would be the same intent under a different key,
    which is the one case the service reads as `IDEMPOTENCY_CONFLICT` - an unknown
    outcome we would have to tell a human about, for nothing.
    """
    composed = unicodedata.normalize("NFC", "Zoé Haddad")
    decomposed = unicodedata.normalize("NFD", "Zoé Haddad")
    assert composed != decomposed  # the inputs really do differ

    key_of = lambda name: idempotency_key(  # noqa: E731
        INBOX, "book_appointment", {**BOOK_REQUEST, "full_name": name}
    )

    assert key_of(composed) == key_of(decomposed)


def test_canonical_json_sorts_keys_and_keeps_unicode():
    """`canonical_json` is the body the key covers AND the shape the body hash at
    the service would cover: sorted, compact, and never escaped to ASCII."""
    rendered = canonical_json({"slot_id": "slot_1", "full_name": "Zoé", "patient_ref": "ref"})

    assert rendered == '{"full_name":"Zoé","patient_ref":"ref","slot_id":"slot_1"}'
    assert "\\u" not in rendered
    assert " " not in rendered.replace('"Zoé"', "")  # no whitespace between tokens
    assert json.loads(rendered) == {"slot_id": "slot_1", "full_name": "Zoé", "patient_ref": "ref"}


def test_canonical_json_normalises_every_value():
    decomposed = unicodedata.normalize("NFD", "Zoé")

    assert canonical_json({"full_name": decomposed}) == canonical_json({"full_name": "Zoé"})


def test_canonical_json_refuses_a_value_that_is_not_a_string():
    """Every field our code sends is a string. A number or a nested object would
    hash differently here than at a service that serialised it its own way."""
    with pytest.raises(TypeError):
        canonical_json({"slot_id": 1})


def test_the_key_prefix_is_versioned_and_pinned():
    """Changing this string changes every key.

    A deploy that changed it while a retry was in flight would send a NEW key for
    the same intent: the service would run the change a second time. The version
    in it is what makes a deliberate change possible - and this test is what makes
    an accidental one fail.
    """
    assert KEY_PREFIX == "doctoleb/booking-idempotency/v1"
    assert (
        idempotency_key(INBOX, "hold_appointment_slot", HOLD_REQUEST)
        == "227b19b092b3ff3f482a2995c51edb4e53b3b835e622e52e67f18c651edba1db"
    )
