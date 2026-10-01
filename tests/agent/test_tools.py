"""What the three tools return, and what they pass to the Booking Service.

No database and no network. The clock is frozen everywhere, because "tomorrow"
and "already past" are only testable when "now" is a fact.
"""

import json
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.agent.tools import MAX_SLOTS_RETURNED, ToolContext, default_registry
from app.integrations.booking import ClinicInfo, Doctor, Location, OpeningHours, Service
from app.integrations.booking.fake import FakeBookingClient, FakeClinic, FakeDoctor
from app.integrations.openai import ToolCallRequest
from tests.integrations.booking_fakes import RecordingBooking

BEIRUT = ZoneInfo("Asia/Beirut")
TUESDAY = datetime(2026, 9, 29, 7, tzinfo=UTC)  # Tue 29 Sep 2026, 10:00 local
TENANT = "clinic-alpha"


async def call(name: str, arguments: dict, *, booking=None, now: datetime = TUESDAY):
    booking = booking or FakeBookingClient.demo(clock=lambda: now)
    content, record = await default_registry().execute(
        ToolCallRequest("call_1", name, json.dumps(arguments)),
        ToolContext(TENANT, booking, now),
        sequence=0,
        model_call=1,
    )
    return json.loads(content), record


# --------------------------------------------------------------------------
# get_clinic_information
# --------------------------------------------------------------------------


async def test_clinic_information_returns_the_clinics_own_details():
    payload, record = await call("get_clinic_information", {})

    assert payload["name"] == "Doctoleb Demo Clinic"
    assert payload["timezone"] == "Asia/Beirut"
    assert payload["locations"] == [{"name": "Main branch", "address": "1 Demo Street, Beirut"}]
    assert len(payload["policies"]) == 2
    assert record.status.value == "OK"


async def test_clinic_information_marks_closed_days():
    """A closed day carries NO times at all, so "closed" cannot disagree with an
    `opens` the model half-reads. Weekday NAMES, because the model is answering
    a patient who asked about "Saturday"."""
    payload, _ = await call("get_clinic_information", {})

    hours = {entry["day"]: entry for entry in payload["opening_hours"]}
    assert hours["Sunday"] == {"day": "Sunday", "closed": True}
    assert "opens" not in hours["Sunday"]
    assert hours["Saturday"] == {"day": "Saturday", "opens": "09:00", "closes": "13:00"}
    assert hours["Monday"]["closes"] == "17:00"
    assert list(hours) == [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    ]


async def test_clinic_information_carries_no_prices_for_the_demo_clinic():
    """Q7. The prompt says facts come only from tool results, so with an empty
    list here the model has no price to state - which is the point."""
    payload, _ = await call("get_clinic_information", {})

    assert payload["pricing"] == []


# --------------------------------------------------------------------------
# list_doctors
# --------------------------------------------------------------------------


async def test_list_doctors_returns_ids_names_specialties_and_services():
    payload, _ = await call("list_doctors", {})

    karim = next(d for d in payload["doctors"] if d["doctor_id"] == "doc_karim")
    assert karim["name"] == "Dr. Karim Haddad"
    assert karim["specialty"] == "General practice"
    assert karim["services"] == [
        {"name": "Consultation", "duration_minutes": 20},
        {"name": "Follow-up visit", "duration_minutes": 20},
    ]


async def test_list_doctors_does_not_expose_a_service_id():
    """No VS-006 tool takes one. An id the model can see but cannot use is an
    invitation to invent a call for it."""
    payload, _ = await call("list_doctors", {})

    assert "service_id" not in json.dumps(payload)


# --------------------------------------------------------------------------
# search_available_slots
# --------------------------------------------------------------------------


