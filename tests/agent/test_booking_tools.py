"""The five booking tools, against the in-memory Booking Service (Task B2).

No database and no model: each test builds a `PatientContext` directly and calls
one tool, so what is under test is the TOOL's decisions rather than the loop's.
The loop's rules are `tests/agent/test_booking_loop.py`.

Every test builds its OWN service: an `asyncio.Lock` binds to the loop it is first
contended in (plan check U5), and `counter_ids()` makes the ids readable so an
assertion can name one.

The three things these tests are really about:

- **the gate** (V3): nothing is EXECUTED unless a reply describing it was already
  sent, and the refusal says which of the three reasons applied;
- **the key** (hard rule 6): every changing call carries a key derived from our
  inbox row, and a test recomputes it independently;
- **what the model is shown**: an `appointment_id` and a `slot_id`, and never a
  `hold_id`, a reference code, a key, the tenant or the patient reference.
"""

import datetime as dt
import json
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.agent.tools import default_registry
from app.agent.tools.appointments import ListMyAppointments
from app.agent.tools.base import (
    BookingState,
    ChangePhase,
    ChangeStatus,
    PatientContext,
    ToolContext,
)
from app.agent.tools.changes import (
    BookAppointment,
    CancelAppointment,
    RescheduleAppointment,
)
from app.agent.tools.errors import ToolCrashed, ToolFailure
from app.agent.tools.holds import HoldAppointmentSlot
from app.agent.tools.idempotency import idempotency_key
from app.db.enums import BookingActionKind, BookingActionStatus
from app.integrations.booking import BookingError, PatientRef
from app.integrations.booking.fake import FakeBookingClient
from app.integrations.booking.memory import FailureScript, InMemoryBookingService
from tests.integrations.booking_fakes import RecordingBooking, counter_ids

NOW = dt.datetime(2026, 9, 29, 7, tzinfo=dt.UTC)  # Tuesday 29 Sep 2026, 10:00 local
TENANT = "clinic-alpha"
# The patient reference is the WhatsApp phone number (V14 as the developer
# overrode it). Synthetic, in a documentation-safe range.
PHONE = "96170000001"
OTHER_PHONE = "96170000002"
INBOX = uuid.UUID("11111111-2222-4333-8444-555555555555")
MESSAGE = uuid.UUID("22222222-3333-4444-8555-666666666666")
SECRET = b"a fixed secret for the booking tool tests"


def service(failures: FailureScript | None = None) -> InMemoryBookingService:
    return InMemoryBookingService(
        FakeBookingClient.demo(clock=lambda: NOW),
        lambda: NOW,
        id_secret=SECRET,
        new_id=counter_ids(),
        failures=failures,
    )


def patient_context(
    bookings: Any,
    *,
    state: BookingState | None = None,
    phone: str = PHONE,
) -> PatientContext:
    return PatientContext(
        bookings=bookings,
        patient=PatientRef(phone),
        inbox_event_id=INBOX,
        inbound_message_id=MESSAGE,
        state=state,
    )


def context(bookings: Any, patient: PatientContext | None = None) -> ToolContext:
    return ToolContext(TENANT, bookings, NOW, patient=patient, remaining=lambda: 30.0)


def pending(
    kind: BookingActionKind,
    *,
    confirmable: bool = True,
    hold_id: str | None = "hold_1",
    appointment_id: str | None = None,
    status: BookingActionStatus = BookingActionStatus.PENDING,
) -> BookingState:
    return BookingState(
        action_id=uuid.uuid4(),
        kind=kind,
        status=status,
        confirmable=confirmable,
        hold_id=hold_id,
        appointment_id=appointment_id,
    )


BEIRUT = ZoneInfo("Asia/Beirut")


async def karim_slots(bookings: Any, tenant: str = TENANT) -> tuple:
    """Dr. Karim's Wednesday afternoon: 14:00, 14:20, 15:40, 16:20.

    The window is CLINIC-LOCAL, because the fake builds slots from a weekly pattern
    of local times. A UTC window looks equivalent and is not: 12:00-17:00 UTC is
    15:00-20:00 in Beirut, which is after the clinic closes.
    """
    return await bookings.search_slots(
        tenant,
        "doc_karim",
        dt.datetime(2026, 9, 30, 12, tzinfo=BEIRUT),
        dt.datetime(2026, 9, 30, 17, tzinfo=BEIRUT),
    )


