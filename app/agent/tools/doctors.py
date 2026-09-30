"""list_doctors: who works at the clinic, and their ids."""

from typing import Any

from pydantic import BaseModel

from app.agent.tools.base import NoArguments, ToolContext

# Decision D5, said twice on purpose: once here, where the model reads it while
# choosing a tool, and once in the system prompt. `search_available_slots` does
# no name lookup, so a model that skips this step has no valid id to search
# with - and an invented id is the one mistake that would look like a real
# answer.
DESCRIPTION = (
    "List the clinic's doctors with their doctor_id, specialty and services. "
    "Call this before search_available_slots: the doctor_id it returns is the "
    "only valid way to name a doctor. Takes no arguments."
)


class ListDoctors:
    name = "list_doctors"
    description = DESCRIPTION
    args_model = NoArguments
    # Read-only: nothing here can change a booking (VS-007).
    changes_bookings = False

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        doctors = await ctx.booking.list_doctors(ctx.tenant_id)
        return {
            "doctors": [
                {
                    "doctor_id": doctor.doctor_id,
                    "name": doctor.name,
                    "specialty": doctor.specialty,
                    # No service_id: no VS-006 tool takes one, and an id the
                    # model can see but cannot use is an invitation to invent a
                    # call for it. VS-007 adds it when a tool can use it.
                    "services": [
                        {"name": service.name, "duration_minutes": service.duration_minutes}
                        for service in doctor.services
                    ],
                }
                for doctor in doctors
            ]
        }
