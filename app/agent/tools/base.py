"""What a tool is, what it may use, and what running one produces.

Hard rule 3: the LLM never gets database or raw HTTP access. It can only ask
for a tool by name, and every tool validates its arguments with Pydantic. This
module is the shape of that promise.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.integrations.booking import BookingClient
from app.tenants.ids import TenantId

# OpenAI's rule for a function name (see FunctionDefinition in the SDK).
TOOL_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"

# The sentinel stored when the model named a tool that is not registered. It is
# a reserved name: the registry refuses to register a tool called this, so a row
# reading "unknown" can only ever mean what it says.
UNKNOWN_TOOL_NAME = "unknown"


class ToolExecutionStatus(StrEnum):
    """Mirrors `app.db.enums.ToolExecutionStatus`.

    Duplicated rather than imported, because `app/agent/` may not import
    `app/db/` at all (hard rule 3, enforced by a test). A test keeps the two
    equal, so drift is a failing test rather than a row PostgreSQL rejects
    inside a job.
    """

    OK = "OK"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class NoArguments(BaseModel):
    """For a tool that takes nothing.

    Still `extra="forbid"`: a model that sends `{"doctor": "Karim"}` to
    `list_doctors` must be told, not silently obeyed.
    """

    model_config = ConfigDict(extra="forbid")


class ToolContext:
    """What a tool may use, built by OUR code once per turn (hard rule 4).

    `tenant_id` comes from `Turn.tenant_id`, which came from the resolver, which
    came from the receiving phone_number_id. It is never an argument, never in a
    schema, and never reachable from anything the model wrote.

    A plain class with `__slots__` rather than a dataclass so it has no
    generated `__repr__` that could grow a field later; the one here prints the
    tenant (a clinic identifier, safe) and nothing else.
    """

    __slots__ = ("booking", "now", "tenant_id")

    def __init__(self, tenant_id: TenantId, booking: BookingClient, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("ToolContext.now must be timezone-aware")
        self.tenant_id = tenant_id
        self.booking = booking
        # Aware UTC, read ONCE per turn from the injected clock. Every tool in
        # the turn sees the same instant, so two searches in one turn cannot
        # disagree about what "now" is.
        self.now = now

    def __repr__(self) -> str:
        return f"ToolContext(tenant_id={self.tenant_id!r})"


@runtime_checkable
class Tool(Protocol):
    """One thing the model may ask for.

    `args_model` must set `extra="forbid"`; the registry refuses a tool whose
    does not. That is what makes "the model sent an argument we do not know
    about" a reported error instead of a silently ignored field - and it is the
    mechanism that rejects a `tenant_id` the model invented.
    """

    name: str
    description: str
    args_model: type[BaseModel]

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        """Do the read and return a JSON-serialisable result.

        `args` is already validated. Anything raised here that is not a
        `BookingError` is a bug in our code, and the registry turns it into
        `ToolCrashed` carrying the exception CLASS name only.
        """
        ...


class ToolCallRecord:
    """One row of `tool_executions`, as plain data (Q9).

    Nothing on it can hold content. `tool_name` is a REGISTERED name or
    `UNKNOWN_TOOL_NAME` - never the model-written name, which could carry a
    patient's words. `argument_names` holds DECLARED parameter names only, never
    values and never an undeclared key, which is model-written too.

    `sequence` and `model_call` exist because every row of a turn is written in
    ONE transaction, and PostgreSQL's `now()` is constant within a transaction,
    so `created_at` cannot order them.
    """

    __slots__ = (
        "argument_names",
        "duration_ms",
        "error_code",
        "model_call",
        "sequence",
        "status",
        "tool_name",
    )

    def __init__(
        self,
        sequence: int,
        model_call: int,
        tool_name: str,
        argument_names: tuple[str, ...],
        status: ToolExecutionStatus,
        error_code: str | None,
        duration_ms: int,
    ) -> None:
        self.sequence = sequence
        self.model_call = model_call
        self.tool_name = tool_name
        self.argument_names = argument_names
        self.status = status
        self.error_code = error_code
        self.duration_ms = duration_ms

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ToolCallRecord):
            return NotImplemented
        return all(getattr(self, name) == getattr(other, name) for name in self.__slots__)

    def __repr__(self) -> str:
        return (
            f"ToolCallRecord(sequence={self.sequence}, model_call={self.model_call}, "
            f"tool_name={self.tool_name!r}, argument_names={self.argument_names!r}, "
            f"status={self.status.value!r}, error_code={self.error_code!r})"
        )
