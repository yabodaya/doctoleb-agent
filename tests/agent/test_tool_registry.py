"""The gate: what the model may ask for, and what happens when it asks wrongly.

No database and no network. Every test builds a registry, hands it one
`ToolCallRequest` - the untrusted thing - and reads back the `tool` message and
the record.

The registry is where hard rule 3 lives, so these tests are mostly about what
does NOT happen: no tenant in a schema, no model-written text in an error, no
undeclared name in a record, no argument value anywhere.
"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, ConfigDict

from app.agent.tools import (
    UNKNOWN_TOOL_NAME,
    ListDoctors,
    NoArguments,
    ToolContext,
    ToolCrashed,
    ToolExecutionStatus,
    ToolRegistry,
    default_registry,
)
from app.integrations.booking import BookingError
from app.integrations.booking.fake import FakeBookingClient
from app.integrations.openai import ToolCallRequest
from tests.integrations.booking_fakes import RecordingBooking

NOW = datetime(2026, 9, 29, 7, tzinfo=UTC)  # Tuesday 10:00 clinic local
TENANT = "clinic-alpha"


def context(booking=None) -> ToolContext:
    return ToolContext(TENANT, booking or FakeBookingClient.demo(clock=lambda: NOW), NOW)


def request(name: str, arguments: str | dict, call_id: str = "call_1") -> ToolCallRequest:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCallRequest(call_id, name, raw)


async def run(name: str, arguments: str | dict, *, booking=None, registry=None):
    registry = registry or default_registry()
    content, record = await registry.execute(
        request(name, arguments), context(booking), sequence=0, model_call=1
    )
    return json.loads(content), record


# --------------------------------------------------------------------------
# The schemas: what the model is shown
# --------------------------------------------------------------------------


def test_the_registry_holds_exactly_the_three_read_only_tools():
    """The slice fixes the set at three. A fourth appearing here means either
    scope creep or VS-007 arriving early, and both deserve a failing test."""
    assert default_registry().names == (
        "get_clinic_information",
        "list_doctors",
        "search_available_slots",
    )


def test_no_tool_schema_mentions_a_tenant():
    """Hard rule 4, as a grep over everything the model is sent.

    If the tenant were a tool argument, a patient could talk the model into
    asking for another clinic's data. It is not in a schema, not in a
    description, and not in a property name - checked over the whole rendered
    spec rather than field by field, so a future field cannot slip in.
    """
    rendered = json.dumps([spec.__dict__ for spec in default_registry().specs()]).lower()

    assert "tenant" not in rendered


def test_no_tool_has_an_id_argument_the_backend_owns():
    """The tenant is not the only id the model must never supply.

    contact_id and conversation_id are ours; a `patient_*` argument would invite
    the model to invent an identity for somebody.
    """
    forbidden = ("tenant_id", "contact_id", "conversation_id", "inbox_event_id")
    for spec in default_registry().specs():
        properties = spec.parameters.get("properties", {})
        for name in properties:
            assert name not in forbidden, f"{spec.name}.{name}"
            assert not name.startswith("patient"), f"{spec.name}.{name}"


def test_every_args_model_forbids_extra_arguments():
    """The mechanism that makes an invented `tenant_id` a REPORTED error rather
    than a silently dropped field."""
    for spec in default_registry().specs():
        assert spec.parameters.get("additionalProperties") is False, spec.name


def test_tool_names_are_valid_function_names():
    """OpenAI's rule for a function name. A name it rejects is a 400 on every
    single model call, not a degraded answer."""
    import re

    from app.agent.tools import TOOL_NAME_PATTERN

    for spec in default_registry().specs():
        assert re.fullmatch(TOOL_NAME_PATTERN, spec.name), spec.name


def test_schemas_carry_no_pydantic_titles():
    """`model_json_schema()` puts a `title` on every property and on the model
    (plan check U5). They are tokens paid for on EVERY model call of every turn,
    and the model-level description would compete with the tool's own."""
    rendered = json.dumps([dict(spec.parameters) for spec in default_registry().specs()])

    assert "title" not in rendered
    assert "$schema" not in rendered


