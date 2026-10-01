"""The fixed vocabulary of tool errors, and the one exception we raise.

Everything the model is told when a tool call goes wrong comes from the tables
in this module. Nothing is ever built from Pydantic's `msg`, `input` or `ctx`,
or from `str(ValidationError)` - that last one QUOTES THE INPUT, confirmed with
a sentinel in plan check U5. An unknown tool name and an undeclared argument key
are model-written too, so neither is ever echoed back.

Hard rule 8 is usually about logs. Here it is also about the request we send to
OpenAI on the NEXT model call: an error that echoed the patient's words would
send them round again, and put them in a row of `tool_executions`.
"""

from typing import Any

# Pydantic error `type` -> (which argument to name, what to say).
#
# `None` for the argument means "do not name one", and is used wherever naming
# it would mean echoing model-written text - `extra_forbidden`'s `loc` IS the
# key the model invented (U5) - or where the problem is about the pair of values
# rather than one field.
FIELD_PROBLEMS: dict[tuple[str, str], str] = {
    # VS-007. Looked up by (type, field) BEFORE the by-type table below, so the
    # window messages VS-006 pins for `start` and `end` are untouched while the
    # new id and name arguments get advice the model can act on.
    ("string_pattern_mismatch", "slot_id"): (
        "must be a slot_id copied exactly from search_available_slots"
    ),
    ("string_pattern_mismatch", "appointment_id"): (
        "must be an appointment_id copied exactly from list_my_appointments"
    ),
    ("string_too_short", "full_name"): "must be the patient's full name",
    ("name_too_short", "full_name"): "must be the patient's full name",
    ("name_has_digits", "full_name"): "must be a person's name, with no digits",
    ("name_not_printable", "full_name"): "must be a person's name, with no hidden characters",
}

PROBLEMS: dict[str, tuple[bool, str]] = {
    # (name_the_field, message)
    "missing": (True, "is required"),
    "extra_forbidden": (
        False,
        "unexpected arguments are not allowed; the allowed arguments are: {allowed}",
    ),
    "string_type": (True, "must be a string"),
    "string_too_short": (True, "must not be empty"),
    "string_too_long": (True, "is too long"),
    "string_pattern_mismatch": (
        True,
        "must be clinic local time written YYYY-MM-DDTHH:MM, with no UTC offset",
    ),
    "not_a_real_date": (False, "start or end is not a real date and time"),
    "range_order": (False, "end must be after start"),
    "range_too_long": (False, "the search window can be at most 14 days"),
    "range_in_past": (
        False,
        "the whole window is in the past; use the current date and time you were given",
    ),
    "too_far_ahead": (False, "start can be at most 90 days from today"),
}
FALLBACK_PROBLEM = "is not valid"

# The tool-error bodies, keyed by our own code. Fixed text: the model reads
# these and acts on them, so they say what to do, not what went wrong.
UNKNOWN_TOOL_MESSAGE = "There is no tool with that name. The tools are: {tools}."
INVALID_JSON_MESSAGE = "The arguments must be one JSON object."
INVALID_ARGUMENTS_MESSAGE = "Fix the arguments and call {tool} again."
DOCTOR_NOT_FOUND_MESSAGE = "No doctor has that doctor_id. Call list_doctors to get the ids."
NOT_FOUND_MESSAGE = "The clinic's booking system has no record of that."
VALIDATION_MESSAGE = "The booking system refused those values. Check them and try once more."
UNAVAILABLE_MESSAGE = (
    "The clinic's booking system could not be reached. Do not guess: tell the patient "
    "the clinic team will get back to them."
)

