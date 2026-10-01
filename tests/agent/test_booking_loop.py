"""The machinery a booking tool runs inside (VS-007 Task B1, plan sections 5.6-5.8).

No real booking tool yet: `StubChange` below is a test-only tool that reports
whatever outcome the test tells it to. That separation is deliberate - the rules
this file proves belong to the loop and the registry, not to any one tool, and a
test written against a real tool would prove them for that tool only.

The rules:

- **one change per patient message** (V12), and a refusal does not use one up;
- **no change is started with too little turn left** (V15), because a write cut
  off in flight costs a dead letter and a human check;
- **a change cut off by the deadline is UNCERTAIN and keeps its key** (V6), never
  ERROR - "it did not happen" is the one thing we cannot say;
- **the turn's ids and booking state never reach the model** (hard rules 4 and 8);
- **the patient side does not exist unless the job wired it**, and a booking tool
  that finds it missing crashes the turn rather than inventing an identity.
"""

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest

from app.agent.core import AgentRuntime, Turn, process_turn
from app.agent.loop import MAX_MODEL_CALLS, MAX_TOOL_CALLS_PER_TURN
from app.agent.tools import (
    MAX_BOOKING_CHANGES_PER_TURN,
    MIN_SECONDS_FOR_A_BOOKING_CHANGE,
    BookingOutcome,
    BookingState,
    ChangePhase,
    ChangeStatus,
    InFlightChange,
    NoArguments,
    ToolContext,
    ToolExecutionStatus,
    ToolFailure,
    ToolRegistry,
)
from app.db.enums import BookingActionKind, BookingActionStatus, MessageModality
from app.integrations.booking import BookingError
from app.integrations.booking.fake import FakeBookingClient
from app.integrations.booking.memory import InMemoryBookingService
from tests.integrations.booking_fakes import counter_ids
from tests.integrations.fakes import FakeChatClient, ok, tool_call, wants_tools

# Tuesday 29 September 2026, 10:00 Beirut, like every other clock in the suite.
NOW = dt.datetime(2026, 9, 29, 7, tzinfo=dt.UTC)
TENANT = "clinic-alpha"
# The patient reference is the patient's WhatsApp phone number (V14, as the
# developer overrode it). A synthetic MSISDN in a documentation-safe range, and
# the sentinel every leak test below searches for.
PHONE = "96170000001"
INBOX = uuid.UUID("11111111-2222-4333-8444-555555555555")
MESSAGE = uuid.UUID("22222222-3333-4444-8555-666666666666")
EARLIER_MESSAGE = uuid.UUID("33333333-4444-4555-8666-777777777777")


class StubChange:
    """A test-only booking tool. It does exactly what the test scripted.

    `outcome` is what it records when it gets that far; `raises` is a
    `BookingError` or a `ToolFailure` to inject; `gate` makes it check the
    incoming `BookingState` the way a real executing tool does.
    """

    name = "stub_change"
    description = "A test-only changing tool."
    args_model = NoArguments
    changes_bookings = True

    def __init__(
        self,
        outcome: BookingOutcome | None = None,
        *,
        raises: BaseException | None = None,
        gate: bool = False,
        hang: bool = False,
        key: str = "k" * 64,
    ) -> None:
        self.outcome = outcome
        self.raises = raises
        self.gate = gate
        self.hang = hang
        self.key = key
        self.calls = 0

    async def run(self, args: Any, ctx: ToolContext) -> dict[str, Any]:
        self.calls += 1
        patient = ctx.patient
        if patient is None:
            # A wiring bug, exactly as a real tool treats it.
            raise RuntimeError("PatientContextMissing")

        if self.gate:
            state = patient.state
            if state is None or state.status is not BookingActionStatus.PENDING:
                raise ToolFailure("nothing_to_confirm")
            if not state.confirmable:
                raise ToolFailure("confirmation_needed")

        patient.begin_change(ctx.seconds_left())

        phase = ChangePhase.EXECUTED if self.gate else ChangePhase.PROPOSED
        patient.begin_call(
            InFlightChange(kind=BookingActionKind.BOOK, phase=phase, idempotency_key=self.key)
        )
        try:
            if self.hang:
                await asyncio.Event().wait()  # until the deadline cuts it
            if self.raises is not None:
                raise self.raises
        except BookingError:
            # Cleared on a BookingError, NEVER in a finally: a cancellation must
            # leave it set so the deadline handler can record UNCERTAIN (R11).
            patient.end_call()
            raise
        patient.end_call()

        outcome = self.outcome or BookingOutcome(
            kind=BookingActionKind.BOOK,
            phase=phase,
            status=ChangeStatus.SUCCESS,
            idempotency_key=self.key,
            hold_id="hold_1",
        )
        patient.record(outcome)
        return {"status": "done"}