async def run(tool: Any, arguments: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Validate the arguments the way the registry does, then run the tool."""
    args = tool.args_model.model_validate(arguments, context={"now": ctx.now})
    return await tool.run(args, ctx)


async def booked(bookings: Any, patient: PatientContext, slot_id: str) -> dict[str, Any]:
    """Hold a slot and book it, the way two messages would.

    The second `PatientContext` is what the second message's turn would build: a
    fresh one, whose state came from `booking_actions` with `confirmable=True`.
    """
    await run(HoldAppointmentSlot(), {"slot_id": slot_id}, context(bookings, patient))
    outcome = patient.outcome
    assert outcome is not None
    second = patient_context(
        bookings,
        state=pending(BookingActionKind.BOOK, hold_id=outcome.hold_id),
        phone=patient.patient.value,
    )
    result = await run(BookAppointment(), {"full_name": "Rami Khoury"}, context(bookings, second))
    return {"result": result, "patient": second}


# --- schemas ------------------------------------------------------------------


def test_no_tool_takes_an_identity_or_a_contact_detail():
    """Hard rules 4 and 8, as a grep over everything the model is sent.

    The tenant and the patient are injected by our code, the `hold_id` is read from
    `booking_actions`, and the idempotency key is derived - so every one of these
    would be a way for the model to name something it must not choose.
    """
    forbidden = ("phone", "wa_id", "contact", "patient", "tenant", "hold_id", "idempotency")
    for spec in default_registry().specs():
        properties = spec.parameters.get("properties", {})
        for name in properties:
            assert not any(name.startswith(bad) for bad in forbidden), f"{spec.name}.{name}"
        rendered = json.dumps(spec.parameters).lower()
        for bad in ("tenant", "hold_id", "idempotency", "wa_id"):
            assert bad not in rendered, f"{spec.name}: {bad}"


def test_every_changing_tool_is_marked_and_no_read_is():
    """`changes_bookings` decides how a failure is recorded: `UNCERTAIN` from a
    write, `booking_unavailable` from a read (V6). A read marked True would start
    dead-lettering failures nobody needs to check; a write marked False would
    record "it did not happen" about a change that may have."""
    marks = {tool.name: tool.changes_bookings for tool in default_registry()._tools}  # noqa: SLF001

    assert marks == {
        "get_clinic_information": False,
        "list_doctors": False,
        "search_available_slots": False,
        "list_my_appointments": False,
        "hold_appointment_slot": True,
        "book_appointment": True,
        "reschedule_appointment": True,
        "cancel_appointment": True,
    }


def test_id_arguments_share_the_opaque_id_pattern():
    """V10. One pattern, so "14:00 tomorrow" is invalid ARGUMENTS everywhere rather
    than a lookup that might guess, and no id can carry a path separator."""
    from app.agent.tools.base import OPAQUE_ID

    found = 0
    for spec in default_registry().specs():
        for name, schema in spec.parameters.get("properties", {}).items():
            if not name.endswith("_id") or name == "doctor_id":
                continue
            # An optional id renders as anyOf(string-with-pattern, null) (U6).
            patterns = [schema.get("pattern"), *[o.get("pattern") for o in schema.get("anyOf", [])]]
            assert OPAQUE_ID in patterns, f"{spec.name}.{name}"
            found += 1
    assert found == 3  # hold's slot_id and appointment_id, cancel's appointment_id


def test_the_search_description_says_to_pass_slot_ids_unchanged():
    """The sentence V10 adds. Without it the model has no reason to treat an opaque
    token as opaque."""
    spec = next(s for s in default_registry().specs() if s.name == "search_available_slots")

    assert "Each time has a slot_id" in spec.description
    assert "exactly as given" in spec.description


def test_list_my_appointments_refuses_any_argument():
    """V5. It takes nothing at all, so there is no argument the model could use to
    ask about somebody else."""
    spec = next(s for s in default_registry().specs() if s.name == "list_my_appointments")

    assert spec.parameters == {
        "additionalProperties": False,
        "properties": {},
        "type": "object",
    }


# --- list ---------------------------------------------------------------------


async def test_list_my_appointments_shows_this_patients_upcoming_ones_with_ids():
    bookings = service()
    slots = await karim_slots(bookings)
    mine = patient_context(bookings)
    await booked(bookings, mine, slots[0].slot_id)

    result = await run(ListMyAppointments(), {}, context(bookings, patient_context(bookings)))

    assert len(result["appointments"]) == 1
    shown = result["appointments"][0]
    assert shown["appointment_id"] == "apt_1"
    assert shown["doctor_name"] == "Dr. Karim Haddad"
    assert shown["day"] == "Wednesday"
    assert shown["start"] == "2026-09-30T14:00"
    assert shown["status"] == "confirmed"
    assert result["more_available"] is False
    # The reference is the patient's own code and appears only in a receipt our
    # code writes: a model that could see one could write one into a false claim.
    assert "reference" not in json.dumps(result)


async def test_list_my_appointments_shows_nothing_for_another_patient():
    bookings = service()
    slots = await karim_slots(bookings)
    await booked(bookings, patient_context(bookings), slots[0].slot_id)

    result = await run(
        ListMyAppointments(), {}, context(bookings, patient_context(bookings, phone=OTHER_PHONE))
    )

    assert result["appointments"] == []


async def test_a_booking_tool_without_a_patient_side_crashes_the_turn():
    """A wiring bug, not something the model can fix: there is no identity to act
    for. PERMANENT with a dead letter, never a guess (hard rule 4)."""
    bookings = service()

    with pytest.raises(ToolCrashed) as raised:
        await run(ListMyAppointments(), {}, context(bookings, None))

    assert raised.value.error_class == "PatientContextMissing"


# --- hold ---------------------------------------------------------------------


async def test_a_hold_says_it_is_not_booked_and_hides_the_hold_id():
    bookings = service()
    slots = await karim_slots(bookings)
    patient = patient_context(bookings)

    result = await run(
        HoldAppointmentSlot(), {"slot_id": slots[0].slot_id}, context(bookings, patient)
    )

    assert result["status"] == "held"
    assert result["booked"] is False
    assert result["doctor_name"] == "Dr. Karim Haddad"
    assert result["start"] == "2026-09-30T14:00"
    assert "not booked yet" in result["next_step"]
    # The hold_id stays ours: the tool that consumes it reads it from
    # booking_actions, so the model can neither invent nor reuse one (C15).
    assert "hold_id" not in json.dumps(result)
    assert patient.outcome is not None
    assert patient.outcome.hold_id == "hold_1"


async def test_a_hold_records_a_prepared_outcome_with_a_key_and_a_receipt():
    bookings = service()
    slots = await karim_slots(bookings)
    patient = patient_context(bookings)

    await run(HoldAppointmentSlot(), {"slot_id": slots[0].slot_id}, context(bookings, patient))

    outcome = patient.outcome
    assert outcome is not None
    assert outcome.kind is BookingActionKind.BOOK
    assert outcome.phase is ChangePhase.PROPOSED
    assert outcome.status is ChangeStatus.SUCCESS
    assert outcome.hold_expires_at == NOW + dt.timedelta(minutes=10)
    assert outcome.receipt == "⏳ Dr. Karim Haddad · 2026-09-30 14:00"
    # The key is recomputed here independently, from the same three inputs.
    assert outcome.idempotency_key == idempotency_key(
        INBOX, "hold_appointment_slot", {"patient_ref": PHONE, "slot_id": slots[0].slot_id}
    )


async def test_a_taken_slot_is_reported_and_nothing_is_held():
    """The slice's own acceptance criterion, at the tool's level: SLOT_TAKEN says
    "nothing was held or booked" and tells the model to offer alternatives."""
    bookings = service()
    slots = await karim_slots(bookings)
    theirs = patient_context(bookings, phone=OTHER_PHONE)
    await run(HoldAppointmentSlot(), {"slot_id": slots[0].slot_id}, context(bookings, theirs))
    mine = patient_context(bookings)

    with pytest.raises(BookingError) as raised:
        await run(HoldAppointmentSlot(), {"slot_id": slots[0].slot_id}, context(bookings, mine))

    assert raised.value.code == "SLOT_TAKEN"
    assert mine.outcome is not None
    assert mine.outcome.status is ChangeStatus.FAILED
    assert mine.outcome.receipt is None


@pytest.mark.parametrize(
    "bad",
    [
        "14:00 tomorrow",
        "slot/../admin",
        "slot id",
        "slot#1",
        "a" * 200,
        # "" is deliberately NOT here: the empty string is a substring of every
        # string, so it cannot be a "never echoed" sentinel. The pattern refuses it
        # anyway, and the length case covers the other end.
    ],
)
async def test_a_malformed_slot_id_is_invalid_and_never_echoed(bad):
    """The pattern refuses it before anything is looked up, and the problem message
    is fixed text - `str(ValidationError)` quotes the input (U6), so it is never
    used."""
    from pydantic import ValidationError

    from app.agent.tools.errors import problems_from

    with pytest.raises(ValidationError) as raised:
        HoldAppointmentSlot().args_model.model_validate({"slot_id": bad}, context={"now": NOW})

    rows = raised.value.errors(include_input=False, include_url=False, include_context=False)
    problems = problems_from(rows, ("appointment_id", "slot_id"))
    assert problems == [
        {
            "argument": "slot_id",
            "problem": "must be a slot_id copied exactly from search_available_slots",
        }
    ]
    assert bad not in json.dumps(problems)


async def test_an_unknown_well_formed_slot_id_is_slot_not_found():
    """V10's third defence: an id this service never issued resolves to nothing,
    and the message tells the model where a real one comes from."""
    from app.agent.tools.errors import BOOKING_MESSAGES

    bookings = service()
    await karim_slots(bookings)
    patient = patient_context(bookings)

    with pytest.raises(BookingError) as raised:
        await run(
            HoldAppointmentSlot(),
            {"slot_id": "slot_neverissuedbythisservice"},
            context(bookings, patient),
        )

    assert raised.value.code == "NOT_FOUND"
    assert "copied exactly" in BOOKING_MESSAGES["slot_not_found"]


async def test_holding_for_a_move_checks_the_appointment_belongs_to_the_patient():
    bookings = service()
    slots = await karim_slots(bookings)
    await booked(bookings, patient_context(bookings), slots[0].slot_id)
    intruder = patient_context(bookings, phone=OTHER_PHONE)

    with pytest.raises(BookingError) as raised:
        await run(
            HoldAppointmentSlot(),
            {"slot_id": slots[1].slot_id, "appointment_id": "apt_1"},
            context(bookings, intruder),
        )

    assert raised.value.code == "NOT_FOUND"
    # The ownership read comes BEFORE the one-change rule, so a wrong id costs the
    # message nothing.
    assert intruder.changes == 0


async def test_holding_for_a_move_records_a_reschedule_with_both_times():
    bookings = service()
    slots = await karim_slots(bookings)
    patient = patient_context(bookings)
    await booked(bookings, patient, slots[0].slot_id)
    mover = patient_context(bookings)

    result = await run(
        HoldAppointmentSlot(),
        {"slot_id": slots[2].slot_id, "appointment_id": "apt_1"},
        context(bookings, mover),
    )

    assert result["status"] == "held_for_change"
    assert result["changed"] is False
    assert result["moving"]["start"] == "2026-09-30T14:00"
    assert result["to"]["start"] == "2026-09-30T15:40"
    assert "Nothing has changed yet" in result["next_step"]
    assert mover.outcome is not None
    assert mover.outcome.kind is BookingActionKind.RESCHEDULE
    assert mover.outcome.receipt == (
        "⏳ Dr. Karim Haddad · 2026-09-30 14:00 → Dr. Karim Haddad · 2026-09-30 15:40"
    )


# --- book ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (None, "nothing_to_confirm"),
        (pending(BookingActionKind.CANCEL), "nothing_to_confirm"),
        (
            pending(BookingActionKind.BOOK, status=BookingActionStatus.DONE),
            "nothing_to_confirm",
        ),
        (
            pending(BookingActionKind.BOOK, status=BookingActionStatus.EXPIRED),
            "hold_expired",
        ),
        (pending(BookingActionKind.BOOK, confirmable=False), "confirmation_needed"),
    ],
    ids=["no row", "a cancel row", "already done", "expired", "not confirmable"],
)
async def test_book_is_refused_when_the_gate_says_so(state, code):
    """V3's gate, one reason at a time, in the order plan section 5.7 fixes.

    Every one is REFUSED, so our code declining is distinguishable from the model
    erring and from the service failing - and none of them costs the message its one
    change or touches the Booking Service.
    """
    bookings = service()
    patient = patient_context(bookings, state=state)

    with pytest.raises(ToolFailure) as raised:
        await run(BookAppointment(), {"full_name": "Rami Khoury"}, context(bookings, patient))

    assert raised.value.code == code
    assert patient.changes == 0
    assert patient.outcome is None


async def test_book_is_refused_in_the_message_that_prepared_the_hold():
    """The gate's condition that does the real work: a patient cannot confirm
    something they were never told.

    Here the hold was made in THIS turn, so T1 never saw it and the in-turn copy is
    not confirmable.
    """
    bookings = service()
    slots = await karim_slots(bookings)
    patient = patient_context(bookings)
    await run(HoldAppointmentSlot(), {"slot_id": slots[0].slot_id}, context(bookings, patient))

    with pytest.raises(ToolFailure) as raised:
        await run(BookAppointment(), {"full_name": "Rami Khoury"}, context(bookings, patient))

    assert raised.value.code == "confirmation_needed"
    assert patient.state is not None
    assert patient.state.action_id is None  # a change made earlier in THIS turn (P7)


async def test_book_is_refused_when_the_hold_expired():
    bookings = service()
    patient = patient_context(
        bookings,
        state=pending(BookingActionKind.BOOK, status=BookingActionStatus.EXPIRED),
    )

    with pytest.raises(ToolFailure) as raised:
        await run(BookAppointment(), {"full_name": "Rami Khoury"}, context(bookings, patient))

    assert raised.value.code == "hold_expired"


async def test_book_executes_a_confirmable_hold_with_a_receipt():
    bookings = service()
    slots = await karim_slots(bookings)
    first = patient_context(bookings)
    done = await booked(bookings, first, slots[0].slot_id)

    result, patient = done["result"], done["patient"]
    assert result["status"] == "booked"
    assert result["appointment_id"] == "apt_1"
    assert result["start"] == "2026-09-30T14:00"
    assert "added to your reply automatically" in result["next_step"]
    outcome = patient.outcome
    assert outcome is not None
    assert outcome.phase is ChangePhase.EXECUTED
    assert outcome.status is ChangeStatus.SUCCESS
    assert outcome.confirmed is True
    assert outcome.receipt is not None
    assert outcome.receipt.startswith("✅ Dr. Karim Haddad · 2026-09-30 14:00 · #")
    assert outcome.action_id == patient.state.action_id or outcome.action_id is not None


async def test_book_never_records_or_returns_the_name():
    """V5 and hard rule 8. The name is the patient's, it goes to the Booking
    Service, and it exists nowhere else of ours: `tool_executions` records the
    argument NAME only."""
    bookings = service()
    slots = await karim_slots(bookings)
    patient = patient_context(bookings)
    await run(HoldAppointmentSlot(), {"slot_id": slots[0].slot_id}, context(bookings, patient))
    second = patient_context(
        bookings, state=pending(BookingActionKind.BOOK, hold_id=patient.outcome.hold_id)
    )

    content, record = await default_registry().execute(
        _request("book_appointment", {"full_name": "SENTINELNAME"}),
        context(bookings, second),
        sequence=0,
        model_call=1,
    )

    assert record.status.value == "OK"
    assert record.argument_names == ("full_name",)
    assert "SENTINELNAME" not in content
    assert "SENTINELNAME" not in repr(record)
    assert "SENTINELNAME" not in repr(second.outcome)
    assert "SENTINELNAME" not in repr(second)


def _request(name: str, arguments: dict[str, Any]):
    from app.integrations.openai import ToolCallRequest

    return ToolCallRequest(f"call_{name}", name, json.dumps(arguments))


@pytest.mark.parametrize(
    ("name", "problem"),
    [
        ("R", "must be the patient's full name"),
        ("  R  ", "must be the patient's full name"),
        ("Rami 2", "must be a person's name, with no digits"),
        ("Rami​Khoury", "must be a person's name, with no hidden characters"),
    ],
    ids=["too short", "too short after trimming", "digits", "hidden characters"],
)
async def test_a_bad_full_name_is_refused_without_being_echoed(name, problem):
    from pydantic import ValidationError

    from app.agent.tools.errors import problems_from

    with pytest.raises(ValidationError) as raised:
        BookAppointment().args_model.model_validate({"full_name": name}, context={"now": NOW})

    rows = raised.value.errors(include_input=False, include_url=False, include_context=False)
    problems = problems_from(rows, ("full_name",))
    assert problems == [{"argument": "full_name", "problem": problem}]
    assert name.strip() not in json.dumps(problems)


def test_a_name_is_normalised_before_it_is_sent():
    """Whitespace collapsed and NFC applied, so a re-run that spells the same name
    differently produces the SAME idempotency key (V1)."""
    import unicodedata

    args = BookAppointment().args_model.model_validate(
        {"full_name": " Rami \n Khoury "}, context={"now": NOW}
    )
    assert args.full_name == "Rami Khoury"

    composed = BookAppointment().args_model.model_validate(
        {"full_name": unicodedata.normalize("NFC", "Zoé Haddad")}, context={"now": NOW}
    )
    decomposed = BookAppointment().args_model.model_validate(
        {"full_name": unicodedata.normalize("NFD", "Zoé Haddad")}, context={"now": NOW}
    )
    assert composed.full_name == decomposed.full_name


async def test_a_pending_approval_booking_is_requested_not_booked():
    """Contract open question 3. Nothing in the result says "booked", and the
    receipt is ⏳ rather than ✅ (hard rule 5)."""
    bookings = service()
    slots = await karim_slots(bookings)
    patient = patient_context(bookings)
    await run(HoldAppointmentSlot(), {"slot_id": slots[0].slot_id}, context(bookings, patient))
    second = patient_context(
        bookings, state=pending(BookingActionKind.BOOK, hold_id=patient.outcome.hold_id)
    )

    class NeedsApproval:
        """A stand-in whose bookings need the clinic's approval."""

        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def create_appointment(self, *args, **kwargs):
            appointment = await self.inner.create_appointment(*args, **kwargs)
            return appointment.model_copy(update={"status": "PENDING_APPROVAL"})

    second.bookings = NeedsApproval(bookings)
    result = await run(BookAppointment(), {"full_name": "Rami Khoury"}, context(bookings, second))

    assert result["status"] == "requested"
    assert result["booked"] is False
    assert "Never say it is booked" in result["next_step"]
    assert second.outcome.confirmed is False
    assert second.outcome.receipt.startswith("⏳")


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        ("UNKNOWN_BEFORE", "booking_unknown_outcome"),
        ("UNKNOWN_AFTER", "booking_unknown_outcome"),
        ("IDEMPOTENCY_CONFLICT", "booking_idempotency_conflict"),
    ],
)
async def test_an_unknown_outcome_is_uncertain_with_the_fixed_message(failure, error_code):
    """V6. The outcome is UNCERTAIN, not FAILED, and it keeps its key - the only
    thing that can find the request at the Booking Service afterwards."""
    from app.agent.tools.errors import BOOKING_MESSAGES

    failures = FailureScript()
    bookings = service(failures)
    slots = await karim_slots(bookings)
    patient = patient_context(bookings)
    await run(HoldAppointmentSlot(), {"slot_id": slots[0].slot_id}, context(bookings, patient))
    second = patient_context(
        bookings, state=pending(BookingActionKind.BOOK, hold_id=patient.outcome.hold_id)
    )
    failures.push("create_appointment", failure)

    content, record = await default_registry().execute(
        _request("book_appointment", {"full_name": "Rami Khoury"}),
        context(bookings, second),
        sequence=0,
        model_call=1,
    )

    assert record.status.value == "UNCERTAIN"
    assert record.error_code == error_code
    assert second.outcome.status is ChangeStatus.UNCERTAIN
    assert second.outcome.idempotency_key is not None
    assert second.outcome.receipt is None
    assert BOOKING_MESSAGES["outcome_unknown"] in content
    assert "never say that it worked and never say that it failed" in content.lower()


