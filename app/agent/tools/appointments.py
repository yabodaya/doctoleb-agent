"""list_my_appointments: what this patient already has booked.

The only patient-side READ. It takes no arguments at all - not even an identity -
because the system knows who the patient is (V5) and anything the model could pass
would be a way to ask about somebody else (hard rule 4).

It is what makes `reschedule_appointment` and `cancel_appointment` possible: both
need an `appointment_id`, and this is the only place one comes from.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.agent.clock import day_name, format_local
from app.agent.tools.base import NoArguments, PatientContext, ToolContext
from app.agent.tools.errors import ToolCrashed
from app.integrations.booking import Appointment

# At most this many. A patient with a long history would otherwise spend the
# turn's tokens on appointments nobody asked about, and a WhatsApp reply cannot
# use a list that long anyway.
MAX_APPOINTMENTS_RETURNED = 10

DESCRIPTION = (
    "List this patient's upcoming appointments, each with its appointment_id, "
    "doctor, day and time. The system knows who the patient is, so this takes no "
    "arguments."
)

# What the model is told a status means. `PENDING_APPROVAL` is deliberately NOT
# called "booked" anywhere the model can read it (contract open question 3).
STATUS_WORDS = {
    "CONFIRMED": "confirmed",
    "PENDING_APPROVAL": "awaiting clinic approval",
}


def require_patient(tool_name: str, ctx: ToolContext) -> PatientContext:
    """The patient side, or a crash.

    A booking tool with no patient side is a WIRING bug in our code, not something
    the model can fix by calling again: there is no identity to act for. So it ends
    the turn PERMANENTLY with a dead letter (Q6) rather than being reported to the
    model, and it certainly never guesses (hard rule 4).
    """
    if ctx.patient is None:
        raise ToolCrashed(tool_name, "PatientContextMissing")
    return ctx.patient


def appointment_view(appointment: Appointment) -> dict[str, Any]:
    """One appointment, as the model sees it.

    `appointment_id` is here because two tools need it (plan conflict C15). The
    `reference` is NOT: it is the patient's code, it appears only in a receipt our
    own code writes, and a model that could see one could write one into a reply
    that claimed a booking.
    """
    return {
        "appointment_id": appointment.appointment_id,
        "doctor_id": appointment.doctor_id,
        "doctor_name": appointment.doctor_name,
        # The day name because the model is usually answering "the Wednesday one?",
        # and a date alone makes it do weekday arithmetic it is bad at.
        "day": day_name(appointment.start),
        "start": format_local(appointment.start),
        "end": format_local(appointment.end),
        "status": STATUS_WORDS.get(appointment.status, appointment.status.lower()),
    }


async def upcoming(ctx: ToolContext, patient: PatientContext) -> tuple[Appointment, ...]:
    """This patient's upcoming appointments, soonest first.

    Filtered on `ctx.now` - the turn's one clock read - rather than on the service's
    idea of now, so every tool in the turn agrees about what "upcoming" means.
    """
    found = await patient.bookings.list_appointments(ctx.tenant_id, patient.patient)
    return tuple(sorted((a for a in found if a.start >= ctx.now), key=_starts))


def _starts(appointment: Appointment) -> datetime:
    return appointment.start


class ListMyAppointments:
    name = "list_my_appointments"
    description = DESCRIPTION
    args_model = NoArguments
    # A read: it changes nothing, so it does not consume the message's one change
    # and a failure here is never "we do not know what happened".
    changes_bookings = False

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        patient = require_patient(self.name, ctx)
        found = await upcoming(ctx, patient)
        shown = found[:MAX_APPOINTMENTS_RETURNED]
        return {
            "appointments": [appointment_view(appointment) for appointment in shown],
            "more_available": len(found) > len(shown),
        }


__all__ = [
    "MAX_APPOINTMENTS_RETURNED",
    "STATUS_WORDS",
    "ListMyAppointments",
    "appointment_view",
    "require_patient",
    "upcoming",
]