class StubRead:
    """A read-only tool that hangs, so a read cut by the deadline is testable."""

    name = "stub_read"
    description = "A test-only read."
    args_model = NoArguments
    changes_bookings = False

    def __init__(self, *, hang: bool = False, raises: BaseException | None = None) -> None:
        self.hang = hang
        self.raises = raises

    async def run(self, args: Any, ctx: ToolContext) -> dict[str, Any]:
        if self.hang:
            await asyncio.Event().wait()
        if self.raises is not None:
            raise self.raises
        return {"ok": True}


def registry(*tools: Any) -> ToolRegistry:
    return ToolRegistry(tools)


def service() -> InMemoryBookingService:
    """One per test: an `asyncio.Lock` binds to the loop it is first contended in."""
    return InMemoryBookingService(
        FakeBookingClient.demo(clock=lambda: NOW),
        lambda: NOW,
        id_secret=b"a fixed secret for the booking loop tests",
        new_id=counter_ids(),
    )


def turn(**overrides: Any) -> Turn:
    values: dict[str, Any] = {
        "tenant_id": TENANT,
        "contact_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "modality": MessageModality.TEXT,
        "input_text": "yes please",
        "inbox_event_id": INBOX,
        "inbound_message_id": MESSAGE,
        "patient_reference": PHONE,
    }
    values.update(overrides)
    return Turn(**values)


def runtime(tools: ToolRegistry, *, wired: bool = True, budget: float = 30.0) -> AgentRuntime:
    booking = service()
    return AgentRuntime(
        booking=booking,
        clock=lambda: NOW,
        turn_timeout_seconds=budget,
        registry=tools,
        patient_bookings=booking if wired else None,
    )


async def cut_by_the_deadline(tool: Any, *, seconds: float = 0.05) -> tuple[Any, Any]:
    """Run one hanging tool call under a REAL deadline, and return (state, patient).

    Why not `process_turn`: V15 refuses to start a change with less than eight
    seconds of budget left, so a `process_turn` test with a 0.05 s budget never
    reaches the write at all - it gets `turn_time_low`, which is a different rule.
    The situation V6 is about is a turn that had plenty of budget, spent most of it
    on model calls, and had the deadline fire while a write was on the wire.

    So the harness reports `remaining` as a comfortable 30 s (what the tool sees
    when it decides to start) while the real `asyncio.timeout` is 0.05 s (what
    fires while it waits). That is the same disagreement real life produces over
    forty seconds, without the test taking forty seconds.
    """
    from app.agent.core import _patient_context
    from app.agent.loop import LoopState, run_loop

    state = LoopState()
    the_turn = turn()
    the_runtime = runtime(registry(tool))
    patient = _patient_context(the_turn, the_runtime)
    chat = FakeChatClient(wants_tools(tool_call(tool.name, {})))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(seconds):
            ctx = ToolContext(
                TENANT,
                the_runtime.booking,
                NOW,
                patient=patient,
                remaining=lambda: 30.0,
            )
            await run_loop([], chat, the_runtime.registry, ctx, state)
    state.close_in_flight("turn_timeout", patient)
    return state, patient


