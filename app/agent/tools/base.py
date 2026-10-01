"""What a tool is, what it may use, and what running one produces.

Hard rule 3: the LLM never gets database or raw HTTP access. It can only ask
for a tool by name, and every tool validates its arguments with Pydantic. This
module is the shape of that promise.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.agent.tools.errors import ToolFailure
from app.db.enums import BookingActionKind, BookingActionStatus
from app.integrations.booking import BookingClient, PatientBookingClient, PatientRef
from app.tenants.ids import TenantId

# OpenAI's rule for a function name (see FunctionDefinition in the SDK).
TOOL_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,64}$"

# V10. Every id argument shares this pattern: it accepts the fake's transparent
# ids, UUIDs, base64url and our `slot_…` tokens, and refuses spaces, `/`, `?`,
# `#` and `%`. So "14:00 tomorrow" is invalid arguments rather than a lookup, and
# an id can never be smuggled into a path segment.
OPAQUE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:+=~-]{0,127}$"

# V12. At most one booking change per patient message. It is what keeps the
# receipt, the recording, V11 and the gate each handling ONE outcome per turn
# rather than a list with interactions nobody needs.
MAX_BOOKING_CHANGES_PER_TURN = 1

# V15, pinned by a test. A change is never STARTED with less than this much turn
# budget left. Most would-be unknown outcomes become a clean "send your
# confirmation again" instead of a dead letter and a human check.
MIN_SECONDS_FOR_A_BOOKING_CHANGE = 8.0

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

    VS-007 adds UNCERTAIN and REFUSED here and in `app.db.enums` in the same
    commit as the CHECK-widening migration, because the equality test means one
    without the other cannot pass.
    """

    OK = "OK"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    # A booking-changing call whose outcome is unknown (V6). Not ERROR: an error
    # means "it did not happen", which is the one thing we cannot say.
    UNCERTAIN = "UNCERTAIN"
    # Our own code declined to run it: the confirmation gate, one change per
    # message, or too little turn budget left (V3, V12, V15).
    REFUSED = "REFUSED"


class ChangePhase(StrEnum):
    """Whether a turn PREPARED a change or EXECUTED one.

    The whole slice turns on this distinction. A change is *prepared* while
    answering one patient message - a hold, or a first `cancel_appointment` call -
    and can only be *executed* while answering a later one, after a reply
    describing it was actually sent. Two messages, always (V3, hard rule 5).
    """

    PROPOSED = "PROPOSED"
    EXECUTED = "EXECUTED"


class ChangeStatus(StrEnum):
    """What the Booking Service said about a change.

    `UNCERTAIN` is a first-class answer, not a kind of failure: a write whose
    result was lost may have been applied, so "it failed" is as dishonest as "it
    worked" (V6).
    """

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class BookingState:
    """The conversation's latest booking action, as T1 found it.

    Plain data, loaded by the job from `booking_actions` and handed in on the
    `Turn`. `app/agent/` cannot read a database, so the gate's verdict arrives
    already computed: `confirmable` is the SQL answer to "was a reply of ours
    actually sent between the message that prepared this and the one being
    answered now?" (V3).

    `action_id` is `None` for a change made earlier in THIS turn: that copy never
    came from a row, and nothing may execute it - a change prepared while
    answering this message can only be confirmed by the next one.

    `hold_id` and `appointment_id` are `repr=False`: a `hold_id` must never reach
    the model, a log line or a traceback (plan conflict C15).
    """

    action_id: uuid.UUID | None
    kind: BookingActionKind
    status: BookingActionStatus
    confirmable: bool
    hold_id: str | None = field(default=None, repr=False)
    appointment_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BookingOutcome:
    """The one booking change a turn made, as plain data (V12: at most one).

    Returned on `AgentResult` for the JOB to record and to render, because
    `app/agent/` cannot open a transaction. Everything that could identify or
    describe is `repr=False`: this object is built on the path that also writes
    log lines and dead letters (hard rule 8).

    `receipt` is the exception that proves the rule: it IS content - a doctor's
    name and a time - and it exists because hard rule 5 says the proof a patient
    can rely on must be built from the service's own answer, never from the
    model's wording. It goes into `messages.text` and nowhere else.
    """

    kind: BookingActionKind
    phase: ChangePhase
    status: ChangeStatus
    error_code: str | None = None
    action_id: uuid.UUID | None = None
    hold_id: str | None = field(default=None, repr=False)
    hold_expires_at: datetime | None = None
    appointment_id: str | None = field(default=None, repr=False)
    idempotency_key: str | None = field(default=None, repr=False)
    # EXECUTED only: the service's status was final (CONFIRMED), not
    # PENDING_APPROVAL. G2 allows a "booked" claim only when this is true.
    confirmed: bool = False
    receipt: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class InFlightChange:
    """A booking call that has been sent and not yet answered.

    It exists for exactly one case: the turn deadline firing while a write is on
    the wire. The change may have been applied, so the turn must record an
    `UNCERTAIN` outcome carrying the key - which is the only thing that lets a
    human find the request at the Booking Service afterwards (V6, plan risk R11).
    """

    kind: BookingActionKind
    phase: ChangePhase
    idempotency_key: str | None = field(default=None, repr=False)
    action_id: uuid.UUID | None = None


