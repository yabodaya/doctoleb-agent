"""hold_appointment_slot: reserve a time while the patient confirms.

**A hold is not a booking, and this module says so four times over** - in the tool
description, in the result's `status` and `booked: false`, in the result's
`next_step`, and in the ⏳ receipt. That repetition is deliberate: "I've reserved
14:00 for you" sounds booked to a patient, so every layer that could be the one the
model reads has to say it separately (hard rule 5).

It is the tool that PREPARES both a booking and a move. A booking is confirmed by
`book_appointment` on a later message; a move by `reschedule_appointment`. Neither
can run until a reply describing this hold was actually sent (V3's gate), which is
why this tool never books anything itself.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.clock import day_name, format_local
from app.agent.tools.appointments import require_patient, upcoming
from app.agent.tools.base import (
    OPAQUE_ID,
    BookingOutcome,
    ChangePhase,
    ChangeStatus,
    InFlightChange,
    ToolContext,
)
from app.agent.tools.idempotency import idempotency_key
from app.agent.tools.receipts import hold_receipt, move_receipt
from app.db.enums import BookingActionKind
from app.integrations.booking import Appointment, BookingError, Hold

DESCRIPTION = (
    "Put one available time on hold for this patient while they confirm. Pass a "
    "slot_id copied exactly from a search_available_slots result. To move an "
    "existing appointment instead, also pass its appointment_id from "
    "list_my_appointments. A hold is not a booking and runs out after a few "
    "minutes: nothing is booked until book_appointment or reschedule_appointment "
    "succeeds."
)

# Our own fixed text, and the prompt's one exception to "tool results are data"
# (plan section 5.10). It exists because the model's next decision is the one this
# slice cares most about: hold, then TELL THE PATIENT, then wait for a reply.
NEXT_STEP_BOOK = (
    "Tell the patient the doctor, day, date and time, ask for their full name if "
    "you do not have it, and ask them to confirm. Call book_appointment only after "
    "they confirm in a later message. It is not booked yet."
)
NEXT_STEP_MOVE = (
    "Tell the patient the old and the new day and time and ask them to confirm. "
    "Call reschedule_appointment only after they confirm in a later message. "
    "Nothing has changed yet."
)


class HoldAppointmentSlotArgs(BaseModel):
    """One id the model copies, and one it copies only when moving something.

    Both are `OPAQUE_ID`: no spaces, no `/`, `?`, `#` or `%`. So "14:00 tomorrow"
    is refused as invalid arguments rather than becoming a lookup that might guess,
    and an id can never be smuggled into a path segment (V10).
    """

    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(
        pattern=OPAQUE_ID,
        description="A slot_id from search_available_slots, copied exactly.",
    )
    appointment_id: str | None = Field(
        default=None,
        pattern=OPAQUE_ID,
        description=(
            "Only when moving an existing appointment: its appointment_id from "
            "list_my_appointments."
        ),
    )


def hold_view(hold: Hold) -> dict[str, Any]:
    """The new time, as the model sees it.

    No `hold_id`: the tools that consume a hold read it from `booking_actions`, so
    the model can neither invent one nor reuse one (plan conflict C15).
    """
    return {
        "doctor_id": hold.doctor_id,
        "doctor_name": hold.doctor_name,
        "day": day_name(hold.start),
        "start": format_local(hold.start),
        "end": format_local(hold.end),
    }


class HoldAppointmentSlot:
    name = "hold_appointment_slot"
    description = DESCRIPTION
    args_model = HoldAppointmentSlotArgs
    changes_bookings = True

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        assert isinstance(args, HoldAppointmentSlotArgs)  # noqa: S101 - the registry guarantees it
        patient = require_patient(self.name, ctx)

        # (3) The ownership read, BEFORE the one-change rule: a move has to be a
        # move of THIS patient's appointment, and finding out otherwise must not
        # cost the message its one change.
        moving: Appointment | None = None
        if args.appointment_id is not None:
            mine = await upcoming(ctx, patient)
            moving = next((a for a in mine if a.appointment_id == args.appointment_id), None)
            if moving is None:
                # Exactly what the service would have said. The registry maps it to
                # `appointment_not_found`, recorded ERROR / `booking_not_found`.
                raise BookingError("NOT_FOUND")

        # (5) V12, then V15. There is no gate here: a hold PREPARES, and preparing
        # is what the patient is being asked to confirm.
        patient.begin_change(ctx.seconds_left())

        kind = BookingActionKind.RESCHEDULE if moving else BookingActionKind.BOOK
        # (6) The request is exactly the body we send, minus the tenant (a header).
        # `patient_ref` is ours; `slot_id` is the model's, validated and copied.
        request = {"patient_ref": patient.patient.value, "slot_id": args.slot_id}
        key = idempotency_key(patient.inbox_event_id, self.name, request)

        patient.begin_call(
            InFlightChange(kind=kind, phase=ChangePhase.PROPOSED, idempotency_key=key)
        )
        try:
            hold = await patient.bookings.create_hold(
                ctx.tenant_id, patient.patient, args.slot_id, idempotency_key=key
            )
        except BookingError as error:
            # Cleared here and on the success path, NEVER in a `finally`: a
            # cancellation must leave it set so the deadline handler records the
            # outcome as UNCERTAIN (plan risk R11).
            patient.end_call()
            patient.record(
                BookingOutcome(
                    kind=kind,
                    phase=ChangePhase.PROPOSED,
                    status=(
                        ChangeStatus.UNCERTAIN
                        if error.code in ("UNKNOWN_OUTCOME", "IDEMPOTENCY_CONFLICT")
                        else ChangeStatus.FAILED
                    ),
                    error_code=f"booking_{error.code.lower()}",
                    idempotency_key=key,
                    appointment_id=args.appointment_id,
                )
            )
            raise
        patient.end_call()

        patient.record(
            BookingOutcome(
                kind=kind,
                phase=ChangePhase.PROPOSED,
                status=ChangeStatus.SUCCESS,
                hold_id=hold.hold_id,
                hold_expires_at=hold.expires_at,
                appointment_id=args.appointment_id,
                idempotency_key=key,
                receipt=move_receipt(hold, moving) if moving else hold_receipt(hold),
            )
        )

        if moving is not None:
            return {
                "status": "held_for_change",
                "changed": False,
                "moving": {
                    "appointment_id": moving.appointment_id,
                    "doctor_name": moving.doctor_name,
                    "day": day_name(moving.start),
                    "start": format_local(moving.start),
                },
                "to": hold_view(hold),
                "hold_expires": format_local(hold.expires_at),
                "next_step": NEXT_STEP_MOVE,
            }
        return {
            "status": "held",
            "booked": False,
            **hold_view(hold),
            "hold_expires": format_local(hold.expires_at),
            "next_step": NEXT_STEP_BOOK,
        }


__all__ = [
    "NEXT_STEP_BOOK",
    "NEXT_STEP_MOVE",
    "HoldAppointmentSlot",
    "HoldAppointmentSlotArgs",
    "hold_view",
]