# --- reschedule ---------------------------------------------------------------


async def test_reschedule_moves_the_appointment_after_confirmation():
    bookings = service()
    slots = await karim_slots(bookings)
    patient = patient_context(bookings)
    await booked(bookings, patient, slots[0].slot_id)

    mover = patient_context(bookings)
    await run(
        HoldAppointmentSlot(),
        {"slot_id": slots[2].slot_id, "appointment_id": "apt_1"},
        context(bookings, mover),
    )
    third = patient_context(
        bookings,
        state=pending(
            BookingActionKind.RESCHEDULE,
            hold_id=mover.outcome.hold_id,
            appointment_id="apt_1",
        ),
    )

    result = await run(RescheduleAppointment(), {}, context(bookings, third))

    assert result["status"] == "moved"
    assert result["appointment_id"] == "apt_1"
    assert result["start"] == "2026-09-30T15:40"
    assert third.outcome.kind is BookingActionKind.RESCHEDULE
    assert third.outcome.phase is ChangePhase.EXECUTED
    assert third.outcome.receipt.startswith("🔁 Dr. Karim Haddad · 2026-09-30 15:40 · #")


async def test_reschedule_is_refused_without_a_prepared_move():
    bookings = service()
    patient = patient_context(bookings, state=pending(BookingActionKind.BOOK))

    with pytest.raises(ToolFailure) as raised:
        await run(RescheduleAppointment(), {}, context(bookings, patient))

    assert raised.value.code == "nothing_to_confirm"


