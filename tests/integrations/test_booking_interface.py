"""The write side of the Booking Service interface (VS-007, plan section 5.1).

No database and no network. Everything here is about the *shape* of the contract:
which error codes exist, what a write must be given, and what a patient reference
is allowed to show. The implementation of the write side arrives in Task A3; what
this file pins is the interface both it and VS-011's HTTP client must satisfy.

Two guarantees are load-bearing:

- `idempotency_key` is KEYWORD-ONLY on every write, so a write without one does
  not type-check and does not run (hard rule 6);
- `PatientRef` never shows its value, anywhere, so the patient reference cannot
  reach a log line, a traceback or an error tracker (hard rule 8, plan V5).
"""

import inspect
import traceback
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.integrations.booking import (
    Appointment,
    BookingClient,
    BookingError,
    Hold,
    PatientBookingClient,
    PatientRef,
)
from app.integrations.booking.fake import FakeBookingClient
from tests.integrations.booking_fakes import RecordingBooking

# Tuesday 29 September 2026, 10:00 Beirut, like every other clock in the suite.
NOW = datetime(2026, 9, 29, 7, tzinfo=UTC)
TENANT = "clinic-alpha"
PATIENT = PatientRef("00000000-0000-4000-8000-000000000001")

HOLD = Hold(
    hold_id="hold_1",
    slot_id="slot_1",
    doctor_id="doc_karim",
    doctor_name="Dr. Karim Haddad",
    start=NOW + timedelta(days=1),
    end=NOW + timedelta(days=1, minutes=20),
    expires_at=NOW + timedelta(minutes=10),
)
APPOINTMENT = Appointment(
    appointment_id="apt_1",
    reference="K7Q2M9",
    doctor_id="doc_karim",
    doctor_name="Dr. Karim Haddad",
    start=NOW + timedelta(days=1),
    end=NOW + timedelta(days=1, minutes=20),
    status="CONFIRMED",
)


class StubPatientBooking:
    """A minimal `PatientBookingClient`, so the spy can be tested in Task A1.

    The real implementation is `InMemoryBookingService` (Task A3). This stub
    exists because the spy's own wiring - does it record every call, does the
    after-hook run at the right moment - is a Task A1 question.
    """

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def list_appointments(
        self, tenant_id: str, patient: PatientRef
    ) -> tuple[Appointment, ...]:
        return (APPOINTMENT,)

    async def create_hold(
        self, tenant_id: str, patient: PatientRef, slot_id: str, *, idempotency_key: str
    ) -> Hold:
        self.keys.append(idempotency_key)
        return HOLD

    async def create_appointment(
        self,
        tenant_id: str,
        patient: PatientRef,
        hold_id: str,
        full_name: str,
        *,
        idempotency_key: str,
    ) -> Appointment:
        self.keys.append(idempotency_key)
        return APPOINTMENT

    async def reschedule_appointment(
        self,
        tenant_id: str,
        patient: PatientRef,
        appointment_id: str,
        new_hold_id: str,
        *,
        idempotency_key: str,
    ) -> Appointment:
        self.keys.append(idempotency_key)
        return APPOINTMENT

    async def cancel_appointment(
        self, tenant_id: str, patient: PatientRef, appointment_id: str, *, idempotency_key: str
    ) -> Appointment:
        self.keys.append(idempotency_key)
        return APPOINTMENT


@pytest.mark.parametrize(
    "code", ["SLOT_TAKEN", "HOLD_EXPIRED", "IDEMPOTENCY_CONFLICT", "UNKNOWN_OUTCOME"]
)
def test_the_four_new_codes_are_accepted_and_carry_only_their_code(code):
    """The write side's codes, carrying nothing the contract's `message` said.

    `UNKNOWN_OUTCOME` is the important one: it is never sent by the service. Our
    own client raises it for a write whose answer was lost, and everything
    downstream - the tool text, `tool_executions.status`, the dead letter - keys
    off this code rather than off a guess (plan V6).
    """
    error = BookingError(code)

    assert str(error) == code
    assert repr(error) == f"BookingError({code!r})"
    assert error.args == (code,)
    assert error.code == code


