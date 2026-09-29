"""get_clinic_information: the clinic's own details."""

from typing import Any

from pydantic import BaseModel

from app.agent.clock import WEEKDAY_NAMES
from app.agent.tools.base import NoArguments, ToolContext
from app.integrations.booking import ClinicInfo

DESCRIPTION = "Get the clinic's name, address, opening hours and policies. Takes no arguments."


class GetClinicInformation:
    name = "get_clinic_information"
    description = DESCRIPTION
    args_model = NoArguments

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        return as_payload(await ctx.booking.get_clinic(ctx.tenant_id))


def as_payload(info: ClinicInfo) -> dict[str, Any]:
    """`ClinicInfo` as the model sees it.

    Weekday NAMES rather than the numbers the DTO carries: the model answers a
    patient who asked about "Saturday", and asking it to map 5 to Saturday is
    arithmetic it does not need to do.

    A closed day says `{"day": ..., "closed": true}` and carries no times at
    all, so "closed" cannot disagree with an `opens` the model half-reads.

    The tenant id is NOT here, and no result anywhere carries it (hard rule 4).
    """
    hours: list[dict[str, Any]] = []
    for entry in sorted(info.opening_hours, key=lambda item: item.weekday):
        day = WEEKDAY_NAMES[entry.weekday % 7]
        if entry.closed:
            hours.append({"day": day, "closed": True})
        else:
            hours.append(
                {
                    "day": day,
                    "opens": entry.opens.strftime("%H:%M"),
                    "closes": entry.closes.strftime("%H:%M"),
                }
            )
    return {
        "name": info.name,
        "timezone": info.timezone,
        "locations": [{"name": place.name, "address": place.address} for place in info.locations],
        "opening_hours": hours,
        "policies": list(info.policies),
        # Empty for the demo clinic (Q7). The prompt says facts come only from
        # tool results, so with none here the model has no price to state.
        "pricing": list(info.pricing),
    }