def statuses(result: Any) -> list[str]:
    return [record.status.value for record in result.tool_calls]


def codes(result: Any) -> list[str | None]:
    return [record.error_code for record in result.tool_calls]


# --- the pinned limits -------------------------------------------------------


def test_min_seconds_for_a_booking_change_is_pinned():
    """V15. Raising it silently would make booking impossible on a short budget;
    lowering it would let a write start with no room to finish."""
    assert MIN_SECONDS_FOR_A_BOOKING_CHANGE == 8.0


def test_one_booking_change_per_turn_is_pinned():
    """V12. Every later step - the receipt, the recording, V11, the gate - handles
    ONE outcome per turn. Raising this makes each of them a list."""
    assert MAX_BOOKING_CHANGES_PER_TURN == 1


def test_the_loop_limits_are_the_vs007_numbers():
    assert MAX_MODEL_CALLS == 6
    assert MAX_TOOL_CALLS_PER_TURN == 12


# --- V12: one change per message ---------------------------------------------


async def test_a_second_booking_change_in_one_message_is_refused():
    """The model asked for two changes in one response. The first runs; the second
    is REFUSED without touching the Booking Service."""
    stub = StubChange()
    chat = FakeChatClient(
        wants_tools(
            tool_call("stub_change", {}, call_id="a"),
            tool_call("stub_change", {}, call_id="b"),
        ),
        ok("done"),
    )

    result = await process_turn(turn(), chat, runtime(registry(stub)))

    assert statuses(result) == ["OK", "REFUSED"]
    assert codes(result) == [None, "one_change_per_message"]
    assert stub.calls == 2  # both ran as far as begin_change; only one got past it
    # The refusal's message tells the model what to do, and names no value.
    second = chat.calls[-1]
    assert "Only one booking change can be made per patient message" in str(second[-1].content)


async def test_a_refused_change_does_not_use_up_the_messages_one_change():
    """A gate refusal costs nothing: the patient must still be able to have their
    one change in the same message, once the model calls the right tool."""
    gated = StubChange(gate=True)  # no PENDING state -> nothing_to_confirm
    allowed = StubChange()
    chat = FakeChatClient(
        wants_tools(tool_call("stub_change", {}, call_id="a")),
        wants_tools(tool_call("stub_hold", {}, call_id="b")),
        ok("done"),
    )
    allowed.name = "stub_hold"

    result = await process_turn(turn(), chat, runtime(registry(gated, allowed)))

    assert statuses(result) == ["REFUSED", "OK"]
    assert codes(result) == ["nothing_to_confirm", None]
    assert result.booking_outcome is not None


async def test_invalid_arguments_do_not_use_up_the_one_change():
    stub = StubChange()
    chat = FakeChatClient(
        wants_tools(tool_call("stub_change", {"invented": 1}, call_id="a")),
        wants_tools(tool_call("stub_change", {}, call_id="b")),
        ok("done"),
    )

    result = await process_turn(turn(), chat, runtime(registry(stub)))

    assert statuses(result) == ["INVALID_ARGUMENTS", "OK"]


# --- V15: the budget floor ---------------------------------------------------


async def test_a_change_is_not_started_with_too_little_turn_left():
    """A budget below the floor means no change can ever start.

    The patient gets "send your confirmation again", which costs one message -
    against an unknown outcome, a dead letter and a human check.
    """
    stub = StubChange()
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})), ok("done"))

    result = await process_turn(
        turn(), chat, runtime(registry(stub), budget=MIN_SECONDS_FOR_A_BOOKING_CHANGE / 2)
    )

    assert statuses(result) == ["REFUSED"]
    assert codes(result) == ["turn_time_low"]
    assert result.booking_outcome is None
    assert "not enough time left" in str(chat.calls[-1][-1].content)


async def test_a_change_starts_normally_with_a_full_budget():
    stub = StubChange()
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})), ok("done"))

    result = await process_turn(turn(), chat, runtime(registry(stub), budget=45.0))

    assert statuses(result) == ["OK"]


