"""The gate between what the model asks for and what actually runs.

Hard rule 3 lives here. The model can ask for anything - a tool that does not
exist, arguments that are not JSON, a `tenant_id` it invented - and every one of
those is untrusted input, exactly like a patient's message. This module decides,
one case at a time, what happens next, and records each decision as codes and
counts only.
"""

import json
import re
import time
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from app.agent.tools.base import (
    TOOL_NAME_PATTERN,
    UNKNOWN_TOOL_NAME,
    Tool,
    ToolCallRecord,
    ToolContext,
    ToolExecutionStatus,
)
from app.agent.tools.errors import (
    BOOKING_MESSAGES,
    DOCTOR_NOT_FOUND_MESSAGE,
    INVALID_ARGUMENTS_MESSAGE,
    INVALID_JSON_MESSAGE,
    NOT_FOUND_MESSAGE,
    UNAVAILABLE_MESSAGE,
    UNKNOWN_TOOL_MESSAGE,
    VALIDATION_MESSAGE,
    ToolCrashed,
    ToolFailure,
    error_body,
    problems_from,
)
from app.integrations.booking import BookingError
from app.integrations.openai import ToolCallRequest, ToolSpec

_NAME = re.compile(TOOL_NAME_PATTERN)

# What a NOT_FOUND means, per changing tool. The same wire code says different
# fixable things depending on what the tool asked for, and the model can only act
# on the specific one: "use a slot_id from search" is advice, "not found" is not.
#
# `book_appointment` and `reschedule_appointment` map NOT_FOUND to `hold_expired`
# because the only thing they look up by id is a HOLD, and a hold this service no
# longer knows about is, from the patient's point of view, a hold that ran out -
# which is exactly what happens after the in-memory service restarts.
_CHANGE_NOT_FOUND: dict[str, str] = {
    "hold_appointment_slot": "slot_not_found",
    "book_appointment": "hold_expired",
    "reschedule_appointment": "hold_expired",
    "cancel_appointment": "appointment_not_found",
}

# BookingError.code -> (our code, how it is recorded), for a CHANGING tool.
# `UNCERTAIN` for the two codes that mean "we do not know": recording them as
# ERROR would assert the change did not happen, which is the one thing we cannot
# say (V6, hard rule 5).
_CHANGE_ERRORS: dict[str, tuple[str, ToolExecutionStatus, str]] = {
    "SLOT_TAKEN": ("slot_taken", ToolExecutionStatus.ERROR, "booking_slot_taken"),
    "HOLD_EXPIRED": ("hold_expired", ToolExecutionStatus.ERROR, "booking_hold_expired"),
    "VALIDATION": ("booking_validation", ToolExecutionStatus.ERROR, "booking_validation"),
    "UNAVAILABLE": ("booking_unavailable", ToolExecutionStatus.ERROR, "booking_unavailable"),
    "UNKNOWN_OUTCOME": (
        "outcome_unknown",
        ToolExecutionStatus.UNCERTAIN,
        "booking_unknown_outcome",
    ),
    "IDEMPOTENCY_CONFLICT": (
        "outcome_unknown",
        ToolExecutionStatus.UNCERTAIN,
        "booking_idempotency_conflict",
    ),
}