# --- cancel -------------------------------------------------------------------


async def test_the_first_cancel_prepares_and_cancels_nothing():
    bookings = service()
    slots = await karim_slots(bookings)
    await booked(bookings, patient_context(bookings), slots[0].slot_id)
    patient = patient_context(bookings)

    result = await run(CancelAppointment(), {"appointment_id": "apt_1"}, context(bookings, patient))

    assert result["status"] == "cancellation_prepared"
    assert result["cancelled"] is False
    assert result["appointment"]["appointment_id"] == "apt_1"
    assert "Nothing is cancelled yet" in result["next_step"]
    # The appointment is still there: the first call really changed nothing.
    still = await run(ListMyAppointments(), {}, context(bookings, patient_context(bookings)))
    assert len(still["appointments"]) == 1
    outcome = patient.outcome
    assert outcome is not None
    assert outcome.phase is ChangePhase.PROPOSED
    assert outcome.kind is BookingActionKind.CANCEL
    # No key: nothing was sent to the Booking Service.
    assert outcome.idempotency_key is None
    assert outcome.receipt == "⏳ ❌ Dr. Karim Haddad · 2026-09-30 14:00"


async def test_a_cancel_in_a_later_message_cancels():
    bookings = service()
    slots = await karim_slots(bookings)
    await booked(bookings, patient_context(bookings), slots[0].slot_id)
    second = patient_context(
        bookings,
        state=pending(BookingActionKind.CANCEL, hold_id=None, appointment_id="apt_1"),
    )

    result = await run(CancelAppointment(), {"appointment_id": "apt_1"}, context(bookings, second))

    assert result["status"] == "cancelled"
    assert "added to your reply automatically" in result["next_step"]
    assert second.outcome.phase is ChangePhase.EXECUTED
    assert second.outcome.receipt.startswith("❌ Dr. Karim Haddad · 2026-09-30 14:00 · #")
    left = await run(ListMyAppointments(), {}, context(bookings, patient_context(bookings)))
    assert left["appointments"] == []