class NoArguments(BaseModel):
    """For a tool that takes nothing.

    Still `extra="forbid"`: a model that sends `{"doctor": "Karim"}` to
    `list_doctors` must be told, not silently obeyed.
    """

    model_config = ConfigDict(extra="forbid")


class PatientContext:
    """What a booking tool may use for THIS patient, built by our code per turn.

    Separate from `ToolContext` because it is the half that must not exist unless
    the job wired it: a read-only turn has no patient side at all, and a booking
    tool that finds it missing raises `ToolCrashed` rather than inventing an
    identity (hard rule 4, plan V5).

    `patient` is a `PatientRef`, whose repr hides its value. It is built by our
    code from the contact's stored WhatsApp identity, never by the model, and it
    appears in no schema, no result, no log line, no table and no dead letter.

    A plain class with `__slots__`, and a repr that prints one integer: the
    `PatientRef`, the hold ids and the key must never reach a traceback.
    """

    __slots__ = (
        "bookings",
        "changes",
        "in_flight",
        "inbound_message_id",
        "inbox_event_id",
        "outcome",
        "patient",
        "state",
    )

    def __init__(
        self,
        bookings: PatientBookingClient,
        patient: PatientRef,
        inbox_event_id: uuid.UUID,
        inbound_message_id: uuid.UUID,
        state: BookingState | None = None,
    ) -> None:
        self.bookings = bookings
        self.patient = patient
        # The idempotency key's source (V1). Our webhook_inbox row, never a wamid.
        self.inbox_event_id = inbox_event_id
        # Which message this turn is answering: the gate's "N" (V3).
        self.inbound_message_id = inbound_message_id
        # Mutable within the turn: a change made here updates it, so a second
        # tool call in the same turn sees what the first one did.
        self.state = state
        self.outcome: BookingOutcome | None = None
        self.in_flight: InFlightChange | None = None
        self.changes = 0

    def begin_change(self, remaining_seconds: float | None) -> None:
        """Claim the message's one change, or refuse (V12, then V15).

        V12 first: a second change in one message is refused whatever the budget,
        so the answer does not depend on how fast the model was. Then V15: a
        change is not STARTED with too little turn left, because a write cut off
        in flight is an unknown outcome, a dead letter and a human check - and
        "send your confirmation again" costs the patient one message instead.
        """
        if self.changes >= MAX_BOOKING_CHANGES_PER_TURN:
            raise ToolFailure("one_change_per_message")
        if remaining_seconds is not None and remaining_seconds < MIN_SECONDS_FOR_A_BOOKING_CHANGE:
            raise ToolFailure("turn_time_low")
        self.changes += 1

    def begin_call(self, change: InFlightChange) -> None:
        """Mark a write as sent. Called BEFORE the await, always."""
        self.in_flight = change

    def end_call(self) -> None:
        """Mark it answered. Called on a normal return or a `BookingError`, and
        NEVER in a `finally`: a cancellation must leave `in_flight` set so the
        deadline handler can record the outcome as UNCERTAIN (plan risk R11)."""
        self.in_flight = None

    def record(self, outcome: BookingOutcome) -> None:
        """Keep the turn's one outcome, and update `state` for same-turn gates.

        A change PREPARED in this turn becomes the state with `action_id=None` and
        `confirmable=False`, which is what makes "hold, then book, in the same
        message" a `confirmation_needed` refusal rather than a booking the patient
        never confirmed (V3, hard rule 5).

        A PROPOSED failure leaves `state` alone: the service changed nothing, so
        whatever was prepared earlier is still the prepared change.
        """
        self.outcome = outcome
        if outcome.phase is ChangePhase.PROPOSED and outcome.status is ChangeStatus.FAILED:
            return
        status = {
            ChangeStatus.SUCCESS: BookingActionStatus.PENDING,
            ChangeStatus.UNCERTAIN: BookingActionStatus.UNCERTAIN,
            ChangeStatus.FAILED: BookingActionStatus.FAILED,
        }[outcome.status]
        if outcome.phase is ChangePhase.EXECUTED:
            status = {
                ChangeStatus.SUCCESS: BookingActionStatus.DONE,
                ChangeStatus.UNCERTAIN: BookingActionStatus.UNCERTAIN,
                ChangeStatus.FAILED: BookingActionStatus.FAILED,
            }[outcome.status]
        self.state = BookingState(
            action_id=outcome.action_id if outcome.phase is ChangePhase.EXECUTED else None,
            kind=outcome.kind,
            status=status,
            confirmable=False,
            hold_id=outcome.hold_id,
            appointment_id=outcome.appointment_id,
        )

    def __repr__(self) -> str:
        return f"PatientContext(changes={self.changes})"