def dump(payload: dict[str, Any]) -> str:
    """The tool message's content: compact JSON, Unicode kept as Unicode.

    `ensure_ascii=False` because a clinic's data can be Arabic, and escaping it
    would triple the token count for no gain.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def strip_titles(schema: Any) -> Any:
    """Pydantic's `title` keys, removed at every depth.

    `model_json_schema()` puts a `title` on every property and on the model
    (plan check U5). They are noise the model pays tokens for on EVERY model
    call.

    PROPERTY descriptions stay: they are what steers the model before it makes a
    mistake ("a slot_id from search_available_slots, copied exactly"). Only the
    model-level one is removed, by `tool_schema` below.
    """
    if isinstance(schema, dict):
        return {
            key: strip_titles(value)
            for key, value in schema.items()
            if key not in ("title", "$schema")
        }
    if isinstance(schema, list):
        return [strip_titles(item) for item in schema]
    return schema


def tool_schema(args_model: Any) -> Any:
    """What the model is sent as a tool's `parameters`.

    The args model's CLASS DOCSTRING becomes the schema's top-level
    `description`, and it must not go out: it is written for whoever maintains the
    code, it names plan decisions and internal rules, it would compete with the
    tool's own description, and every tool pays for it on every model call. Two of
    ours share `NoArguments`, so without this three tools would send the model a
    paragraph about `list_doctors` needing `extra="forbid"`.

    Property descriptions are kept. Only the model-level one is dropped, and only
    at the top level - a nested object's description, if a tool ever has one, is
    part of that field's guidance.
    """
    rendered = strip_titles(args_model.model_json_schema())
    if isinstance(rendered, dict):
        rendered.pop("description", None)
    return rendered


class ToolRegistry:
    """The tools the model may call, and the only way one runs.

    Validated at construction rather than per call, so a malformed tool is one
    import-time failure instead of a surprise in front of a patient.
    """

    def __init__(self, tools: Sequence[Tool]) -> None:
        names: set[str] = set()
        for tool in tools:
            if not _NAME.fullmatch(tool.name):
                raise ValueError(f"invalid tool name: {tool.name!r}")
            if tool.name == UNKNOWN_TOOL_NAME:
                # Reserved: a `tool_executions` row reading "unknown" must only
                # ever mean "the model named something that is not a tool".
                raise ValueError(f"{UNKNOWN_TOOL_NAME!r} is reserved")
            if tool.name in names:
                raise ValueError(f"duplicate tool name: {tool.name!r}")
            if tool.args_model.model_config.get("extra") != "forbid":
                # Without this, an argument we do not know about is silently
                # dropped - including a `tenant_id` the model invented, which
                # must be REFUSED and reported (hard rule 4).
                raise ValueError(f"{tool.name}: args_model must set extra='forbid'")
            names.add(tool.name)
        self._tools = tuple(tools)
        self._by_name = {tool.name: tool for tool in tools}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self._tools)

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        """What is sent to the model on every call, in a fixed order.

        Fixed order because the whole list goes in every request: a set's
        iteration order would change the request between turns and defeat
        OpenAI's automatic prompt caching.
        """
        return tuple(
            ToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=tool_schema(tool.args_model),
            )
            for tool in self._tools
        )

    def _declared(self, tool: Tool) -> tuple[str, ...]:
        return tuple(sorted(tool.args_model.model_fields))

    async def execute(
        self, call: ToolCallRequest, ctx: ToolContext, *, sequence: int, model_call: int
    ) -> tuple[str, ToolCallRecord]:
        """Run one tool call. Returns the `tool` message content and its record.

        Every case returns rather than raising, except a genuine bug in a tool
        (`ToolCrashed`). That asymmetry is the design: everything the MODEL can
        get wrong is something it can be told about and fix on the next call,
        and everything WE get wrong ends the turn and produces a dead letter.
        """
        started = time.monotonic()

        def finish(
            payload: dict[str, Any],
            tool_name: str,
            argument_names: tuple[str, ...],
            status: ToolExecutionStatus,
            error_code: str | None,
        ) -> tuple[str, ToolCallRecord]:
            elapsed = int((time.monotonic() - started) * 1000)
            record = ToolCallRecord(
                sequence, model_call, tool_name, argument_names, status, error_code, elapsed
            )
            return dump(payload), record

        tool = self._by_name.get(call.name)
        if tool is None:
            # The name the model sent is NEVER echoed or stored: it is
            # model-written and could carry a patient's words (Q9).
            return finish(
                error_body(
                    "unknown_tool", UNKNOWN_TOOL_MESSAGE.format(tools=", ".join(self.names))
                ),
                UNKNOWN_TOOL_NAME,
                (),
                ToolExecutionStatus.UNKNOWN_TOOL,
                "unknown_tool",
            )

        declared = self._declared(tool)
        try:
            raw = json.loads(call.arguments)
        except (ValueError, RecursionError):
            # RecursionError as well as ValueError: deeply nested JSON
            # ("[[[[..." five thousand deep) blows the stack rather than
            # failing to parse, and an unhandled one would end the turn.
            return finish(
                error_body("invalid_json", INVALID_JSON_MESSAGE),
                tool.name,
                (),
                ToolExecutionStatus.INVALID_ARGUMENTS,
                "invalid_json",
            )
        if not isinstance(raw, dict):
            return finish(
                error_body("invalid_json", INVALID_JSON_MESSAGE),
                tool.name,
                (),
                ToolExecutionStatus.INVALID_ARGUMENTS,
                "invalid_json",
            )

        present = tuple(sorted(name for name in declared if name in raw))
        try:
            # `context` carries `now` to the window validator. Always passed, so
            # a KeyError there would be our bug, not the model's.
            args = tool.args_model.model_validate(raw, context={"now": ctx.now})
        except ValidationError as error:
            rows = error.errors(include_input=False, include_url=False, include_context=False)
            return finish(
                error_body(
                    "invalid_arguments",
                    INVALID_ARGUMENTS_MESSAGE.format(tool=tool.name),
                    problems=problems_from(rows, declared),
                ),
                tool.name,
                present,
                ToolExecutionStatus.INVALID_ARGUMENTS,
                "invalid_arguments",
            )
        except Exception as crash:  # noqa: BLE001 - re-raised as our own safe type
            raise ToolCrashed(tool.name, type(crash).__name__) from None

        changes = bool(getattr(tool, "changes_bookings", False))
        try:
            payload = await tool.run(args, ctx)
        except ToolFailure as refusal:
            # OUR rule declined it, and nothing was sent to the Booking Service.
            # REFUSED rather than ERROR, so an operator reading `tool_executions`
            # can tell "we said no" from "the service failed" (V3, V12, V15).
            return finish(
                error_body(refusal.code, BOOKING_MESSAGES[refusal.code]),
                tool.name,
                present,
                ToolExecutionStatus.REFUSED,
                refusal.code,
            )
        except BookingError as error:
            if changes:
                code, status, error_code = _change_error(tool.name, error)
            else:
                code, message = _booking_error(tool.name, error)
                return finish(
                    error_body(code, message),
                    tool.name,
                    present,
                    ToolExecutionStatus.ERROR,
                    f"booking_{error.code.lower()}",
                )
            return finish(
                error_body(code, BOOKING_MESSAGES[code]), tool.name, present, status, error_code
            )
        except ToolCrashed:
            raise
        except Exception as crash:  # noqa: BLE001 - a bug in our code (Q6)
            raise ToolCrashed(tool.name, type(crash).__name__) from None

        return finish(payload, tool.name, present, ToolExecutionStatus.OK, None)

    def skipped(
        self, call: ToolCallRequest, *, sequence: int, model_call: int, reason: str
    ) -> tuple[str, ToolCallRecord]:
        """A call we record but deliberately do not run.

        It still gets a `tool` message, because OpenAI requires one per
        `tool_call_id`. The name is resolved the same way as in `execute`, so a
        skipped call the model invented is still stored as "unknown".
        """
        name = call.name if call.name in self._by_name else UNKNOWN_TOOL_NAME
        return dump(
            error_body("not_run", "This call was not run. Answer the patient with what you have.")
        ), ToolCallRecord(sequence, model_call, name, (), ToolExecutionStatus.SKIPPED, reason, 0)


def _change_error(tool_name: str, error: BookingError) -> tuple[str, ToolExecutionStatus, str]:
    """A `BookingError` from a CHANGING tool, as a code and how to record it.

    Two things make this different from the read-side mapping below. NOT_FOUND is
    per tool, because the model can only act on the specific advice. And
    UNKNOWN_OUTCOME and IDEMPOTENCY_CONFLICT are recorded `UNCERTAIN`, not
    `ERROR`: the change may have been applied, and a row saying ERROR would be a
    claim that it was not (V6, hard rule 5).
    """
    if error.code == "NOT_FOUND":
        code = _CHANGE_NOT_FOUND.get(tool_name, "appointment_not_found")
        return code, ToolExecutionStatus.ERROR, "booking_not_found"
    mapped = _CHANGE_ERRORS.get(error.code)
    if mapped is None:  # pragma: no cover - CODES is closed, and every code is above
        return "booking_unavailable", ToolExecutionStatus.ERROR, "booking_unavailable"
    return mapped


def _booking_error(tool_name: str, error: BookingError) -> tuple[str, str]:
    """A BookingError as a code and a fixed message.

    The contract's error body carries a `message`; it is never read. It comes
    from another system and can quote clinic or patient data.

    NOT_FOUND from the search means one specific, fixable thing - the doctor id
    was wrong - so it gets a message that tells the model how to fix it. From
    anywhere else it means the clinic itself is unknown, which the model cannot
    fix.

    Anything this table does not name - `UNKNOWN_OUTCOME` included - falls through
    to `booking_unavailable`. That is a deliberate defence: a read that failed
    changed nothing, so "we could not check" is the whole truth, and an
    UNKNOWN_OUTCOME reaching a read tool would be a bug in the client rather than
    a state a patient should hear about (V6's read/write rule).
    """
    if error.code == "NOT_FOUND":
        if tool_name == "search_available_slots":
            return "doctor_not_found", DOCTOR_NOT_FOUND_MESSAGE
        return "not_found", NOT_FOUND_MESSAGE
    if error.code == "VALIDATION":
        return "booking_validation", VALIDATION_MESSAGE
    return "booking_unavailable", UNAVAILABLE_MESSAGE