async def test_the_one_change_rule_is_checked_before_the_budget():
    """V12 first, then V15 (plan section 5.6). Otherwise the answer to "can I make
    a second change?" would depend on how fast the model happened to be."""
    from app.agent.tools.base import PatientContext
    from app.integrations.booking import PatientRef

    patient = PatientContext(
        bookings=service(),
        patient=PatientRef(PHONE),
        inbox_event_id=INBOX,
        inbound_message_id=MESSAGE,
    )
    patient.begin_change(60.0)

    with pytest.raises(ToolFailure) as raised:
        patient.begin_change(0.0)  # both rules broken at once

    assert raised.value.code == "one_change_per_message"


# --- V6: the deadline, and what a cut write means ----------------------------


async def test_a_change_cut_by_the_turn_deadline_is_uncertain_and_keeps_its_key():
    """The whole of V6 in one test.

    The write is on the wire when the deadline fires. It MAY have been applied, so
    the record is UNCERTAIN rather than ERROR, and the outcome carries the
    idempotency key - the only thing that lets a human find the request at the
    Booking Service afterwards.
    """
    state, patient = await cut_by_the_deadline(StubChange(hang=True, key="f" * 64))

    assert [record.status.value for record in state.records] == ["UNCERTAIN"]
    assert [record.error_code for record in state.records] == ["turn_timeout"]
    outcome = patient.outcome
    assert outcome is not None
    assert outcome.status is ChangeStatus.UNCERTAIN
    assert outcome.idempotency_key == "f" * 64
    assert outcome.kind is BookingActionKind.BOOK
    assert outcome.phase is ChangePhase.PROPOSED


async def test_a_turn_whose_deadline_fires_is_retryable_and_keeps_its_records():
    """The `process_turn` half: the records and any outcome reach the job on the
    retry path too, which is what V9's T1r records."""
    chat = FakeChatClient(wants_tools(tool_call("stub_read", {})))

    result = await process_turn(turn(), chat, runtime(registry(StubRead(hang=True)), budget=0.05))

    assert result.outcome.value == "RETRYABLE"
    assert result.reason == "agent_turn_timeout"
    assert len(result.tool_calls) == 1


async def test_a_read_cut_by_the_turn_deadline_is_still_an_error():
    """VS-006's behaviour, kept. A read that was cut changed nothing, so ERROR is
    the truth and UNCERTAIN would be alarming noise."""
    chat = FakeChatClient(wants_tools(tool_call("stub_read", {})))

    result = await process_turn(turn(), chat, runtime(registry(StubRead(hang=True)), budget=0.05))

    assert statuses(result) == ["ERROR"]
    assert codes(result) == ["turn_timeout"]
    assert result.booking_outcome is None


async def test_the_in_flight_change_survives_cancellation():
    """Plan risk R11, at the level it actually matters.

    `in_flight` is set before the await and cleared only on a normal return or a
    `BookingError`. Clearing it in a `finally` would clear it on cancellation too,
    and the deadline handler would then have nothing to record - the change would
    be invisible, and an applied booking would leave no trace anywhere.
    """
    state, patient = await cut_by_the_deadline(StubChange(hang=True))

    # A record exists at all, which is only possible if in_flight survived.
    assert len(state.records) == 1
    assert patient.outcome is not None
    # And it has been cleared by now, so nothing can record it twice.
    assert patient.in_flight is None


async def test_close_in_flight_does_nothing_when_no_tool_was_running():
    """The deadline can fire between tool calls, with nothing to attribute it to."""
    from app.agent.loop import LoopState

    state = LoopState()
    state.close_in_flight("turn_timeout", None)

    assert state.records == []


async def test_a_booking_error_clears_the_in_flight_change():
    """The other half: a call that was ANSWERED must not look cut off."""
    stub = StubChange(raises=BookingError("SLOT_TAKEN"))
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})), ok("sorry"))

    result = await process_turn(turn(), chat, runtime(registry(stub)))

    assert statuses(result) == ["ERROR"]
    assert codes(result) == ["booking_slot_taken"]