async def test_a_prepared_cancel_in_the_same_message_is_refused():
    bookings = service()
    slots = await karim_slots(bookings)
    await booked(bookings, patient_context(bookings), slots[0].slot_id)
    second = patient_context(
        bookings,
        state=pending(
            BookingActionKind.CANCEL,
            confirmable=False,
            hold_id=None,
            appointment_id="apt_1",
        ),
    )

    with pytest.raises(ToolFailure) as raised:
        await run(CancelAppointment(), {"appointment_id": "apt_1"}, context(bookings, second))

    assert raised.value.code == "confirmation_needed"


async def test_a_cancel_for_another_appointment_prepares_that_one_instead():
    """A prepared cancellation is for ONE appointment. Asking about a different one
    is a new question, not a confirmation of the old one."""
    bookings = service()
    slots = await karim_slots(bookings)
    first = patient_context(bookings)
    await booked(bookings, first, slots[0].slot_id)
    mover = patient_context(bookings)
    await run(HoldAppointmentSlot(), {"slot_id": slots[1].slot_id}, context(bookings, mover))
    second = patient_context(
        bookings, state=pending(BookingActionKind.BOOK, hold_id=mover.outcome.hold_id)
    )
    await run(BookAppointment(), {"full_name": "Rami Khoury"}, context(bookings, second))

    # A PENDING cancel for apt_1, but the model asks about apt_2.
    patient = patient_context(
        bookings,
        state=pending(BookingActionKind.CANCEL, hold_id=None, appointment_id="apt_1"),
    )
    result = await run(CancelAppointment(), {"appointment_id": "apt_2"}, context(bookings, patient))

    assert result["status"] == "cancellation_prepared"
    assert result["appointment"]["appointment_id"] == "apt_2"
    assert patient.outcome.appointment_id == "apt_2"


