"""The tools the model may call (hard rule 3).

Eight: four read-only, and four that change something at the Booking Service. The
model can ASK for any of them; this package decides what actually runs, with what
arguments, for which patient, and against which tenant.

The four changing tools are bounded three ways beyond argument validation: one
change per patient message (V12), none started with under eight seconds of turn
budget left (V15), and none EXECUTED unless a reply describing it was already sent
(V3's gate). None of those is a prompt instruction.
"""

from app.agent.tools.appointments import (
    MAX_APPOINTMENTS_RETURNED,
    ListMyAppointments,
)
from app.agent.tools.base import (
    MAX_BOOKING_CHANGES_PER_TURN,
    MIN_SECONDS_FOR_A_BOOKING_CHANGE,
    OPAQUE_ID,
    TOOL_NAME_PATTERN,
    UNKNOWN_TOOL_NAME,
    BookingOutcome,
    BookingState,
    ChangePhase,
    ChangeStatus,
    InFlightChange,
    NoArguments,
    PatientContext,
    Tool,
    ToolCallRecord,
    ToolContext,
    ToolExecutionStatus,
)
from app.agent.tools.changes import (
    BookAppointment,
    BookAppointmentArgs,
    CancelAppointment,
    CancelAppointmentArgs,
    RescheduleAppointment,
)
from app.agent.tools.clinic import GetClinicInformation
from app.agent.tools.doctors import ListDoctors
from app.agent.tools.errors import BOOKING_MESSAGES, ToolCrashed, ToolFailure
from app.agent.tools.holds import HoldAppointmentSlot, HoldAppointmentSlotArgs
from app.agent.tools.idempotency import WRITE_TOOLS, idempotency_key
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.slots import (
    MAX_SLOTS_RETURNED,
    SearchAvailableSlots,
    SearchAvailableSlotsArgs,
)


def default_registry() -> ToolRegistry:
    """The eight tools, in the order they are sent to the model.

    A function rather than a module-level singleton: arq runs several jobs at
    once in one process, and a shared mutable default is the kind of thing that
    becomes a race later. The registry itself holds no mutable state, so the
    worker builds one per runtime and nothing is lost.

    The order is FIXED, and it is not cosmetic: the whole list goes out on every
    model call, so a changing order would change the request between turns and
    defeat OpenAI's automatic prompt caching.

    Reads first, then writes, each immediately after the tool whose output it
    needs: `list_doctors` before `search_available_slots` (D5),
    `search_available_slots` before `hold_appointment_slot` (its `slot_id`),
    `list_my_appointments` before the two tools that take an `appointment_id`.
    """
    return ToolRegistry(
        (
            GetClinicInformation(),
            ListDoctors(),
            SearchAvailableSlots(),
            ListMyAppointments(),
            HoldAppointmentSlot(),
            BookAppointment(),
            RescheduleAppointment(),
            CancelAppointment(),
        ),
    )


__all__ = [
    "BOOKING_MESSAGES",
    "MAX_APPOINTMENTS_RETURNED",
    "MAX_BOOKING_CHANGES_PER_TURN",
    "MAX_SLOTS_RETURNED",
    "MIN_SECONDS_FOR_A_BOOKING_CHANGE",
    "OPAQUE_ID",
    "TOOL_NAME_PATTERN",
    "UNKNOWN_TOOL_NAME",
    "WRITE_TOOLS",
    "BookAppointment",
    "BookAppointmentArgs",
    "BookingOutcome",
    "BookingState",
    "CancelAppointment",
    "CancelAppointmentArgs",
    "ChangePhase",
    "ChangeStatus",
    "GetClinicInformation",
    "HoldAppointmentSlot",
    "HoldAppointmentSlotArgs",
    "InFlightChange",
    "ListDoctors",
    "ListMyAppointments",
    "NoArguments",
    "PatientContext",
    "RescheduleAppointment",
    "SearchAvailableSlots",
    "SearchAvailableSlotsArgs",
    "Tool",
    "ToolCallRecord",
    "ToolContext",
    "ToolCrashed",
    "ToolExecutionStatus",
    "ToolFailure",
    "ToolRegistry",
    "default_registry",
    "idempotency_key",
]