class ToolContext:
    """What a tool may use, built by OUR code once per turn (hard rule 4).

    `tenant_id` comes from `Turn.tenant_id`, which came from the resolver, which
    came from the receiving phone_number_id. It is never an argument, never in a
    schema, and never reachable from anything the model wrote.

    A plain class with `__slots__` rather than a dataclass so it has no
    generated `__repr__` that could grow a field later; the one here prints the
    tenant (a clinic identifier, safe) and nothing else.

    VS-007 adds two KEYWORD-ONLY slots, so every existing
    `ToolContext(TENANT, booking, now)` keeps working:

    - `patient`: the patient side, or `None` for a turn with no booking wiring;
    - `remaining`: how many seconds of turn budget are left, read from the live
      `asyncio.timeout` deadline. `None` when there is no deadline (a direct tool
      test), which V15 treats as "no limit to check".
    """

    __slots__ = ("booking", "now", "patient", "remaining", "tenant_id")

    def __init__(
        self,
        tenant_id: TenantId,
        booking: BookingClient,
        now: datetime,
        *,
        patient: PatientContext | None = None,
        remaining: Callable[[], float] | None = None,
    ) -> None:
        if now.tzinfo is None:
            raise ValueError("ToolContext.now must be timezone-aware")
        self.tenant_id = tenant_id
        self.booking = booking
        # Aware UTC, read ONCE per turn from the injected clock. Every tool in
        # the turn sees the same instant, so two searches in one turn cannot
        # disagree about what "now" is.
        self.now = now
        self.patient = patient
        self.remaining = remaining

    def seconds_left(self) -> float | None:
        """The turn's remaining budget, or `None` when nothing is measuring it."""
        return None if self.remaining is None else self.remaining()

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
    # VS-007. True for the four tools that change something at the Booking
    # Service. The registry reads it to decide how a failure is recorded: an
    # UNKNOWN_OUTCOME from a CHANGE is `UNCERTAIN` (it may have happened), while
    # the same code reaching a read is downgraded to `booking_unavailable`,
    # because a read that failed changed nothing (V6's read/write rule).
    changes_bookings: bool

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