# --- the error mapping -------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "nothing_to_confirm",
        "confirmation_needed",
        "one_change_per_message",
        "turn_time_low",
        "hold_expired",
    ],
)
async def test_tool_failures_map_to_refused_with_their_fixed_message(code):
    """Every `ToolFailure` is REFUSED, with its code as the error code and its
    message straight from the table. Nothing is built from a value."""
    from app.agent.tools.errors import BOOKING_MESSAGES

    stub = StubChange(raises=ToolFailure(code))
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})), ok("ok"))

    result = await process_turn(turn(), chat, runtime(registry(stub)))

    assert statuses(result) == ["REFUSED"]
    assert codes(result) == [code]
    content = str(chat.calls[-1][-1].content)
    assert BOOKING_MESSAGES[code] in content
    assert code in content


def test_an_unknown_tool_failure_code_cannot_be_raised():
    """The table is the vocabulary: a typo is a loud ValueError here rather than a
    tool message with an empty body."""
    with pytest.raises(ValueError):
        ToolFailure("not-a-code")


@pytest.mark.parametrize(
    ("booking_code", "expected_code", "expected_status", "expected_error"),
    [
        ("SLOT_TAKEN", "slot_taken", "ERROR", "booking_slot_taken"),
        ("HOLD_EXPIRED", "hold_expired", "ERROR", "booking_hold_expired"),
        ("NOT_FOUND", "appointment_not_found", "ERROR", "booking_not_found"),
        ("VALIDATION", "booking_validation", "ERROR", "booking_validation"),
        ("UNAVAILABLE", "booking_unavailable", "ERROR", "booking_unavailable"),
        ("UNKNOWN_OUTCOME", "outcome_unknown", "UNCERTAIN", "booking_unknown_outcome"),
        (
            "IDEMPOTENCY_CONFLICT",
            "outcome_unknown",
            "UNCERTAIN",
            "booking_idempotency_conflict",
        ),
    ],
)
async def test_unknown_outcome_is_uncertain_from_a_changing_tool_and_unavailable_from_a_read(
    booking_code, expected_code, expected_status, expected_error
):
    """V6's read/write rule, as the registry applies it.

    From a CHANGE, `UNKNOWN_OUTCOME` and `IDEMPOTENCY_CONFLICT` are UNCERTAIN: the
    write may have been applied. From a READ the same code is downgraded to
    `booking_unavailable`, because a read that failed changed nothing and an
    "unknown outcome" there would be a bug in the client, not news for a patient.
    """
    stub = StubChange(raises=BookingError(booking_code))
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})), ok("ok"))

    result = await process_turn(turn(), chat, runtime(registry(stub)))

    assert statuses(result) == [expected_status]
    assert codes(result) == [expected_error]
    assert expected_code in str(chat.calls[-1][-1].content)


@pytest.mark.parametrize("booking_code", ["UNKNOWN_OUTCOME", "IDEMPOTENCY_CONFLICT"])
async def test_an_unknown_outcome_reaching_a_read_tool_is_unavailable(booking_code):
    chat = FakeChatClient(wants_tools(tool_call("stub_read", {})), ok("ok"))
    read = StubRead(raises=BookingError(booking_code))

    result = await process_turn(turn(), chat, runtime(registry(read)))

    assert statuses(result) == ["ERROR"]
    assert codes(result) == [f"booking_{booking_code.lower()}"]
    assert "booking_unavailable" in str(chat.calls[-1][-1].content)


