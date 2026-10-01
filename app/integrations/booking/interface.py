"""The Booking Service, behind an interface (`docs/booking-contract.md`).

Like `ChatClient` and `TenantResolver`: the agent's tools depend on this
Protocol, and the HTTP client sits behind it in one module (VS-011). Until the
Booking Service exists, `FakeBookingClient` implements the same interface with
seeded in-memory data, so this slice is not blocked on it.

This module imports no HTTP stack and no SDK. That is what lets `app/agent/`
import it (hard rule 3: the model can only reach a tool, and a tool can only
reach this).

**The shapes below are a PROPOSAL.** `docs/booking-contract.md` lists endpoints
but no response bodies for `/clinic`, `/doctors` or `/slots`, so the DTOs here
are this repo's suggestion, to be agreed with the Booking Service owner (plan
conflict C4a, recorded in the contract under "Proposal").
"""

from datetime import datetime, time
from typing import Literal, Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.tenants.ids import TenantId

# Frozen, and extra="ignore" on purpose. Frozen because one client instance is
# shared by every concurrently running job (arq runs several in one process), so
# anything reachable from it must be immutable. extra="ignore" because VS-011
# parses these straight from the Booking Service's JSON, and a service that adds
# a field must not break this one.
_DTO = ConfigDict(frozen=True, extra="ignore")


class Location(BaseModel):
    model_config = _DTO

    name: str
    address: str


class OpeningHours(BaseModel):
    """One weekday's hours. `opens is None` means the clinic is closed."""

    model_config = _DTO

    weekday: int  # 0 = Monday, matching datetime.date.weekday()
    opens: time | None = None
    closes: time | None = None

    @property
    def closed(self) -> bool:
        return self.opens is None or self.closes is None


class Service(BaseModel):
    model_config = _DTO

    service_id: str
    name: str
    duration_minutes: int


class Doctor(BaseModel):
    model_config = _DTO

    doctor_id: str
    name: str
    specialty: str
    services: tuple[Service, ...] = ()


class ClinicInfo(BaseModel):
    model_config = _DTO

    name: str
    # An IANA name, from the clinic. `docs/booking-contract.md` open question 5
    # is unanswered, so carrying it explicitly is cheaper than assuming
    # Asia/Beirut forever - see Follow-up 3.
    timezone: str
    locations: tuple[Location, ...] = ()
    opening_hours: tuple[OpeningHours, ...] = ()
    policies: tuple[str, ...] = ()
    # Q7: the demo clinic carries none, and the prompt says facts come only from
    # tool results, so in practice the model states no price. The field exists
    # because the contract's /clinic mentions pricing info.
    pricing: tuple[str, ...] = ()


class Slot(BaseModel):
    model_config = _DTO

    slot_id: str
    doctor_id: str
    # Aware, always. A naive datetime here would be an hour wrong twice a year
    # and silently right the rest of the time, which is the worst failure mode
    # available. On the wire this is ISO 8601 with an offset.
    start: AwareDatetime
    end: AwareDatetime
    service_id: str | None = None


class Hold(BaseModel):
    """A slot reserved for one patient for a few minutes (VS-007).

    A hold is NOT a booking, and nothing in this repo is allowed to describe it
    as one: the prompt, the tool results, the receipt symbol and the reply guard
    each say so separately (hard rule 5).

    `doctor_name` is here, and on `Appointment`, so that our code can build a
    receipt line from the service's own answer without a second read. That is a
    proposal to the Booking Service owner, recorded in `docs/booking-contract.md`.
    """

    model_config = _DTO

    hold_id: str
    slot_id: str
    doctor_id: str
    doctor_name: str
    start: AwareDatetime
    end: AwareDatetime
    # When the hold lapses, from the SERVICE's clock. An operational deadline,
    # never an appointment time. The fake assumes ten minutes (contract open
    # question 4).
    expires_at: AwareDatetime


# PENDING_APPROVAL exists for contract open question 3 ("does booking need
# doctor or staff approval?"). The fake never returns it, and no tool calls a
# PENDING_APPROVAL appointment "booked": until the clinic confirms, saying so
# would be exactly the false claim hard rule 5 forbids.
AppointmentStatus = Literal["CONFIRMED", "PENDING_APPROVAL", "CANCELLED"]