def test_an_unknown_code_is_still_refused_without_echoing_it():
    """A code we do not know is a bug, and its text is not evidence worth
    keeping: it could have come from a service body, and this message reaches a
    traceback."""
    with pytest.raises(ValueError) as raised:
        BookingError("SENTINEL-not-a-code")

    assert "SENTINEL" not in str(raised.value)
    assert "SENTINEL" not in repr(raised.value)


def test_the_vs006_fake_is_a_booking_client_and_not_a_patient_booking_client():
    """Plan conflict C2: the write side could not go on the VS-006 fake.

    `FakeBookingClient` is frozen, stateless and read-only, and three tests in
    `test_fake_booking.py` pin that. So the patient side is a SECOND Protocol,
    and the fake deliberately fails it.
    """
    fake = FakeBookingClient.demo(clock=lambda: NOW)

    assert isinstance(fake, BookingClient)
    assert not isinstance(fake, PatientBookingClient)


def test_every_write_takes_the_idempotency_key_as_keyword_only():
    """Hard rule 6, enforced by the signature rather than by a review.

    A positional key could be passed by accident in the wrong position - or
    omitted while the call still ran. Keyword-only means a write with no key is a
    TypeError at the call site.
    """
    writes = (
        PatientBookingClient.create_hold,
        PatientBookingClient.create_appointment,
        PatientBookingClient.reschedule_appointment,
        PatientBookingClient.cancel_appointment,
    )
    for write in writes:
        parameter = inspect.signature(write).parameters["idempotency_key"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, write.__name__
        assert parameter.default is inspect.Parameter.empty, write.__name__

    read = inspect.signature(PatientBookingClient.list_appointments).parameters
    assert "idempotency_key" not in read
    # V5: the patient is injected by our code, and the read takes no other
    # argument at all - nothing for the model to name a patient with.
    assert list(read) == ["self", "tenant_id", "patient"]


def test_a_patient_ref_never_shows_its_value():
    """V5 and hard rule 8. The value is our contact UUID (V14); VS-011 may make
    it something more sensitive, and no call site should have to be revisited."""
    ref = PatientRef("SENTINEL-contact-uuid")

    assert repr(ref) == "PatientRef(<hidden>)"
    assert str(ref) == "PatientRef(<hidden>)"
    assert f"{ref}" == "PatientRef(<hidden>)"
    assert f"{ref!r}" == "PatientRef(<hidden>)"

    # The realistic leak: a reference caught in an exception that a logger or an
    # error tracker then formats.
    try:
        raise RuntimeError(ref)
    except RuntimeError as error:
        formatted = "".join(traceback.format_exception(error))
    assert "SENTINEL" not in formatted

    # The value is still reachable on purpose: the client has to send it.
    assert ref.value == "SENTINEL-contact-uuid"
    assert ref == PatientRef("SENTINEL-contact-uuid")
    assert ref != PatientRef("other")
    assert len({ref, PatientRef("SENTINEL-contact-uuid")}) == 1


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        " SENTINEL",
        "SENTINEL ",
        "SENTINEL\nvalue",
        "SENTINEL\rvalue",
        "zero​width-SENTINEL",
        42,
        None,
    ],
)
def test_a_patient_ref_refuses_an_empty_or_unprintable_value_without_echoing_it(value):
    """The same hygiene the tenant id gets (VS-006 Q2), for the same reason: this
    value goes into an HTTP request, and a CR/LF in it is header injection.

    The refusal never names the value, because a refusal is exactly where a bad
    value would otherwise be written down.
    """
    with pytest.raises(ValueError) as raised:
        PatientRef(value)

    assert "SENTINEL" not in str(raised.value)
    assert "SENTINEL" not in repr(raised.value)


@pytest.mark.parametrize("field", ["start", "end", "expires_at"])
def test_hold_and_appointment_times_must_be_aware(field):
    """A naive datetime would be an hour wrong twice a year and silently right
    the rest of the time. `AwareDatetime` refuses it at the boundary."""
    values = {
        "hold_id": "hold_1",
        "slot_id": "slot_1",
        "doctor_id": "doc_karim",
        "doctor_name": "Dr. Karim Haddad",
        "start": NOW,
        "end": NOW,
        "expires_at": NOW,
    }
    values[field] = NOW.replace(tzinfo=None)

    with pytest.raises(ValidationError):
        Hold(**values)