async def test_field_aware_problems_leave_the_window_messages_unchanged():
    """The `(type, field)` table is consulted first, and only for OUR fields.

    `search_available_slots`'s `start` keeps VS-006's window message; a `slot_id`
    with the same Pydantic error type gets id advice instead.
    """
    from app.agent.tools import SearchAvailableSlots
    from app.agent.tools.errors import problems_from

    window = problems_from(
        [{"type": "string_pattern_mismatch", "loc": ("start",)}], ("doctor_id", "end", "start")
    )
    assert window == [
        {
            "argument": "start",
            "problem": ("must be clinic local time written YYYY-MM-DDTHH:MM, with no UTC offset"),
        }
    ]

    slot = problems_from([{"type": "string_pattern_mismatch", "loc": ("slot_id",)}], ("slot_id",))
    assert slot == [
        {
            "argument": "slot_id",
            "problem": "must be a slot_id copied exactly from search_available_slots",
        }
    ]

    # An undeclared field never reaches the field-aware table either.
    undeclared = problems_from(
        [{"type": "string_pattern_mismatch", "loc": ("slot_id",)}], ("start",)
    )
    assert undeclared == [
        {
            "argument": None,
            "problem": ("must be clinic local time written YYYY-MM-DDTHH:MM, with no UTC offset"),
        }
    ]
    assert SearchAvailableSlots().changes_bookings is False


@pytest.mark.parametrize(
    ("error_type", "problem"),
    [
        ("string_too_short", "must be the patient's full name"),
        ("name_too_short", "must be the patient's full name"),
        ("name_has_digits", "must be a person's name, with no digits"),
        ("name_not_printable", "must be a person's name, with no hidden characters"),
    ],
)
def test_the_name_problems_say_what_a_name_is(error_type, problem):
    from app.agent.tools.errors import problems_from

    assert problems_from([{"type": error_type, "loc": ("full_name",)}], ("full_name",)) == [
        {"argument": "full_name", "problem": problem}
    ]


# --- wiring, identity and leaks ----------------------------------------------


async def test_the_patient_context_exists_only_when_wired():
    """Without `patient_bookings` there is no patient side, and a booking tool
    crashes the turn PERMANENTLY with a dead letter - rather than booking for a
    guessed identity (hard rule 4)."""
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})))

    result = await process_turn(turn(), chat, runtime(registry(StubChange()), wired=False))

    assert result.outcome.value == "PERMANENT"
    assert result.reason == "agent_tool_crashed"
    assert result.booking_outcome is None


@pytest.mark.parametrize("missing", ["inbox_event_id", "inbound_message_id", "patient_reference"])
async def test_a_turn_missing_any_ingredient_has_no_patient_side(missing):
    """All four ingredients or none. A half-wired turn would book without knowing
    which message confirmed it, or without a key that survives a retry."""
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})))

    result = await process_turn(turn(**{missing: None}), chat, runtime(registry(StubChange())))

    assert result.reason == "agent_tool_crashed"


async def test_an_unusable_patient_reference_has_no_patient_side():
    """`PatientRef` refuses an unprintable value. Rather than letting that escape
    `process_turn` - arq would fail the job with no dead letter - the turn runs
    with no patient side, so a booking tool produces the loud dead letter."""
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})))

    result = await process_turn(
        turn(patient_reference="9617\n0000001"), chat, runtime(registry(StubChange()))
    )

    assert result.reason == "agent_tool_crashed"


async def test_the_patient_reference_is_the_contacts_phone_number():
    """V14, as the developer overrode it: the Booking Service asked for the phone
    number, not our contact UUID.

    It reaches the service as a `PatientRef`, and appears nowhere else - not in a
    message to the model, not in a tool spec, not in a result, and not in a repr.
    """
    seen: list[str] = []

    class Spy(StubChange):
        async def run(self, args: Any, ctx: ToolContext) -> dict[str, Any]:
            seen.append(ctx.patient.patient.value)
            return await super().run(args, ctx)

    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})), ok("done"))
    the_turn = turn()

    result = await process_turn(the_turn, chat, runtime(registry(Spy())))

    assert seen == [PHONE]
    # Nowhere else. Including the Turn's own repr, which a traceback would print.
    assert PHONE not in repr(the_turn)
    assert PHONE not in repr(result)
    assert PHONE not in repr(result.booking_outcome)
    for messages in chat.calls:
        for message in messages:
            assert PHONE not in str(message.content or "")
    for specs in chat.tool_specs:
        assert PHONE not in str(specs)


