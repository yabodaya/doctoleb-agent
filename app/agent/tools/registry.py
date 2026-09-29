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
    DOCTOR_NOT_FOUND_MESSAGE,
    INVALID_ARGUMENTS_MESSAGE,
    INVALID_JSON_MESSAGE,
    NOT_FOUND_MESSAGE,
    UNAVAILABLE_MESSAGE,
    UNKNOWN_TOOL_MESSAGE,
    VALIDATION_MESSAGE,
    ToolCrashed,
    error_body,
    problems_from,
)
from app.integrations.booking import BookingError
from app.integrations.openai import ToolCallRequest, ToolSpec

_NAME = re.compile(TOOL_NAME_PATTERN)


def dump(payload: dict[str, Any]) -> str:
    """The tool message's content: compact JSON, Unicode kept as Unicode.

    `ensure_ascii=False` because a clinic's data can be Arabic, and escaping it
    would triple the token count for no gain.
    """
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def strip_titles(schema: Any) -> Any:
    """Pydantic's `title` keys and the model-level description, removed.

    `model_json_schema()` puts a `title` on every property and on the model
    (plan check U5). They are noise the model pays tokens for on EVERY model
    call, and the model-level description would compete with the tool's own.
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
                parameters=strip_titles(tool.args_model.model_json_schema()),
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

        try:
            payload = await tool.run(args, ctx)
        except BookingError as error:
            code, message = _booking_error(tool.name, error)
            return finish(
                error_body(code, message),
                tool.name,
                present,
                ToolExecutionStatus.ERROR,
                f"booking_{error.code.lower()}",
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


def _booking_error(tool_name: str, error: BookingError) -> tuple[str, str]:
    """A BookingError as a code and a fixed message.

    The contract's error body carries a `message`; it is never read. It comes
    from another system and can quote clinic or patient data.

    NOT_FOUND from the search means one specific, fixable thing - the doctor id
    was wrong - so it gets a message that tells the model how to fix it. From
    anywhere else it means the clinic itself is unknown, which the model cannot
    fix.
    """
    if error.code == "NOT_FOUND":
        if tool_name == "search_available_slots":
            return "doctor_not_found", DOCTOR_NOT_FOUND_MESSAGE
        return "not_found", NOT_FOUND_MESSAGE
    if error.code == "VALIDATION":
        return "booking_validation", VALIDATION_MESSAGE
    return "booking_unavailable", UNAVAILABLE_MESSAGE