async def test_cancelling_another_patients_appointment_is_not_found():
    bookings = service()
    slots = await karim_slots(bookings)
    await booked(bookings, patient_context(bookings), slots[0].slot_id)
    intruder = patient_context(bookings, phone=OTHER_PHONE)

    with pytest.raises(BookingError) as raised:
        await run(CancelAppointment(), {"appointment_id": "apt_1"}, context(bookings, intruder))

    assert raised.value.code == "NOT_FOUND"
    assert intruder.changes == 0


# --- keys and leaks -----------------------------------------------------------


async def test_every_changing_call_sends_the_key_derived_from_the_inbox_row():
    """Hard rule 6, end to end over all four writes.

    Each key is recomputed from the three inputs independently, so this fails if a
    tool ever builds a request body that differs from the one it hashed.
    """
    inner = service()
    spy = RecordingBooking(inner)
    slots = await karim_slots(inner)

    holder = patient_context(spy)
    await run(HoldAppointmentSlot(), {"slot_id": slots[0].slot_id}, context(spy, holder))
    booker = patient_context(
        spy, state=pending(BookingActionKind.BOOK, hold_id=holder.outcome.hold_id)
    )
    await run(BookAppointment(), {"full_name": "Rami Khoury"}, context(spy, booker))
    mover = patient_context(spy)
    await run(
        HoldAppointmentSlot(),
        {"slot_id": slots[2].slot_id, "appointment_id": "apt_1"},
        context(spy, mover),
    )
    rescheduler = patient_context(
        spy,
        state=pending(
            BookingActionKind.RESCHEDULE, hold_id=mover.outcome.hold_id, appointment_id="apt_1"
        ),
    )
    await run(RescheduleAppointment(), {}, context(spy, rescheduler))
    canceller = patient_context(
        spy, state=pending(BookingActionKind.CANCEL, hold_id=None, appointment_id="apt_1")
    )
    await run(CancelAppointment(), {"appointment_id": "apt_1"}, context(spy, canceller))

    expected = [
        idempotency_key(
            INBOX, "hold_appointment_slot", {"patient_ref": PHONE, "slot_id": slots[0].slot_id}
        ),
        idempotency_key(
            INBOX,
            "book_appointment",
            {"full_name": "Rami Khoury", "hold_id": "hold_1", "patient_ref": PHONE},
        ),
        idempotency_key(
            INBOX, "hold_appointment_slot", {"patient_ref": PHONE, "slot_id": slots[2].slot_id}
        ),
        idempotency_key(
            INBOX,
            "reschedule_appointment",
            {"appointment_id": "apt_1", "new_hold_id": "hold_2", "patient_ref": PHONE},
        ),
        idempotency_key(
            INBOX, "cancel_appointment", {"appointment_id": "apt_1", "patient_ref": PHONE}
        ),
    ]
    sent = [key for key in spy.keys if key is not None]
    assert sent == expected
    assert all(len(key) == 64 for key in sent)
    assert len(set(sent)) == 5  # four distinct intents, five distinct keys