def test_every_tool_has_a_description_that_says_what_it_returns():
    for spec in default_registry().specs():
        assert len(spec.description) > 40, spec.name
        assert spec.description.endswith("."), spec.name


def test_all_three_periods_are_defined_in_the_search_description():
    """Decision D3 with amendment B2.

    Morning, afternoon and evening are all defined, in the description, where
    the model reads them while writing the call. Leaving two of them undefined
    would make the model choose a window - and a different one each time, so the
    same question would get different answers on different days.
    """
    description = next(
        spec.description
        for spec in default_registry().specs()
        if spec.name == "search_available_slots"
    )

    assert "morning, search from 08:00 to 12:00" in description
    assert "afternoon, from 12:00 to 17:00" in description
    assert "evening, from 17:00 to 21:00" in description


def test_the_search_schema_steers_the_model_to_clinic_local_time():
    """The pattern goes INTO the schema, so it steers before the mistake."""
    spec = next(s for s in default_registry().specs() if s.name == "search_available_slots")
    properties = spec.parameters["properties"]

    assert properties["start"]["pattern"] == properties["end"]["pattern"]
    assert "+" not in properties["start"]["pattern"]  # no offset accepted
    assert set(spec.parameters["required"]) == {"doctor_id", "start", "end"}


# --------------------------------------------------------------------------
# Construction rules
# --------------------------------------------------------------------------


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _LooseTool:
    name = "loose"
    description = "d"
    args_model = _Loose

    async def run(self, args, ctx):
        return {}


class _NamedTool:
    description = "d"
    args_model = NoArguments

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, args, ctx):
        return {}


def test_a_tool_whose_arguments_are_not_forbidden_is_refused_at_construction():
    with pytest.raises(ValueError, match="extra='forbid'"):
        ToolRegistry((_LooseTool(),))


def test_a_duplicate_or_invalid_or_reserved_tool_name_is_refused():
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry((ListDoctors(), ListDoctors()))
    with pytest.raises(ValueError, match="invalid tool name"):
        ToolRegistry((_NamedTool("not a valid name"),))
    # "unknown" is the sentinel stored for a tool the model invented, so a real
    # tool may not take it: a row reading "unknown" must mean one thing only.
    with pytest.raises(ValueError, match="reserved"):
        ToolRegistry((_NamedTool(UNKNOWN_TOOL_NAME),))


# --------------------------------------------------------------------------
# Execution: everything the model can get wrong
# --------------------------------------------------------------------------


async def test_an_unknown_tool_is_reported_and_recorded_as_unknown():
    """The name the model sent is model-written text and could quote a patient.

    It is answered - the model needs to know - but it is never echoed in the
    message and never stored (Q9).
    """
    payload, record = await run("SENTINELNAME_delete_everything", {})

    assert payload["error"]["code"] == "unknown_tool"
    assert "SENTINELNAME" not in json.dumps(payload)
    assert "list_doctors" in payload["error"]["message"]  # told what DOES exist
    assert record.tool_name == UNKNOWN_TOOL_NAME
    assert record.status is ToolExecutionStatus.UNKNOWN_TOOL
    assert record.error_code == "unknown_tool"
    assert record.argument_names == ()
    assert "SENTINELNAME" not in repr(record)


@pytest.mark.parametrize(
    ("arguments", "label"),
    [
        ("{not json", "not json at all"),
        ("[1,2]", "a JSON array"),
        ("null", "a JSON null"),
        ('"a string"', "a JSON string"),
        ("[" * 5000, "deeply nested: RecursionError, not ValueError"),
    ],
)
async def test_arguments_that_are_not_a_json_object_are_reported(arguments, label):
    """`json.loads` raises ValueError for most of these and RecursionError for
    the deeply nested one. An unhandled RecursionError would end the turn on
    something the model could simply be told to fix."""
    payload, record = await run("list_doctors", arguments)

    assert payload["error"]["code"] == "invalid_json", label
    assert record.status is ToolExecutionStatus.INVALID_ARGUMENTS
    assert record.error_code == "invalid_json"
    assert record.argument_names == ()


