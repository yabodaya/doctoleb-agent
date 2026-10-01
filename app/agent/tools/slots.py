"""search_available_slots: one doctor's free times in a window.

The only tool that takes arguments, and therefore the only one where the model
can get something wrong. Everything it sends is checked here before it reaches
the Booking Service.
"""

from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator
from pydantic_core import PydanticCustomError

from app.agent.clock import CLINIC_TIMEZONE, day_name, format_local, local_to_aware
from app.agent.tools.base import ToolContext

# No offset, no "Z", optional seconds. Clinic local time, always.
LOCAL_TIME = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$"

MAX_WINDOW_DAYS = 14
MAX_DAYS_AHEAD = 90
# At most this many slots in a result. A doctor with a dense week could
# otherwise return two hundred times, which costs tokens on every later model
# call of the turn and gives the model a list no WhatsApp reply could use.
MAX_SLOTS_RETURNED = 10

# Amendment B2 (replacing Q12's default): all three periods are defined, here,
# where the model reads them while writing the call. Leaving "morning" and
# "evening" undefined would make the model choose a window - and a different
# one each time.
DESCRIPTION = (
    "Find the available appointment times of one doctor between start and end. "
    "Use a doctor_id returned by list_doctors. start and end are clinic local "
    "time written YYYY-MM-DDTHH:MM, with no UTC offset. end must be after start "
    "and at most 14 days later. When the patient says morning, search from 08:00 "
    "to 12:00; afternoon, from 12:00 to 17:00; evening, from 17:00 to 21:00. "
    # VS-007's V10. The one sentence that makes a slot_id usable: it is opaque, so
    # the model has nothing to gain by interpreting it and everything to lose by
    # retyping it.
    "Each time has a slot_id: to hold it, pass that slot_id to "
    "hold_appointment_slot exactly as given."
)


class SearchAvailableSlotsArgs(BaseModel):
    """`str` with a pattern, deliberately - not `datetime` and not `NaiveDatetime`.

    Pydantic's datetime parsing accepts forms the model should never use,
    including a bare number as a Unix timestamp, and it would silently accept an
    offset the patient's clinic does not use. A pattern also goes INTO the
    schema, where it steers the model before it makes the mistake.
    """

    model_config = ConfigDict(extra="forbid")

    doctor_id: str = Field(
        min_length=1, max_length=64, description="A doctor_id returned by list_doctors."
    )
    start: str = Field(
        pattern=LOCAL_TIME,
        description="Start of the window, clinic local time, YYYY-MM-DDTHH:MM.",
    )
    end: str = Field(
        pattern=LOCAL_TIME,
        description="End of the window, clinic local time, YYYY-MM-DDTHH:MM.",
    )

    @model_validator(mode="after")
    def _window(self, info: ValidationInfo) -> "SearchAvailableSlotsArgs":
        """Everything the pattern cannot say.

        Each failure raises a `PydanticCustomError` with OUR type name, which
        `app/agent/tools/errors.py` maps to fixed text. Nothing here builds a
        message from a value.

        The after-validator does not run at all when field validation already
        failed, so `self.start` and `self.end` are known to match the pattern.
        """
        # Always passed by the registry; a KeyError here would be our bug.
        now: datetime = info.context["now"]
        try:
            start = datetime.fromisoformat(self.start)
            end = datetime.fromisoformat(self.end)
        except ValueError:
            # The pattern accepts 2026-02-30: it is shaped like a date and is
            # not one.
            raise PydanticCustomError("not_a_real_date", "not a real date") from None

        if end <= start:
            raise PydanticCustomError("range_order", "end must be after start")
        if end - start > timedelta(days=MAX_WINDOW_DAYS):
            raise PydanticCustomError("range_too_long", "window too long")

        aware_start = local_to_aware(start)
        aware_end = local_to_aware(end)
        if aware_end <= now:
            # The WHOLE window is behind us. A window that merely STARTS in the
            # past is fine and gets clamped: "today from 09:00" asked at noon is
            # a reasonable thing for the model to send.
            raise PydanticCustomError("range_in_past", "window in the past")
        if aware_start > now + timedelta(days=MAX_DAYS_AHEAD):
            raise PydanticCustomError("too_far_ahead", "too far ahead")
        return self

    def window(self, now: datetime) -> tuple[datetime, datetime]:
        """(start, end) as real instants, with each date's own UTC offset.

        `start` is clamped UP to `now`, never an error: it saves the model a
        second call for something our code can settle, and the fake refuses to
        return a past slot anyway.
        """
        start = local_to_aware(datetime.fromisoformat(self.start))
        end = local_to_aware(datetime.fromisoformat(self.end))
        return max(start, now), end


class SearchAvailableSlots:
    name = "search_available_slots"
    description = DESCRIPTION
    args_model = SearchAvailableSlotsArgs
    # Read-only: nothing here can change a booking (VS-007).
    changes_bookings = False

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        assert isinstance(args, SearchAvailableSlotsArgs)  # noqa: S101 - the registry guarantees it
        start, end = args.window(ctx.now)
        slots = await ctx.booking.search_slots(ctx.tenant_id, args.doctor_id, start, end)
        shown = slots[:MAX_SLOTS_RETURNED]
        return {
            "doctor_id": args.doctor_id,
            "timezone": CLINIC_TIMEZONE,
            # Echoed back so the model can see what was actually searched after
            # a clamp, rather than assuming its own values were used.
            "searched": {"start": format_local(start), "end": format_local(end)},
            "slots": [
                {
                    # VS-007's V10. The id is here now, because a tool can finally
                    # act on one. What stops the model inventing or reusing one:
                    # the ids are opaque tokens the service issues and only it can
                    # resolve; every id argument must match OPAQUE_ID, so "14:00
                    # tomorrow" is refused as invalid arguments; an unknown id is
                    # the fixed error `slot_not_found`; and the prompt forbids
                    # writing an id into a reply, so a stale one can only come from
                    # the model echoing itself - where it fails at the service.
                    "slot_id": slot.slot_id,
                    # The day name because the model is often answering "what
                    # about Monday?", and a date alone makes it do weekday
                    # arithmetic it is bad at.
                    "day": day_name(slot.start),
                    "start": format_local(slot.start),
                    "end": format_local(slot.end),
                }
                for slot in shown
            ],
            "more_available": len(slots) > len(shown),
        }