# VS-007's booking errors and refusals, keyed by our own code. Fixed text: the
# model reads these and acts on them, so each says what to do next, and none is
# built from a value the model or the service supplied.
#
# Three of them carry the whole weight of hard rule 5:
#  - `slot_taken` and `hold_expired` both say "nothing was held or booked", so the
#    model cannot read a failure as a success;
#  - `outcome_unknown` forbids BOTH claims. It is the only honest answer when a
#    write's result was lost, and the one a model is most likely to get wrong.
BOOKING_MESSAGES: dict[str, str] = {
    "slot_taken": (
        "Someone else took that time just now, so nothing was held or booked. "
        "Search again and offer the patient other available times."
    ),
    "slot_not_found": (
        "No available time has that slot_id. Use a slot_id from "
        "search_available_slots, copied exactly; never make one up."
    ),
    "appointment_not_found": (
        "This patient has no upcoming appointment with that appointment_id. "
        "Call list_my_appointments to get the ids."
    ),
    "hold_expired": (
        "The hold on that time ran out, so nothing was booked or changed. "
        "Search again and offer the patient available times."
    ),
    "booking_validation": VALIDATION_MESSAGE,
    "booking_unavailable": (
        "The clinic's booking system could not be reached, so nothing was changed. "
        "Do not guess: tell the patient the clinic team will get back to them."
    ),
    "outcome_unknown": (
        "The booking system did not confirm what happened, so nobody knows yet "
        "whether this worked. Never say that it worked and never say that it "
        "failed: tell the patient the clinic team will check and get back to them."
    ),
    "nothing_to_confirm": (
        "No held time is waiting for this patient's confirmation. Search, call "
        "hold_appointment_slot, tell the patient the details and ask them to "
        "confirm first."
    ),
    "confirmation_needed": (
        "The patient has not confirmed this yet: it was prepared while answering "
        "this same message, or they have not seen the details. Tell the patient "
        "the details, ask them to confirm, and wait for their reply."
    ),
    "one_change_per_message": (
        "Only one booking change can be made per patient message, and one was "
        "already made. Tell the patient what happened and ask what they want next."
    ),
    "turn_time_low": (
        "There is not enough time left to change the booking safely, so nothing "
        "was changed. Ask the patient to send their confirmation again."
    ),
}


class ToolFailure(Exception):
    """OUR code declined to run a booking change, or refused it outright.

    Not a `BookingError`: nothing was sent to the Booking Service, so nothing can
    have happened. Not a `ToolCrashed` either: this is a rule working as intended,
    and the model is told what to do about it.

    Every one of these is recorded `REFUSED` with the code as its `error_code`
    (V3's gate, V12's one change per message, V15's budget floor). That status
    exists so an operator can tell "we declined" from "the model got it wrong"
    (`INVALID_ARGUMENTS`) and from "the service failed" (`ERROR`).
    """

    def __init__(self, code: str) -> None:
        if code not in BOOKING_MESSAGES:
            raise ValueError("unknown tool failure code")
        self.code = code
        super().__init__(code)


class ToolCrashed(Exception):
    """A tool raised something that is not a `BookingError`: a bug in our code.

    Carries the tool name (ours, registered) and the exception CLASS name, never
    its message - an exception message is the least controlled string in the
    system and this one came from code that had just been handed model-written
    arguments (Q6, hard rule 8).

    It ends the turn PERMANENTLY rather than being reported to the model: a bug
    is not something the model can fix by calling again, and hard rule 11 wants
    a dead letter rather than a stranded job.
    """

    def __init__(self, tool_name: str, error_class: str) -> None:
        self.tool_name = tool_name
        self.error_class = error_class
        super().__init__(f"{tool_name} raised {error_class}")


def error_body(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """The one shape every tool error takes, so the model learns one shape."""
    body: dict[str, Any] = {"code": code, "message": message}
    body.update(extra)
    return {"error": body}


def problems_from(errors: list[dict[str, Any]], allowed: tuple[str, ...]) -> list[dict[str, Any]]:
    """Pydantic's error list, translated through PROBLEMS and nothing else.

    `errors` must already have been produced with `include_input=False`,
    `include_url=False` and `include_context=False`. Even so, only `type` and
    `loc` are read here, and `loc` only when the table says the field is safe to
    name - which it is exactly when the field is one WE declared.
    """
    declared = set(allowed)
    problems: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str]] = set()
    for error in errors:
        error_type = str(error.get("type", ""))
        loc = error.get("loc") or ()
        raw_field = str(loc[0]) if loc and isinstance(loc[0], str) else None
        # FIELD_PROBLEMS first (VS-007): a `string_pattern_mismatch` on `slot_id`
        # needs different advice from one on `start`, and looking the pair up
        # first is what lets VS-006's window messages stay exactly as they were.
        # Only a field WE declared can reach this lookup.
        specific = FIELD_PROBLEMS.get((error_type, raw_field)) if raw_field in declared else None
        if specific is not None:
            name_field, template = True, specific
        else:
            name_field, template = PROBLEMS.get(error_type, (True, FALLBACK_PROBLEM))
        field = raw_field
        # The last line of defence: even when the table says "name the field",
        # only a field we declared is ever echoed.
        if not name_field or field not in declared:
            field = None
        message = template.format(allowed=", ".join(allowed))
        key = (field, message)
        if key in seen:
            continue
        seen.add(key)
        problems.append({"argument": field, "problem": message})
    return problems