@pytest.mark.parametrize(
    "tool_name",
    [
        "get_clinic_information",
        "list_doctors",
        "search_available_slots",
        "list_my_appointments",
        "hold_appointment_slot",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    ],
)
async def test_no_result_contains_the_tenant_the_patient_ref_a_hold_id_a_reference_or_a_key(
    tool_name,
):
    """Hard rules 4 and 8, over every tool's real output.

    The five things that must never reach the model, each for its own reason: the
    tenant (a patient could talk the model into another clinic), the patient
    reference (personal data, and ours to choose), a `hold_id` (the model could
    then confirm a hold nobody described), a reference code (it could write a false
    receipt), and an idempotency key (it could replay a change).
    """
    bookings = service()
    slots = await karim_slots(bookings)
    first = patient_context(bookings)
    done = await booked(bookings, first, slots[0].slot_id)
    reference = None
    for appointment in await bookings.list_appointments(TENANT, PatientRef(PHONE)):
        reference = appointment.reference
    assert reference is not None

    arguments: dict[str, Any] = {}
    patient = patient_context(bookings)
    if tool_name == "search_available_slots":
        arguments = {
            "doctor_id": "doc_karim",
            "start": "2026-09-30T12:00",
            "end": "2026-09-30T17:00",
        }
    elif tool_name == "hold_appointment_slot":
        arguments = {"slot_id": slots[1].slot_id}
    elif tool_name == "book_appointment":
        arguments = {"full_name": "Rami Khoury"}
        patient = patient_context(bookings, state=pending(BookingActionKind.BOOK, hold_id="hold_9"))
    elif tool_name == "reschedule_appointment":
        patient = patient_context(
            bookings,
            state=pending(BookingActionKind.RESCHEDULE, hold_id="hold_9", appointment_id="apt_1"),
        )
    elif tool_name == "cancel_appointment":
        arguments = {"appointment_id": "apt_1"}

    content, _ = await default_registry().execute(
        _request(tool_name, arguments), context(bookings, patient), sequence=0, model_call=1
    )

    for sentinel in (
        TENANT,
        PHONE,
        "hold_1",
        "hold_9",
        reference,
        done["patient"].outcome.idempotency_key,
    ):
        assert sentinel not in content, f"{tool_name}: {sentinel}"