async def test_the_turn_ids_and_the_booking_state_never_reach_the_model():
    """Hard rules 4 and 8. The gate ids, the inbox row and a `hold_id` are ours;
    the model gets a question, the clock, and some tool results."""
    state = BookingState(
        action_id=uuid.UUID("44444444-5555-4666-8777-888888888888"),
        kind=BookingActionKind.BOOK,
        status=BookingActionStatus.PENDING,
        confirmable=True,
        hold_id="hold_SENTINEL",
        appointment_id="apt_SENTINEL",
    )
    chat = FakeChatClient(wants_tools(tool_call("stub_read", {})), ok("done"))

    the_turn = turn(booking_state=state)
    await process_turn(the_turn, chat, runtime(registry(StubRead())))

    seen_by_the_model = "".join(
        str(message.content or "") for messages in chat.calls for message in messages
    ) + str(chat.tool_specs)
    for sentinel in (
        str(INBOX),
        str(MESSAGE),
        str(state.action_id),
        "hold_SENTINEL",
        "apt_SENTINEL",
        TENANT,
        PHONE,
        str(the_turn.contact_id),
        str(the_turn.conversation_id),
    ):
        assert sentinel not in seen_by_the_model, sentinel


def test_no_repr_shows_the_phone_number_or_a_service_id():
    """What a traceback or an error tracker would print.

    OUR OWN row ids stay visible on purpose: `inbox_event_id` is the `event_id=`
    every log line already carries, and being able to read it out of a traceback
    is the point of it. What must never appear is the patient's phone number, the
    patient's words, and the Booking Service's own ids and keys (section 5.13).
    """
    state = BookingState(
        action_id=uuid.uuid4(),
        kind=BookingActionKind.BOOK,
        status=BookingActionStatus.PENDING,
        confirmable=True,
        hold_id="hold_SENTINEL",
        appointment_id="apt_SENTINEL",
    )
    the_turn = turn(booking_state=state)
    outcome = BookingOutcome(
        kind=BookingActionKind.BOOK,
        phase=ChangePhase.EXECUTED,
        status=ChangeStatus.SUCCESS,
        hold_id="hold_SENTINEL",
        appointment_id="apt_SENTINEL",
        idempotency_key="key_SENTINEL",
        receipt="Dr. SENTINEL - 2026-09-30 14:00 - #SENTINEL",
    )

    for text in (repr(the_turn), repr(state), repr(outcome)):
        assert "SENTINEL" not in text, text
    assert PHONE not in repr(the_turn)
    assert "yes please" not in repr(the_turn)
    # Our own ids ARE readable, deliberately.
    assert str(INBOX) in repr(the_turn)


async def test_process_turn_returns_the_outcome_as_plain_data():
    """`app/agent/` cannot open a transaction, so the outcome goes back to the job
    as plain data for T1b or T1r to record."""
    outcome = BookingOutcome(
        kind=BookingActionKind.CANCEL,
        phase=ChangePhase.EXECUTED,
        status=ChangeStatus.SUCCESS,
        action_id=uuid.UUID("55555555-6666-4777-8888-999999999999"),
        appointment_id="apt_1",
        idempotency_key="a" * 64,
        confirmed=True,
        receipt="❌ Dr. Karim Haddad · 2026-09-30 14:00 · #K7Q2M9",
    )
    stub = StubChange(outcome)
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})), ok("cancelled"))

    result = await process_turn(turn(), chat, runtime(registry(stub)))

    assert result.booking_outcome == outcome
    assert result.reply_text == "cancelled"


