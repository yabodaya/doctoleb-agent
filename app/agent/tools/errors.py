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
        name_field, template = PROBLEMS.get(error_type, (True, FALLBACK_PROBLEM))
        loc = error.get("loc") or ()
        field = str(loc[0]) if loc and isinstance(loc[0], str) else None
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
