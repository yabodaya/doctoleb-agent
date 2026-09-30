"""book_appointment, reschedule_appointment, cancel_appointment: the three tools
that actually change something, and the gate that stops them being wrong.

**The gate (V3) is the point of this module.** A change prepared while answering
message *M* may be executed while answering message *N* only if the row is still
`PENDING`, *N* is not *M*, and a reply of ours was **actually sent** in between. The
first two conditions and the third arrive already computed: `BookingState.status`
and `BookingState.confirmable`, filled in by SQL in T1. So these tools check a flag,
which means the rule cannot be half-applied by a tool that forgot a condition.

Why that matters more than a prompt instruction: a hallucinated booking costs a
patient a slot they never asked for, and a hallucinated cancellation costs them one
they wanted. The prompt asks the model to confirm first; this refuses if it did not
(hard rule 5).

`cancel_appointment` is deliberately a TWO-CALL tool. There is no hold to prepare a
cancellation with, so the first call prepares one and cancels nothing, and a second
call with the same `appointment_id` executes it. That gives a cancellation the same
two-message shape as a booking, with the same gate.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from app.agent.clock import day_name, format_local
from app.agent.tools.appointments import require_patient, upcoming
from app.agent.tools.base import (
    OPAQUE_ID,
    BookingOutcome,
    BookingState,
    ChangePhase,
    ChangeStatus,
    InFlightChange,
    NoArguments,
    PatientContext,
    ToolContext,
)
from app.agent.tools.errors import ToolFailure
from app.agent.tools.idempotency import idempotency_key
from app.agent.tools.receipts import (
    booked_receipt,
    cancelled_receipt,
    moved_receipt,
    prepared_cancellation_receipt,
)
from app.db.enums import BookingActionKind, BookingActionStatus
from app.integrations.booking import Appointment, BookingError

BOOK_DESCRIPTION = (
    "Book the time this patient has on hold. Call it only after the patient "
    "clearly confirmed the doctor, date, time and their name, in a message after "
    "the one where you told them those details. full_name is the name the patient "
    "gave for the appointment."
)
RESCHEDULE_DESCRIPTION = (
    "Move this patient's appointment to the new time they have on hold. Call it "
    "only after the patient clearly confirmed the move, in a message after the one "
    "where you told them the old and the new time. Takes no arguments."
)
CANCEL_DESCRIPTION = (
    "Cancel one of this patient's appointments, by its appointment_id from "
    "list_my_appointments. The first call cancels nothing: it prepares the "
    "cancellation so you can ask the patient to confirm. Call it again with the "
    "same appointment_id only after they confirm in a later message."
)

NEXT_STEP_BOOKED = (
    "Tell the patient it is booked. The booking details are added to your reply automatically."
)
NEXT_STEP_REQUESTED = (
    "Tell the patient the clinic received the request and will confirm it. Never say it is booked."
)
NEXT_STEP_MOVED = (
    "Tell the patient the appointment has been moved. The new details are added to "
    "your reply automatically."
)
NEXT_STEP_CANCEL_PREPARED = (
    "Tell the patient which appointment would be cancelled and ask them to "
    "confirm. Call cancel_appointment again with the same appointment_id only "
    "after they confirm in a later message. Nothing is cancelled yet."
)
NEXT_STEP_CANCELLED = (
    "Tell the patient it is cancelled. The details are added to your reply automatically."
)

MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 100


def check_gate(state: BookingState | None, kind: BookingActionKind) -> BookingState:
    """V3's gate, in the order plan section 5.7 fixes.

    The order matters because the messages differ in what they tell the model to do:

    1. an `EXPIRED` row of this kind -> `hold_expired`: search again, the time is
       free once more. Checked FIRST, because "your hold ran out" is more useful
       than "nothing is waiting";
    2. no row, or a row that is not a `PENDING` one of this kind ->
       `nothing_to_confirm`: hold something first;
    3. a `PENDING` row that is not confirmable -> `confirmation_needed`: it was
       prepared while answering this same message, or the patient never saw the
       details. Tell them and wait.

    Every one of these raises `ToolFailure`, so it is recorded `REFUSED` and costs
    the message nothing: our code declined, the model did not err, and the Booking
    Service was never called.
    """
    if state is not None and state.kind is kind and state.status is BookingActionStatus.EXPIRED:
        raise ToolFailure("hold_expired")
    if state is None or state.kind is not kind or state.status is not BookingActionStatus.PENDING:
        raise ToolFailure("nothing_to_confirm")
    if not state.confirmable:
        raise ToolFailure("confirmation_needed")
    return state


async def execute_change(
    tool_name: str,
    ctx: ToolContext,
    patient: PatientContext,
    kind: BookingActionKind,
    state: BookingState,
    request: dict[str, str],
    call: Any,
) -> Appointment:
    """The shared half of every executing tool: key, call, record, re-raise.

    One function so the six steps cannot drift between book, reschedule and cancel:
    compute the key over exactly the body we send (hard rule 6), mark the write in
    flight BEFORE the await, call the service, clear the flag on a normal return or
    a `BookingError` but never in a `finally` (R11), and record the outcome either
    way.

    `UNKNOWN_OUTCOME` and `IDEMPOTENCY_CONFLICT` are recorded `UNCERTAIN`, not
    `FAILED`: the change may have been applied, and a row saying FAILED would be a
    claim that it was not (V6).
    """
    key = idempotency_key(patient.inbox_event_id, tool_name, request)
    patient.begin_call(
        InFlightChange(
            kind=kind,
            phase=ChangePhase.EXECUTED,
            idempotency_key=key,
            action_id=state.action_id,
        )
    )
    try:
        appointment = await call(key)
    except BookingError as error:
        patient.end_call()
        uncertain = error.code in ("UNKNOWN_OUTCOME", "IDEMPOTENCY_CONFLICT")
        patient.record(
            BookingOutcome(
                kind=kind,
                phase=ChangePhase.EXECUTED,
                status=ChangeStatus.UNCERTAIN if uncertain else ChangeStatus.FAILED,
                error_code=f"booking_{error.code.lower()}",
                action_id=state.action_id,
                idempotency_key=key,
                appointment_id=state.appointment_id,
            )
        )
        raise
    patient.end_call()

    patient.record(
        BookingOutcome(
            kind=kind,
            phase=ChangePhase.EXECUTED,
            status=ChangeStatus.SUCCESS,
            action_id=state.action_id,
            appointment_id=appointment.appointment_id,
            idempotency_key=key,
            # The service's own status, not the model's opinion. Only CONFIRMED
            # earns a ✅ and lets G2 allow a "booked" claim (hard rule 5).
            confirmed=appointment.status == "CONFIRMED",
            receipt=_receipt_for(kind, appointment),
        )
    )
    return appointment


def _receipt_for(kind: BookingActionKind, appointment: Appointment) -> str:
    if kind is BookingActionKind.BOOK:
        return booked_receipt(appointment)
    if kind is BookingActionKind.RESCHEDULE:
        return moved_receipt(appointment)
    return cancelled_receipt(appointment)


def _done_view(appointment: Appointment, status: str, next_step: str) -> dict[str, Any]:
    return {
        "status": status,
        "appointment_id": appointment.appointment_id,
        "doctor_name": appointment.doctor_name,
        "day": day_name(appointment.start),
        "start": format_local(appointment.start),
        "end": format_local(appointment.end),
        "next_step": next_step,
    }


class BookAppointmentArgs(BaseModel):
    """One argument, and it is the patient's own words about themselves.

    `full_name`, not `patient_name`: the pinned test that forbids an argument the
    backend owns rejects anything starting with `patient`, and rightly - the patient
    is injected by our code. What the model may pass is the NAME the patient gave
    for the appointment, which is a fact only the patient has.

    It is sent to the Booking Service and **never stored or logged by us**:
    `tool_executions` records the argument NAME only (hard rule 8, V5).
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(
        min_length=MIN_NAME_LENGTH,
        max_length=MAX_NAME_LENGTH,
        description="The patient's full name, as they gave it.",
    )

    @field_validator("full_name", mode="after")
    @classmethod
    def _clean(cls, value: str) -> str:
        """NFC, whitespace collapsed, and three refusals.

        Each `PydanticCustomError` carries OUR type name, which
        `app/agent/tools/errors.py` maps to fixed text. Nothing here builds a
        message from the value, because `str(ValidationError)` quotes the input
        (plan check U6) and this input is a person's name.

        Normalising here rather than at the call site is what makes the idempotency
        key stable: a re-run that spells the same name in a different Unicode form
        must produce the same key (V1).
        """
        import unicodedata

        value = unicodedata.normalize("NFC", " ".join(value.split()))
        if len(value) < MIN_NAME_LENGTH:
            raise PydanticCustomError("name_too_short", "too short")
        if any(character.isdigit() for character in value):
            raise PydanticCustomError("name_has_digits", "digits")
        if any(unicodedata.category(character).startswith("C") for character in value):
            # A zero-width space is category Cf. A name carrying one would hash to
            # a different idempotency key while looking identical to everybody.
            raise PydanticCustomError("name_not_printable", "hidden characters")
        return value