class Appointment(BaseModel):
    """A booked appointment (VS-007)."""

    model_config = _DTO

    appointment_id: str
    # A short human-readable code for the patient, issued by the service. It is
    # only ever shown inside a receipt line our own code writes; it never reaches
    # the model and never reaches a log line.
    reference: str
    doctor_id: str
    doctor_name: str
    start: AwareDatetime
    end: AwareDatetime
    status: AppointmentStatus


class PatientRef:
    """Who the patient is, for the Booking Service. Built by OUR code, never by
    the model (plan V5 and V14).

    A class rather than a `str` for two reasons. First, VS-011 may have to change
    what it carries - the contract's open question 2 is not settled - and no call
    site should have to be revisited when it does. Second, a `str` would print
    itself in every traceback, log line and error-tracker payload it reached;
    this one prints `PatientRef(<hidden>)` (hard rule 8).

    Today the value is our contact row's UUID (V14): stable per clinic and phone
    number, and not personal data by itself. No phone number is sent.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        # The same hygiene as the tenant id (VS-006 Q2) and for the same reason:
        # this value goes into an HTTP request, so a CR/LF in it is header
        # injection. The message never names the value - a refusal is exactly
        # where a bad value would otherwise be written down.
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or not value.isprintable()
        ):
            raise ValueError(
                "a patient reference must be a non-empty printable string "
                "with no leading or trailing whitespace"
            )
        self.value = value

    def __repr__(self) -> str:
        return "PatientRef(<hidden>)"

    # str() too, so an f-string, a log format and str(exception) are all safe.
    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PatientRef):
            return NotImplemented
        return self.value == other.value

    def __hash__(self) -> int:
        return hash(self.value)


class BookingError(Exception):
    """A Booking Service failure, as a CODE and nothing else.

    `docs/booking-contract.md` gives error bodies as
    `{"error": {"code": ..., "message": ...}}`. The `message` is NEVER read: it
    comes from another system and can quote clinic or patient data, and this
    exception is logged and turned into a tool result (hard rule 8).

    VS-006 had the three read-side codes: 404 -> NOT_FOUND, 422 -> VALIDATION,
    5xx -> UNAVAILABLE. VS-007 adds four, and with them one rule that matters
    more than any of them:

    **A timeout or a lost connection is `UNAVAILABLE` on a READ and
    `UNKNOWN_OUTCOME` on a WRITE.** A read that failed changed nothing, so
    "could not check" is the whole truth. A write that failed may have been
    applied before the answer was lost, so the only honest answer is "we do not
    know" - never "booked" and never "not booked" (plan V6). `UNKNOWN_OUTCOME` is
    never sent by the service: our own client raises it.

    `IDEMPOTENCY_CONFLICT` (a key reused with a different body) is handled the
    same way: an earlier request did something, and we cannot tell what.
    """

    CODES = (
        # VS-006, the read side.
        "NOT_FOUND",
        "VALIDATION",
        "UNAVAILABLE",
        # VS-007, the write side.
        "SLOT_TAKEN",  # 409: someone else holds or booked that slot
        "HOLD_EXPIRED",  # 410: the hold ran out or was released
        "IDEMPOTENCY_CONFLICT",  # 422: this key was used with a different body
        "UNKNOWN_OUTCOME",  # raised by US: the write may or may not have happened
    )

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            # The rejected code is NOT echoed: it can have come from a service
            # body, and this message reaches a traceback.
            raise ValueError("unknown booking error code")
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"BookingError({self.code!r})"


@runtime_checkable
class BookingClient(Protocol):
    """What the agent's tools are allowed to know about the Booking Service.

    Read-only in VS-006. `tenant_id` is ALWAYS our resolved tenant, passed by
    our code from `ToolContext` - never a value the model supplied (hard rule
    4). It is the first parameter of every method precisely so that a call
    without one does not type-check and does not run.

    Every method raises `BookingError` for a service-side failure, never a
    transport exception: "what kind of failure was this" has one home, exactly
    as with `ChatClient`.
    """

    async def get_clinic(self, tenant_id: TenantId) -> ClinicInfo:
        """The clinic's name, timezone, locations, hours and policies."""
        ...

    async def list_doctors(self, tenant_id: TenantId) -> tuple[Doctor, ...]:
        """Every doctor, with the services each offers."""
        ...

    async def search_slots(
        self,
        tenant_id: TenantId,
        doctor_id: str,
        start: datetime,
        end: datetime,
        service_id: str | None = None,
    ) -> tuple[Slot, ...]:
        """Available slots for one doctor in `[start, end)`.

        `start` and `end` must be AWARE. A naive value is `VALIDATION`, not a
        guess about which zone was meant.

        `service_id` is accepted because `GET /slots` takes one (plan conflict
        C4b). No tool exposes it in VS-006 - the tool signature the brief fixes
        has no service - so the fake ignores it. Follow-up 2.
        """
        ...