async def test_the_search_passes_the_injected_tenant_and_aware_datetimes():
    """Hard rule 4 at the boundary the tool actually crosses.

    The tenant the client receives is the one OUR code put in the context, and
    the window is two real instants - never the naive strings the model sent.
    """
    spy = RecordingBooking(FakeBookingClient.demo(clock=lambda: TUESDAY))

    await call(
        "search_available_slots",
        {"doctor_id": "doc_karim", "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
        booking=spy,
    )

    assert spy.tenants == [TENANT]
    start = spy.calls[0].kwargs["start"]
    end = spy.calls[0].kwargs["end"]
    assert start.tzinfo is not None and end.tzinfo is not None
    assert start == datetime(2026, 9, 30, 12, tzinfo=BEIRUT)
    assert start.utcoffset().total_seconds() == 3 * 3600
    assert spy.calls[0].kwargs["doctor_id"] == "doc_karim"


async def test_results_are_clinic_local_times_with_day_names():
    """The slice's goal. Same spelling as the model was asked to send, plus the
    weekday - the model is often answering "what about Wednesday?" and should
    not have to do weekday arithmetic it is bad at."""
    payload, _ = await call(
        "search_available_slots",
        {"doctor_id": "doc_karim", "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
    )

    assert payload["doctor_id"] == "doc_karim"
    assert payload["timezone"] == "Asia/Beirut"
    assert payload["searched"] == {"start": "2026-09-30T12:00", "end": "2026-09-30T17:00"}
    # `slot_id` was added in VS-007 (V10), so it is compared separately from the
    # times: what this test is about is the SPELLING of the times, and the ids the
    # frozen fake issues are pinned in tests/integrations/test_fake_booking.py.
    assert [
        {key: value for key, value in slot.items() if key != "slot_id"} for slot in payload["slots"]
    ] == [
        {"day": "Wednesday", "start": "2026-09-30T14:00", "end": "2026-09-30T14:20"},
        {"day": "Wednesday", "start": "2026-09-30T14:20", "end": "2026-09-30T14:40"},
        {"day": "Wednesday", "start": "2026-09-30T15:40", "end": "2026-09-30T16:00"},
        {"day": "Wednesday", "start": "2026-09-30T16:20", "end": "2026-09-30T16:40"},
    ]
    assert all(slot["slot_id"] for slot in payload["slots"])
    assert payload["more_available"] is False


async def test_results_expose_each_slots_id():
    """INVERTED deliberately in VS-007 (plan conflict C4).

    VS-006 hid `slot_id` because nothing could act on one, and an id the model can
    see but cannot use invites it to claim it has reserved something. Now
    `hold_appointment_slot` takes one, so hiding it would mean asking the model to
    name a time in words and having our code guess which slot it meant.

    What stops the model inventing or reusing one (V10): the ids are opaque tokens
    only the issuing service can resolve; `OPAQUE_ID` refuses anything with a space
    in it, so "14:00 tomorrow" is invalid arguments; an unknown id is the fixed
    error `slot_not_found`; and the prompt forbids writing an id into a reply.
    """
    payload, _ = await call(
        "search_available_slots",
        {"doctor_id": "doc_karim", "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
    )

    assert payload["slots"], "the demo clinic has Wednesday afternoon slots"
    for slot in payload["slots"]:
        assert slot["slot_id"]
    # The FAKE's ids are transparent here, because this test calls the frozen fake
    # directly rather than the in-memory service that tokenises them. That is the
    # honest boundary: this tool passes through whatever id its client issued, and
    # tests/integrations/test_memory_booking.py is where the tokens are pinned.
    assert "hold_id" not in json.dumps(payload)
    assert "reference" not in json.dumps(payload)


async def test_a_start_in_the_past_is_clamped_to_now():
    """Not an error: "today from 09:00" asked at 10:00 is a reasonable thing for
    the model to send, and clamping saves a whole model call.

    `searched` echoes the CLAMPED window, so the model can see what was actually
    searched rather than assuming its own values were used.
    """
    spy = RecordingBooking(FakeBookingClient.demo(clock=lambda: TUESDAY))

    payload, _ = await call(
        "search_available_slots",
        {"doctor_id": "doc_karim", "start": "2026-09-29T09:00", "end": "2026-09-29T17:00"},
        booking=spy,
    )

    assert spy.calls[0].kwargs["start"] == TUESDAY  # clamped up to now
    assert payload["searched"]["start"] == "2026-09-29T10:00"
    assert payload["searched"]["end"] == "2026-09-29T17:00"
    # And nothing before 10:00 comes back.
    assert all(slot["start"] >= "2026-09-29T10:00" for slot in payload["slots"])


async def test_monday_afternoon_after_the_autumn_change_is_searched_at_plus_two():
    """The DST trap, at the layer that converts.

    Frozen on Saturday 24 October (still +03:00), a search for Monday the 26th
    must be sent at +02:00 - the offset in force on THAT date. Reusing today's
    offset would search an hour early and return the wrong times.
    """
    saturday = datetime(2026, 10, 24, 7, tzinfo=UTC)  # 10:00 local, +03:00
    spy = RecordingBooking(FakeBookingClient.demo(clock=lambda: saturday))

    payload, _ = await call(
        "search_available_slots",
        {"doctor_id": "doc_karim", "start": "2026-10-26T12:00", "end": "2026-10-26T17:00"},
        booking=spy,
        now=saturday,
    )

    start = spy.calls[0].kwargs["start"]
    end = spy.calls[0].kwargs["end"]
    assert start.utcoffset().total_seconds() == 2 * 3600
    assert end.utcoffset().total_seconds() == 2 * 3600
    assert start.astimezone(UTC) == datetime(2026, 10, 26, 10, tzinfo=UTC)
    # And what comes back is still spelled in local time.
    assert payload["slots"][0]["start"] == "2026-10-26T14:00"
    assert payload["slots"][0]["day"] == "Monday"


async def test_results_are_capped_and_say_so():
    """A dense week could otherwise return two hundred times: tokens paid on
    every later model call of the turn, and a list no WhatsApp reply could use.
    `more_available` is how the model knows not to say "that is all"."""
    busy = FakeClinic(
        info=ClinicInfo(
            name="Busy Demo Clinic",
            timezone="Asia/Beirut",
            locations=(Location(name="Main", address="2 Demo Street, Beirut"),),
            opening_hours=(OpeningHours(weekday=2, opens=time(9), closes=time(17)),),
        ),
        doctors=(
            FakeDoctor(
                doctor=Doctor(
                    doctor_id="doc_busy",
                    name="Dr. Busy Demo",
                    specialty="General practice",
                    services=(Service(service_id="s", name="Consultation", duration_minutes=10),),
                ),
                starts={2: tuple(time(9 + n // 6, (n % 6) * 10) for n in range(30))},
                slot_minutes=10,
            ),
        ),
    )
    booking = FakeBookingClient(clinics={TENANT: busy}, clock=lambda: TUESDAY)

    payload, _ = await call(
        "search_available_slots",
        {"doctor_id": "doc_busy", "start": "2026-09-30T09:00", "end": "2026-09-30T17:00"},
        booking=booking,
    )

    assert len(payload["slots"]) == MAX_SLOTS_RETURNED == 10
    assert payload["more_available"] is True


async def test_an_empty_result_is_an_empty_list_not_an_error():
    """Sunday is closed. "No times" is a real answer the model must be able to
    give; turning it into an error would push the model towards guessing."""
    payload, record = await call(
        "search_available_slots",
        {"doctor_id": "doc_karim", "start": "2026-10-04T09:00", "end": "2026-10-04T17:00"},
    )

    assert payload["slots"] == []
    assert payload["more_available"] is False
    assert record.status.value == "OK"


# --------------------------------------------------------------------------
# Across all three
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_clinic_information", {}),
        ("list_doctors", {}),
        (
            "search_available_slots",
            {"doctor_id": "doc_karim", "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
        ),
    ],
)
async def test_no_tool_result_contains_the_tenant_id(name, arguments):
    """Hard rule 4. A result goes straight into the next request to OpenAI, so
    a tenant id in one is a tenant id handed to the model."""
    sentinel_tenant = "clinic-SENTINELTENANT"
    booking = FakeBookingClient.demo(clock=lambda: TUESDAY)
    content, record = await default_registry().execute(
        ToolCallRequest("call_1", name, json.dumps(arguments)),
        ToolContext(sentinel_tenant, booking, TUESDAY),
        sequence=0,
        model_call=1,
    )

    assert "SENTINELTENANT" not in content
    assert "tenant" not in content.lower()
    assert "SENTINELTENANT" not in repr(record)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_clinic_information", {}),
        ("list_doctors", {}),
        (
            "search_available_slots",
            {"doctor_id": "doc_karim", "start": "2026-09-30T12:00", "end": "2026-09-30T17:00"},
        ),
    ],
)
async def test_every_result_is_json_the_model_can_read(name, arguments):
    """The tool message is a string. Unicode stays Unicode (`ensure_ascii=False`)
    because a clinic's data can be Arabic and escaping it triples the tokens."""
    content, _ = await default_registry().execute(
        ToolCallRequest("call_1", name, json.dumps(arguments)),
        ToolContext(TENANT, FakeBookingClient.demo(clock=lambda: TUESDAY), TUESDAY),
        sequence=0,
        model_call=1,
    )

    assert isinstance(json.loads(content), dict)
    assert "\\u" not in content


def test_the_tool_context_refuses_a_naive_now():
    with pytest.raises(ValueError, match="aware"):
        ToolContext(TENANT, FakeBookingClient.demo(clock=lambda: TUESDAY), datetime(2026, 9, 29))


def test_the_tool_context_repr_shows_only_the_tenant():
    """A clinic identifier is safe; a booking client's repr might not be, and
    `now` is noise. Reprs reach tracebacks and pytest output."""
    rendered = repr(ToolContext(TENANT, FakeBookingClient.demo(clock=lambda: TUESDAY), TUESDAY))

    assert rendered == "ToolContext(tenant_id='clinic-alpha')"