async def test_a_change_made_this_turn_is_not_confirmable_in_the_same_turn():
    """The same-turn half of the gate (V3). "Hold, then book, in one message" is
    refused, because the patient never saw the details they are confirming."""
    hold = StubChange()
    book = StubChange(gate=True)
    book.name = "stub_book"
    chat = FakeChatClient(
        wants_tools(tool_call("stub_change", {}, call_id="a")),
        wants_tools(tool_call("stub_book", {}, call_id="b")),
        ok("done"),
    )

    result = await process_turn(turn(), chat, runtime(registry(hold, book)))

    assert statuses(result) == ["OK", "REFUSED"]
    assert codes(result) == [None, "confirmation_needed"]


async def test_a_confirmable_state_from_t1_lets_an_executing_tool_run():
    """The other side of the same gate: T1 said a reply went out in between."""
    book = StubChange(gate=True)
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})), ok("booked"))
    state = BookingState(
        action_id=uuid.uuid4(),
        kind=BookingActionKind.BOOK,
        status=BookingActionStatus.PENDING,
        confirmable=True,
        hold_id="hold_1",
    )

    result = await process_turn(turn(booking_state=state), chat, runtime(registry(book)))

    assert statuses(result) == ["OK"]
    assert result.booking_outcome is not None
    assert result.booking_outcome.phase is ChangePhase.EXECUTED


def test_the_patient_context_repr_shows_one_integer():
    """It holds a `PatientRef`, hold ids and a key. A traceback gets a count."""
    from app.agent.tools.base import PatientContext
    from app.integrations.booking import PatientRef

    patient = PatientContext(
        bookings=service(),
        patient=PatientRef(PHONE),
        inbox_event_id=INBOX,
        inbound_message_id=MESSAGE,
    )

    assert repr(patient) == "PatientContext(changes=0)"
    assert PHONE not in repr(patient)


def test_the_tool_context_repr_still_shows_only_the_tenant():
    """Unchanged by the two new keyword-only slots (a VS-006 pin, restated here
    because this task is what could have broken it)."""
    from app.agent.tools.base import PatientContext
    from app.integrations.booking import PatientRef

    ctx = ToolContext(
        TENANT,
        service(),
        NOW,
        patient=PatientContext(
            bookings=service(),
            patient=PatientRef(PHONE),
            inbox_event_id=INBOX,
            inbound_message_id=MESSAGE,
        ),
        remaining=lambda: 30.0,
    )

    assert repr(ctx) == "ToolContext(tenant_id='clinic-alpha')"
    assert ctx.seconds_left() == 30.0


def test_seconds_left_is_none_without_a_deadline():
    """A direct tool test has no `asyncio.timeout`, and V15 treats that as "no
    limit to check" rather than refusing every change."""
    assert ToolContext(TENANT, service(), NOW).seconds_left() is None


# --- the test harness itself -------------------------------------------------


async def test_the_fake_chat_client_accepts_a_callable_step():
    """What a real booking script needs: the second step reads the `slot_id` the
    search result just produced, which no test can know in advance."""
    from tests.integrations.fakes import last_tool_result

    seen: list[dict] = []

    def then(messages):
        seen.append(last_tool_result(messages))
        return ok("read it")

    chat = FakeChatClient(wants_tools(tool_call("stub_read", {})), then)

    result = await process_turn(turn(), chat, runtime(registry(StubRead())))

    assert result.reply_text == "read it"
    assert seen == [{"ok": True}]


async def test_a_callable_step_may_be_async():
    async def then(messages):
        await asyncio.sleep(0)
        return ok("async step")

    chat = FakeChatClient(then)

    result = await process_turn(turn(), chat, runtime(registry(StubRead())))

    assert result.reply_text == "async step"


def test_the_record_statuses_cover_every_database_value():
    """A record status the CHECK constraint does not know is a row PostgreSQL
    rejects inside a job. The equality test in test_tool_registry.py pins the two
    enums; this pins that the two new ones are reachable from here."""
    assert ToolExecutionStatus.UNCERTAIN.value == "UNCERTAIN"
    assert ToolExecutionStatus.REFUSED.value == "REFUSED"