@pytest.mark.parametrize(
    ("arguments", "problem"),
    [
        pytest.param(
            {"start": "2026-09-30T12:00", "end": "2026-09-30T17:00"}, "is required", id="missing"
        ),
        pytest.param(
            {
                "doctor_id": "doc_karim",
                "start": "2026-09-30T12:00+03:00",
                "end": "2026-09-30T17:00",
            },
            "must be clinic local time written YYYY-MM-DDTHH:MM, with no UTC offset",
            id="offset-sent",
        ),
        pytest.param(
            {"doctor_id": "doc_karim", "start": 1730000000, "end": "2026-09-30T17:00"},
            "must be a string",
            id="unix-timestamp",
        ),
        pytest.param(
            {"doctor_id": "", "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
            "must not be empty",
            id="blank-doctor",
        ),
        pytest.param(
            {"doctor_id": "d" * 65, "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
            "is too long",
            id="long-doctor",
        ),
        pytest.param(
            {"doctor_id": "doc_karim", "start": "2026-02-30T12:00", "end": "2026-03-01T17:00"},
            "start or end is not a real date and time",
            id="not-a-real-date",
        ),
        pytest.param(
            {"doctor_id": "doc_karim", "start": "2026-09-30T17:00", "end": "2026-09-30T12:00"},
            "end must be after start",
            id="reversed",
        ),
        pytest.param(
            {"doctor_id": "doc_karim", "start": "2026-09-30T12:00", "end": "2026-10-30T12:00"},
            "the search window can be at most 14 days",
            id="too-long",
        ),
        pytest.param(
            {"doctor_id": "doc_karim", "start": "2026-09-01T12:00", "end": "2026-09-02T17:00"},
            "the whole window is in the past; use the current date and time you were given",
            id="in-the-past",
        ),
        pytest.param(
            {"doctor_id": "doc_karim", "start": "2027-09-30T12:00", "end": "2027-09-30T17:00"},
            "start can be at most 90 days from today",
            id="too-far-ahead",
        ),
        pytest.param(
            {
                "doctor_id": "doc_karim",
                "start": "2026-09-30T12:00",
                "end": "2026-09-30T17:00",
                "extra": 1,
            },
            "unexpected arguments are not allowed; the allowed arguments are: "
            "doctor_id, end, start",
            id="extra-argument",
        ),
    ],
)
async def test_invalid_arguments_are_rejected_and_the_model_is_told_why(arguments, problem):
    """Every row of plan section 5.6's table.

    The message says what to FIX, not what went wrong: the model gets one more
    model call to act on it, and "is not valid" would waste it.
    """
    payload, record = await run("search_available_slots", arguments)

    assert payload["error"]["code"] == "invalid_arguments"
    assert "search_available_slots" in payload["error"]["message"]
    assert problem in [entry["problem"] for entry in payload["error"]["problems"]]
    assert record.status is ToolExecutionStatus.INVALID_ARGUMENTS
    assert record.error_code == "invalid_arguments"


async def test_a_tenant_id_argument_is_rejected_and_never_reaches_the_booking_client():
    """Hard rule 4's sharpest case: the model invents a tenant.

    `extra="forbid"` refuses it, the booking client is never called, and the
    key the model wrote is not echoed back - so a patient cannot even learn
    whether a tenant they guessed exists.
    """
    spy = RecordingBooking(FakeBookingClient.demo(clock=lambda: NOW))

    payload, record = await run(
        "search_available_slots",
        {
            "doctor_id": "doc_karim",
            "start": "2026-09-30T12:00",
            "end": "2026-09-30T17:00",
            "tenant_id": "clinic-SENTINEL-other",
        },
        booking=spy,
    )

    assert payload["error"]["code"] == "invalid_arguments"
    assert spy.calls == []
    rendered = json.dumps(payload)
    assert "SENTINEL" not in rendered
    assert "tenant_id" not in rendered  # not even the KEY is echoed
    assert record.argument_names == ("doctor_id", "end", "start")


async def test_no_error_ever_echoes_what_the_model_sent():
    """`str(ValidationError)` quotes the input, confirmed with a sentinel in
    plan check U5. This is the test that says we never use it.

    Sentinels go in both a VALUE and an undeclared KEY, because
    `extra_forbidden`'s `loc` IS the key the model wrote.
    """
    payload, record = await run(
        "search_available_slots",
        {
            "doctor_id": "SENTINELVALUE-my-knee-hurts",
            "start": "not-a-time-SENTINELVALUE",
            "end": "2026-09-30T17:00",
            "SENTINELKEY_note": "SENTINELVALUE-again",
        },
    )

    rendered = json.dumps(payload)
    assert "SENTINEL" not in rendered, rendered
    assert "SENTINEL" not in repr(record)
    assert "SENTINEL" not in str(record.argument_names)


async def test_only_declared_argument_names_are_recorded():
    """An undeclared key is model-written; a declared one is ours. Only ours is
    stored, and only when it was actually present."""
    _, record = await run(
        "search_available_slots",
        {"doctor_id": "doc_karim", "SENTINELKEY": 1, "start": "2026-09-30T12:00"},
    )

    assert record.argument_names == ("doctor_id", "start")  # no "end", no SENTINELKEY


async def test_a_successful_call_records_the_names_it_was_given():
    _, record = await run(
        "search_available_slots",
        {"doctor_id": "doc_karim", "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
    )

    assert record.status is ToolExecutionStatus.OK
    assert record.error_code is None
    assert record.argument_names == ("doctor_id", "end", "start")
    assert record.duration_ms >= 0


async def test_list_doctors_rejects_arguments_it_does_not_take():
    """`NoArguments` is still `extra="forbid"`: a model that sends
    `{"doctor": "Karim"}` must be told, not silently obeyed."""
    payload, record = await run("list_doctors", {"doctor": "Karim"})

    assert payload["error"]["code"] == "invalid_arguments"
    assert record.tool_name == "list_doctors"
    assert record.argument_names == ()


@pytest.mark.parametrize(
    ("code", "expected_code", "expected_error"),
    [
        ("NOT_FOUND", "doctor_not_found", "booking_not_found"),
        ("VALIDATION", "booking_validation", "booking_validation"),
        ("UNAVAILABLE", "booking_unavailable", "booking_unavailable"),
    ],
)
async def test_booking_errors_become_fixed_tool_errors(code, expected_code, expected_error):
    """The contract's error body carries a `message`; it is never read.

    UNAVAILABLE's text tells the model NOT to guess, which is the whole point:
    the alternative to a real answer is an honest one, never an invented one
    (hard rule 5's neighbourhood).
    """
    spy = RecordingBooking(FakeBookingClient.demo(clock=lambda: NOW), raises=BookingError(code))

    payload, record = await run(
        "search_available_slots",
        {"doctor_id": "doc_karim", "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
        booking=spy,
    )

    assert payload["error"]["code"] == expected_code
    assert record.status is ToolExecutionStatus.ERROR
    assert record.error_code == expected_error
    if code == "UNAVAILABLE":
        assert "Do not guess" in payload["error"]["message"]


async def test_an_unknown_doctor_is_told_how_to_get_a_real_id():
    """NOT_FOUND from the search means one specific, fixable thing, so the
    message names the fix rather than describing the failure."""
    payload, _ = await run(
        "search_available_slots",
        {"doctor_id": "doc_nobody", "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
    )

    assert payload["error"]["code"] == "doctor_not_found"
    assert "Call list_doctors" in payload["error"]["message"]


async def test_a_crashing_tool_raises_tool_crashed_with_the_class_name_only():
    """Q6. A bug in our code is not something the model can fix by calling
    again, so it ends the turn - but it ends it as OUR exception type, carrying
    the class name and never the message (hard rule 8)."""

    class Boom(RuntimeError):
        pass

    spy = RecordingBooking(
        FakeBookingClient.demo(clock=lambda: NOW), raises=Boom("SENTINEL-internal-detail")
    )

    with pytest.raises(ToolCrashed) as raised:
        await run("list_doctors", {}, booking=spy)

    assert raised.value.tool_name == "list_doctors"
    assert raised.value.error_class == "Boom"
    assert "SENTINEL" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


async def test_a_skipped_call_is_recorded_without_running_anything():
    """A call the loop declines to run still gets a `tool` message, because
    OpenAI requires one per tool_call_id - and still gets a row, because it was
    something the model asked for and paid for."""
    spy = RecordingBooking(FakeBookingClient.demo(clock=lambda: NOW))
    registry = default_registry()

    content, record = registry.skipped(
        request("list_doctors", {}), sequence=3, model_call=4, reason="max_model_calls"
    )

    assert json.loads(content)["error"]["code"] == "not_run"
    assert record.status is ToolExecutionStatus.SKIPPED
    assert record.error_code == "max_model_calls"
    assert (record.sequence, record.model_call) == (3, 4)
    assert spy.calls == []


async def test_a_skipped_call_the_model_invented_is_still_stored_as_unknown():
    _, record = default_registry().skipped(
        request("SENTINELNAME_nope", {}), sequence=0, model_call=4, reason="max_model_calls"
    )

    assert record.tool_name == UNKNOWN_TOOL_NAME
    assert "SENTINEL" not in repr(record)


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------


async def test_the_record_carries_the_sequence_and_model_call_it_was_given():
    """Every row of a turn is written in one transaction, and PostgreSQL's
    now() is constant within one - so these two integers are the only ordering
    there is."""
    _, record = await default_registry().execute(
        request("list_doctors", {}), context(), sequence=7, model_call=3
    )

    assert (record.sequence, record.model_call) == (7, 3)


def test_the_agent_status_enum_matches_the_database_one():
    """`app/agent/` may not import `app/db/`, so the two are separate enums -
    which is exactly why they need a test. A value here that the CHECK
    constraint does not know is a row PostgreSQL rejects inside a job."""
    from app.db.enums import ToolExecutionStatus as DbStatus

    assert [s.value for s in ToolExecutionStatus] == [s.value for s in DbStatus]
    assert {s.name for s in ToolExecutionStatus} == {s.name for s in DbStatus}


def test_the_tool_specs_and_clock_template_are_pinned_to_the_prompt_version():
    """Q13. The tool descriptions and the clock message instruct the model as
    much as the system prompt does.

    A tool description is read on EVERY model call and is what the model uses to
    decide which tool to call and how - amendment B2's three time periods live
    there, not in the prompt. Pinning only the prompt would let someone change
    "afternoon is 12:00 to 17:00" without a version bump, and the `agent_runs`
    row would still say `vs006-1`.

    If this fails after an intentional edit: bump SYSTEM_PROMPT_VERSION, add
    both digests (this one and the prompt's), and keep the old entries.
    """
    import hashlib
    from dataclasses import asdict

    from app.agent.clock import CLOCK_TEMPLATE
    from app.agent.prompts import SYSTEM_PROMPT_VERSION

    pinned = {
        "vs006-1": "966c838e54d036ed07a8a43973429bb7fd02e35537139e08a6578d71845c34a1",
    }
    specs = json.dumps([asdict(spec) for spec in default_registry().specs()], sort_keys=True)
    digest = hashlib.sha256((specs + CLOCK_TEMPLATE).encode("utf-8")).hexdigest()

    assert SYSTEM_PROMPT_VERSION in pinned, (
        f"unpinned version {SYSTEM_PROMPT_VERSION}: add its tool digest {digest}"
    )
    assert digest == pinned[SYSTEM_PROMPT_VERSION], (
        f"a tool spec or the clock template changed without a version bump; new digest is {digest}"
    )