def test_an_appointment_time_must_be_aware_and_its_status_is_a_fixed_set():
    """`PENDING_APPROVAL` exists for contract open question 3. The fake never
    returns it, and no tool ever calls it "booked" (plan section 5.7)."""
    base = APPOINTMENT.model_dump()

    with pytest.raises(ValidationError):
        Appointment.model_validate({**base, "start": NOW.replace(tzinfo=None)})
    with pytest.raises(ValidationError):
        Appointment.model_validate({**base, "status": "BOOKED"})

    for status in ("CONFIRMED", "PENDING_APPROVAL", "CANCELLED"):
        assert Appointment.model_validate({**base, "status": status}).status == status


@pytest.mark.parametrize("dto", [HOLD, APPOINTMENT])
def test_the_new_dtos_are_frozen_and_ignore_unknown_fields(dto):
    """Frozen because one client instance is shared by every concurrent job;
    `extra="ignore"` because VS-011 parses these from a service that may add
    fields, and a new field must not break a reply."""
    with pytest.raises(ValidationError):
        dto.doctor_id = "doc_other"

    widened = type(dto).model_validate({**dto.model_dump(), "invented_by_the_service": "x"})
    assert widened == dto
    assert not hasattr(widened, "invented_by_the_service")


async def test_the_spy_wraps_the_patient_side_and_records_calls():
    """Every later booking test asserts through this.

    The patient is recorded as the `PatientRef` OBJECT, not its value, so even a
    failed assertion printing the whole call list shows nothing.
    """
    spy = RecordingBooking(StubPatientBooking())

    await spy.list_appointments(TENANT, PATIENT)
    await spy.create_hold(TENANT, PATIENT, "slot_1", idempotency_key="k1")
    await spy.create_appointment(TENANT, PATIENT, "hold_1", "Rami Khoury", idempotency_key="k2")
    await spy.reschedule_appointment(TENANT, PATIENT, "apt_1", "hold_2", idempotency_key="k3")
    await spy.cancel_appointment(TENANT, PATIENT, "apt_1", idempotency_key="k4")

    assert spy.methods == [
        "list_appointments",
        "create_hold",
        "create_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    ]
    assert spy.tenants == [TENANT] * 5
    assert [call.kwargs["patient"] for call in spy.calls] == [PATIENT] * 5
    assert [call.kwargs.get("idempotency_key") for call in spy.calls] == [
        None,
        "k1",
        "k2",
        "k3",
        "k4",
    ]
    assert "SENTINEL" not in repr(spy.calls)


async def test_the_spy_records_the_patient_reference_without_revealing_it():
    ref = PatientRef("SENTINEL-contact-uuid")
    spy = RecordingBooking(StubPatientBooking())

    await spy.list_appointments(TENANT, ref)

    assert "SENTINEL" not in repr(spy.calls)
    assert spy.calls[0].kwargs["patient"] is ref


async def test_the_spy_after_hook_runs_after_the_wrapped_call():
    """How a takeover is made to happen AFTER a booking succeeded.

    `hook` runs before the wrapped call, which proves hard rule 7 across a call
    in flight. `after_hook` runs once the service has already answered, which is
    the harder case: the change exists, and the reply must still be dropped
    (plan section 5.12).
    """
    order: list[str] = []
    inner = StubPatientBooking()

    async def before() -> None:
        order.append("before")

    async def after() -> None:
        order.append("after")

    spy = RecordingBooking(inner, hook=before, after_hook=after)

    appointment = await spy.create_appointment(
        TENANT, PATIENT, "hold_1", "Rami Khoury", idempotency_key="k9"
    )

    assert appointment == APPOINTMENT
    assert order == ["before", "after"]
    # The inner client really ran, between the two hooks.
    assert inner.keys == ["k9"]


async def test_the_spy_after_hook_does_not_run_when_the_call_was_made_to_fail():
    """`raises` short-circuits before the wrapped call, so there is no "after"."""
    order: list[str] = []

    async def after() -> None:
        order.append("after")

    spy = RecordingBooking(
        StubPatientBooking(), after_hook=after, raises=BookingError("SLOT_TAKEN")
    )

    with pytest.raises(BookingError):
        await spy.create_hold(TENANT, PATIENT, "slot_1", idempotency_key="k1")

    assert order == []
    assert spy.methods == ["create_hold"]