@runtime_checkable
class PatientBookingClient(Protocol):
    """Everything done on behalf of ONE patient (VS-007).

    A SECOND Protocol rather than more methods on `BookingClient`, because the
    VS-006 fake is frozen, stateless and read-only, and three tests pin it that
    way (plan conflict C2). `InMemoryBookingService` implements both; the fake
    implements only `BookingClient`, and a test pins that too.

    `tenant_id` is our resolved tenant (hard rule 4) and `patient` is always
    built by our code from the contact (plan V5 and V14). Neither is ever a value
    the model supplied, and neither appears in any tool schema.

    Every write takes `idempotency_key` as a KEYWORD-ONLY argument, so a write
    without one does not type-check and does not run (hard rule 6). The key is
    derived from our `webhook_inbox` row, the tool name and the exact request
    body - never from a wamid, which decodes to the patient's phone number.

    Every method raises `BookingError`, never a transport exception. On a write,
    a timeout or a lost connection is `UNKNOWN_OUTCOME`, not `UNAVAILABLE`.
    """

    async def list_appointments(
        self, tenant_id: TenantId, patient: PatientRef
    ) -> tuple[Appointment, ...]:
        """This patient's upcoming appointments, soonest first.

        A read: it takes no idempotency key, and no argument at all beyond the
        two our code injects. There is nothing here for the model to name another
        patient with.
        """
        ...

    async def create_hold(
        self, tenant_id: TenantId, patient: PatientRef, slot_id: str, *, idempotency_key: str
    ) -> Hold:
        """Reserve `slot_id` for this patient for a few minutes.

        `slot_id` comes from `search_slots` and is opaque: the service issues it
        and only the service can resolve it. An unknown one is `NOT_FOUND`; a slot
        another patient holds or booked is `SLOT_TAKEN`. Repeating the hold of a
        slot this patient already holds returns that same hold (plan V13), so a
        re-run of the same turn cannot pile up holds.
        """
        ...

    async def create_appointment(
        self,
        tenant_id: TenantId,
        patient: PatientRef,
        hold_id: str,
        full_name: str,
        *,
        idempotency_key: str,
    ) -> Appointment:
        """Turn this patient's hold into an appointment.

        `hold_id` is never shown to the model: our code reads it from the
        `booking_actions` row the hold created, so the model can neither invent
        nor reuse one.

        `full_name` is the patient's own answer, validated by the tool and sent
        on. It is never logged and never stored by us (hard rule 8);
        `tool_executions` records the argument NAME only.

        An expired or released hold is `HOLD_EXPIRED`. A hold this patient
        already converted returns that same appointment (plan V13).
        """
        ...

    async def reschedule_appointment(
        self,
        tenant_id: TenantId,
        patient: PatientRef,
        appointment_id: str,
        new_hold_id: str,
        *,
        idempotency_key: str,
    ) -> Appointment:
        """Move an existing appointment onto a new hold of this patient's.

        The appointment keeps its id and its reference (a proposal). Another
        patient's appointment or hold is `NOT_FOUND`, never `403`: a 403 would
        confirm that it exists.
        """
        ...

    async def cancel_appointment(
        self, tenant_id: TenantId, patient: PatientRef, appointment_id: str, *, idempotency_key: str
    ) -> Appointment:
        """Cancel one of this patient's appointments, and return it cancelled.

        Cancelling an already-cancelled appointment returns it rather than
        failing (plan V13): a re-run of the same turn must not turn a completed
        cancellation into an error the patient would be told about.
        """
        ...


__all__ = [
    "Appointment",
    "AppointmentStatus",
    "BookingClient",
    "BookingError",
    "ClinicInfo",
    "Doctor",
    "Hold",
    "Location",
    "OpeningHours",
    "PatientBookingClient",
    "PatientRef",
    "Service",
    "Slot",
]