class BookAppointment:
    name = "book_appointment"
    description = BOOK_DESCRIPTION
    args_model = BookAppointmentArgs
    changes_bookings = True

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        assert isinstance(args, BookAppointmentArgs)  # noqa: S101 - the registry guarantees it
        patient = require_patient(self.name, ctx)
        state = check_gate(patient.state, BookingActionKind.BOOK)
        patient.begin_change(ctx.seconds_left())

        hold_id = state.hold_id or ""
        request = {
            "full_name": args.full_name,
            "hold_id": hold_id,
            "patient_ref": patient.patient.value,
        }

        async def call(key: str) -> Appointment:
            return await patient.bookings.create_appointment(
                ctx.tenant_id, patient.patient, hold_id, args.full_name, idempotency_key=key
            )

        appointment = await execute_change(
            self.name, ctx, patient, BookingActionKind.BOOK, state, request, call
        )
        if appointment.status == "CONFIRMED":
            return _done_view(appointment, "booked", NEXT_STEP_BOOKED)
        # Contract open question 3. The clinic has not confirmed it, so nothing in
        # this result says "booked", and the receipt is ⏳ rather than ✅.
        return {
            **_done_view(appointment, "requested", NEXT_STEP_REQUESTED),
            "booked": False,
        }


class RescheduleAppointment:
    name = "reschedule_appointment"
    description = RESCHEDULE_DESCRIPTION
    args_model = NoArguments
    changes_bookings = True

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        patient = require_patient(self.name, ctx)
        state = check_gate(patient.state, BookingActionKind.RESCHEDULE)
        patient.begin_change(ctx.seconds_left())

        # Both ids come from `booking_actions`, never from the model: the hold it is
        # moving onto and the appointment it is moving. The model supplies nothing
        # here at all, which is why this tool takes no arguments.
        appointment_id = state.appointment_id or ""
        new_hold_id = state.hold_id or ""
        request = {
            "appointment_id": appointment_id,
            "new_hold_id": new_hold_id,
            "patient_ref": patient.patient.value,
        }

        async def call(key: str) -> Appointment:
            return await patient.bookings.reschedule_appointment(
                ctx.tenant_id,
                patient.patient,
                appointment_id,
                new_hold_id,
                idempotency_key=key,
            )

        appointment = await execute_change(
            self.name, ctx, patient, BookingActionKind.RESCHEDULE, state, request, call
        )
        return _done_view(appointment, "moved", NEXT_STEP_MOVED)


class CancelAppointmentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appointment_id: str = Field(
        pattern=OPAQUE_ID,
        description="An appointment_id from list_my_appointments.",
    )


class CancelAppointment:
    """Two calls, always.

    There is no hold to prepare a cancellation with, so the FIRST call prepares one
    and cancels nothing, and a second call with the same `appointment_id` executes
    it. That gives a cancellation the same two-message shape as a booking, and the
    same gate - which matters because a hallucinated cancellation costs a patient an
    appointment they wanted (V3, hard rule 5).
    """

    name = "cancel_appointment"
    description = CANCEL_DESCRIPTION
    args_model = CancelAppointmentArgs
    changes_bookings = True

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        assert isinstance(args, CancelAppointmentArgs)  # noqa: S101 - the registry guarantees it
        patient = require_patient(self.name, ctx)
        state = patient.state

        prepared_for_this = (
            state is not None
            and state.kind is BookingActionKind.CANCEL
            and state.status is BookingActionStatus.PENDING
            and state.appointment_id == args.appointment_id
        )
        if prepared_for_this:
            assert state is not None  # noqa: S101 - prepared_for_this proves it
            if not state.confirmable:
                # Prepared while answering this same message, or the patient has not
                # seen the details. Either way they have not confirmed.
                raise ToolFailure("confirmation_needed")
            return await self._execute(ctx, patient, state, args.appointment_id)
        return await self._prepare(ctx, patient, args.appointment_id)

    async def _prepare(
        self, ctx: ToolContext, patient: PatientContext, appointment_id: str
    ) -> dict[str, Any]:
        """The first call. It reads, records an intention, and changes nothing.

        The ownership read comes BEFORE `begin_change`, so a wrong id does not cost
        the message its one change. An id that is not this patient's raises the
        `NOT_FOUND` the service itself would raise.
        """
        appointment = next(
            (a for a in await upcoming(ctx, patient) if a.appointment_id == appointment_id),
            None,
        )
        if appointment is None:
            raise BookingError("NOT_FOUND")

        patient.begin_change(ctx.seconds_left())
        # No idempotency key: nothing is sent to the Booking Service (plan section
        # 5.2's table says so explicitly for this call).
        patient.record(
            BookingOutcome(
                kind=BookingActionKind.CANCEL,
                phase=ChangePhase.PROPOSED,
                status=ChangeStatus.SUCCESS,
                appointment_id=appointment_id,
                receipt=prepared_cancellation_receipt(appointment),
            )
        )
        return {
            "status": "cancellation_prepared",
            "cancelled": False,
            "appointment": {
                "appointment_id": appointment.appointment_id,
                "doctor_name": appointment.doctor_name,
                "day": day_name(appointment.start),
                "start": format_local(appointment.start),
            },
            "next_step": NEXT_STEP_CANCEL_PREPARED,
        }

    async def _execute(
        self,
        ctx: ToolContext,
        patient: PatientContext,
        state: BookingState,
        appointment_id: str,
    ) -> dict[str, Any]:
        patient.begin_change(ctx.seconds_left())
        request = {
            "appointment_id": appointment_id,
            "patient_ref": patient.patient.value,
        }

        async def call(key: str) -> Appointment:
            return await patient.bookings.cancel_appointment(
                ctx.tenant_id, patient.patient, appointment_id, idempotency_key=key
            )

        appointment = await execute_change(
            self.name, ctx, patient, BookingActionKind.CANCEL, state, request, call
        )
        return {
            "status": "cancelled",
            "appointment_id": appointment.appointment_id,
            "doctor_name": appointment.doctor_name,
            "day": day_name(appointment.start),
            "start": format_local(appointment.start),
            "next_step": NEXT_STEP_CANCELLED,
        }


__all__ = [
    "BOOK_DESCRIPTION",
    "CANCEL_DESCRIPTION",
    "MAX_NAME_LENGTH",
    "MIN_NAME_LENGTH",
    "NEXT_STEP_BOOKED",
    "NEXT_STEP_CANCELLED",
    "NEXT_STEP_CANCEL_PREPARED",
    "NEXT_STEP_MOVED",
    "NEXT_STEP_REQUESTED",
    "RESCHEDULE_DESCRIPTION",
    "BookAppointment",
    "BookAppointmentArgs",
    "CancelAppointment",
    "CancelAppointmentArgs",
    "RescheduleAppointment",
    "check_gate",
    "execute_change",
]
