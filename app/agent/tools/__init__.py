"""The tools the model may call (hard rule 3).

Three, all read-only. The model can ASK for any of them; this package decides
what actually runs, with what arguments, and against which tenant.
"""

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
from app.agent.tools.clinic import GetClinicInformation
from app.agent.tools.doctors import ListDoctors
from app.agent.tools.errors import BOOKING_MESSAGES, ToolCrashed, ToolFailure
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.slots import (
    MAX_SLOTS_RETURNED,
    SearchAvailableSlots,
    SearchAvailableSlotsArgs,
)


def default_registry() -> ToolRegistry:
    """The three tools, in the order they are sent to the model.

    A function rather than a module-level singleton: arq runs several jobs at
    once in one process, and a shared mutable default is the kind of thing that
    becomes a race later. The registry itself holds no mutable state, so the
    worker builds one per runtime and nothing is lost.

    The order is `get_clinic_information`, `list_doctors`,
    `search_available_slots` - cheapest first, and `list_doctors` immediately
    before the tool that needs its output (D5).
    """
    return ToolRegistry(
        (GetClinicInformation(), ListDoctors(), SearchAvailableSlots()),
    )


__all__ = [
    "BOOKING_MESSAGES",
    "MAX_BOOKING_CHANGES_PER_TURN",
    "MAX_SLOTS_RETURNED",
    "MIN_SECONDS_FOR_A_BOOKING_CHANGE",
    "OPAQUE_ID",
    "TOOL_NAME_PATTERN",
    "UNKNOWN_TOOL_NAME",
    "BookingOutcome",
    "BookingState",
    "ChangePhase",
    "ChangeStatus",
    "GetClinicInformation",
    "InFlightChange",
    "ListDoctors",
    "NoArguments",
    "PatientContext",
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
]
