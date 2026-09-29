"""The BookingClient interface and its in-memory implementation.

No database and no network: every test here builds a client and asks it one
question. The clock is always frozen, because "a slot in the past is never
returned" is only testable if "now" is a fact rather than the wall clock.
"""

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.integrations.booking import (
    BookingClient,
    BookingError,
    ClinicInfo,
    Doctor,
    Location,
    Service,
)
from app.integrations.booking.fake import (
    DEMO_CLINIC,
    FakeBookingClient,
    FakeClinic,
    FakeDoctor,
)
from tests.integrations.booking_fakes import RecordingBooking

BEIRUT = ZoneInfo("Asia/Beirut")
# Tuesday 29 September 2026, 10:00 local. The day Task 9's acceptance test
# freezes, so "tomorrow" is Wednesday the 30th.
TUESDAY_MORNING = datetime(2026, 9, 29, 7, tzinfo=UTC)
TENANT = "clinic-alpha"


def frozen(moment: datetime = TUESDAY_MORNING):
    return lambda: moment


def local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=BEIRUT)


def demo(moment: datetime = TUESDAY_MORNING) -> FakeBookingClient:
    return FakeBookingClient.demo(clock=frozen(moment))


def test_the_fake_and_the_spy_satisfy_the_booking_protocol():
    """A runtime_checkable Protocol, so a spy that drifts from the interface
    fails here rather than deep inside a loop test."""
    assert isinstance(demo(), BookingClient)
    assert isinstance(RecordingBooking(demo()), BookingClient)


async def test_the_demo_clinic_has_dr_karim_and_his_services():
    doctors = await demo().list_doctors(TENANT)

    karim = next(d for d in doctors if d.doctor_id == "doc_karim")
    assert karim.name == "Dr. Karim Haddad"
    assert karim.specialty == "General practice"
    assert [s.name for s in karim.services] == ["Consultation", "Follow-up visit"]
    assert all(s.duration_minutes == 20 for s in karim.services)
    assert [d.doctor_id for d in doctors] == ["doc_karim", "doc_rania", "doc_samir"]


async def test_the_demo_clinic_is_obviously_fake():
    """Hard rule 8, and risk R8: fake availability reaching a real patient.

    The name and address are the last line of defence if the startup warning is
    missed, so they are pinned rather than left to taste.
    """
    info = await demo().get_clinic(TENANT)

    assert info.name == "Doctoleb Demo Clinic"
    assert info.locations[0].address == "1 Demo Street, Beirut"
    assert info.timezone == "Asia/Beirut"
    # Q7: no prices. The prompt says facts come only from tool results, so with
    # none here the model has none to state.
    assert info.pricing == ()


async def test_sunday_is_closed_and_saturday_ends_early():
    info = await demo().get_clinic(TENANT)

    hours = {entry.weekday: entry for entry in info.opening_hours}
    assert hours[6].closed is True
    assert hours[5].closes == time(13, 0)
    assert hours[0].opens == time(9, 0) and hours[0].closes == time(17, 0)


async def test_wednesday_afternoon_for_dr_karim_is_exactly_four_slots():
    """The slice's goal, at the fake's level.

    Frozen on Tuesday 10:00 local, "tomorrow afternoon" is Wednesday
    12:00-17:00, and Dr. Karim's Wednesday pattern puts exactly four starts in
    it. Task 9's end-to-end test asserts the same four.
    """
    slots = await demo().search_slots(
        TENANT, "doc_karim", local(2026, 9, 30, 12), local(2026, 9, 30, 17)
    )

    assert [s.start.astimezone(BEIRUT).strftime("%H:%M") for s in slots] == [
        "14:00",
        "14:20",
        "15:40",
        "16:20",
    ]
    assert all(s.doctor_id == "doc_karim" for s in slots)
    assert (slots[0].end - slots[0].start).total_seconds() == 20 * 60
    # Sorted, and distinct ids: the loop turns these into a list for the model,
    # and a duplicate would be a time offered twice.
    assert [s.start for s in slots] == sorted(s.start for s in slots)
    assert len({s.slot_id for s in slots}) == len(slots)


async def test_slots_already_past_are_never_returned():
    """Offering a time that has gone is worse than offering nothing: the patient
    asks for it and the clinic has to take it back."""
    # 15:00 local on Wednesday: 14:00 and 14:20 are gone, 15:40 and 16:20 are not.
    client = demo(datetime(2026, 9, 30, 12, tzinfo=UTC))

    slots = await client.search_slots(
        TENANT, "doc_karim", local(2026, 9, 30, 12), local(2026, 9, 30, 17)
    )

    assert [s.start.astimezone(BEIRUT).strftime("%H:%M") for s in slots] == ["15:40", "16:20"]


async def test_slots_across_the_autumn_change_carry_each_days_offset():
    """The DST trap, at the only layer that can get it wrong silently.

    Lebanon leaves DST at 2026-10-24T21:00Z. Saturday 24 October is +03:00 and
    Monday 26 October is +02:00, so the same local clock time is a different
    instant on the two days. Reusing one offset for the whole window would be an
    hour wrong on one side and right on the other.
    """
    client = demo(datetime(2026, 10, 24, 5, tzinfo=UTC))

    slots = await client.search_slots(
        TENANT, "doc_karim", local(2026, 10, 24, 9), local(2026, 10, 26, 17)
    )

    by_local = {
        (
            s.start.astimezone(BEIRUT).date().isoformat(),
            s.start.astimezone(BEIRUT).strftime("%H:%M"),
        ): s
        for s in slots
    }
    saturday_noon = by_local[("2026-10-24", "12:00")]
    monday_two = by_local[("2026-10-26", "14:00")]
    assert saturday_noon.start.astimezone(UTC).hour == 9  # 12:00 +03:00
    assert monday_two.start.astimezone(UTC).hour == 12  # 14:00 +02:00
    assert saturday_noon.start.utcoffset().total_seconds() == 3 * 3600
    assert monday_two.start.utcoffset().total_seconds() == 2 * 3600
    # Sunday is not in the pattern at all, so nothing was invented for it.
    assert not [s for s in slots if s.start.astimezone(BEIRUT).date().isoformat() == "2026-10-25"]


async def test_an_unknown_doctor_is_not_found():
    with pytest.raises(BookingError) as raised:
        await demo().search_slots(
            TENANT, "doc_nobody", local(2026, 9, 30, 12), local(2026, 9, 30, 17)
        )

    assert raised.value.code == "NOT_FOUND"


async def test_an_unknown_tenant_is_not_found_without_a_default():
    """Hard rule 4 at the fake's level: no default tenant, ever.

    Serving "the only clinic we know" to an unrecognised tenant is how one
    clinic's availability reaches another clinic's patient - and a fake that
    does it teaches the wrong shape to VS-011's real client.
    """
    client = FakeBookingClient(clinics={TENANT: DEMO_CLINIC}, clock=frozen())

    assert (await client.get_clinic(TENANT)).name == "Doctoleb Demo Clinic"
    for method in (client.get_clinic, client.list_doctors):
        with pytest.raises(BookingError) as raised:
            await method("clinic-nobody")
        assert raised.value.code == "NOT_FOUND"


async def test_each_tenant_sees_only_its_own_clinic():
    """Including a pair differing only in case (Q2): two spellings, two tenants."""
    other = FakeClinic(
        info=ClinicInfo(
            name="Other Demo Clinic",
            timezone="Asia/Beirut",
            locations=(Location(name="Branch", address="2 Demo Street, Beirut"),),
        ),
        doctors=(
            FakeDoctor(
                doctor=Doctor(
                    doctor_id="doc_other",
                    name="Dr. Other Demo",
                    specialty="General practice",
                    services=(Service(service_id="svc", name="Consultation", duration_minutes=15),),
                ),
                starts={2: (time(10, 0),)},
                slot_minutes=15,
            ),
        ),
    )
    client = FakeBookingClient(
        clinics={"clinic-alpha": DEMO_CLINIC, "Clinic-Alpha": other}, clock=frozen()
    )

    assert (await client.get_clinic("clinic-alpha")).name == "Doctoleb Demo Clinic"
    assert (await client.get_clinic("Clinic-Alpha")).name == "Other Demo Clinic"
    assert [d.doctor_id for d in await client.list_doctors("Clinic-Alpha")] == ["doc_other"]
    # And one tenant's doctor id is not valid for the other.
    with pytest.raises(BookingError):
        await client.search_slots(
            "clinic-alpha", "doc_other", local(2026, 9, 30, 9), local(2026, 9, 30, 17)
        )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 9, 30, 12), local(2026, 9, 30, 17)),  # naive start
        (local(2026, 9, 30, 12), datetime(2026, 9, 30, 17)),  # naive end
        (local(2026, 9, 30, 17), local(2026, 9, 30, 12)),  # reversed
        (local(2026, 9, 30, 12), local(2026, 9, 30, 12)),  # empty
    ],
)
async def test_a_naive_or_reversed_window_is_a_validation_error(start, end):
    """A naive datetime is refused rather than assumed to be clinic-local.

    Guessing a zone here would be wrong for exactly one hour twice a year and
    right the rest of the time, which is the hardest kind of bug to find.
    """
    with pytest.raises(BookingError) as raised:
        await demo().search_slots(TENANT, "doc_karim", start, end)

    assert raised.value.code == "VALIDATION"


async def test_service_id_is_accepted_and_ignored():
    """Plan conflict C4b. `GET /slots` takes a service_id, the tool does not
    expose one, and the fake must not start filtering on something no caller can
    set (Follow-up 2)."""
    window = (local(2026, 9, 30, 12), local(2026, 9, 30, 17))

    without = await demo().search_slots(TENANT, "doc_karim", *window)
    with_service = await demo().search_slots(TENANT, "doc_karim", *window, "svc_consultation")
    nonsense = await demo().search_slots(TENANT, "doc_karim", *window, "svc_does_not_exist")

    assert (
        [s.slot_id for s in without]
        == [s.slot_id for s in with_service]
        == [s.slot_id for s in nonsense]
    )


async def test_the_fake_keeps_no_per_call_state():
    """arq runs several jobs at once in one process, sharing this instance.

    A call log or a counter would be a race AND a memory leak in a worker that
    runs for weeks. Tests that need to see calls use RecordingBooking.
    """
    client = demo()
    before = dict(client.__dict__)

    for _ in range(100):
        await client.get_clinic(TENANT)
        await client.list_doctors(TENANT)
        await client.search_slots(
            TENANT, "doc_karim", local(2026, 9, 30, 12), local(2026, 9, 30, 17)
        )

    assert client.__dict__ == before
    # Frozen too, so nothing can add state to it later either.
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        client.default = None  # type: ignore[misc]


async def test_repeated_searches_return_the_same_slot_ids():
    """Deterministic ids, built from the UTC instant.

    VS-007 will hold a slot by its id, so an id that changed between two calls
    would turn "the time you asked for" into "a time that no longer exists".
    """
    window = (local(2026, 9, 30, 12), local(2026, 9, 30, 17))

    first = await demo().search_slots(TENANT, "doc_karim", *window)
    second = await demo().search_slots(TENANT, "doc_karim", *window)

    assert [s.slot_id for s in first] == [s.slot_id for s in second]
    assert first[0].slot_id.startswith("doc_karim:")


def test_a_booking_error_carries_only_its_code():
    """The contract's error body has a `message`; it is never read.

    It comes from another system and can quote clinic or patient data, and this
    exception reaches a log line and a tool result (hard rule 8).
    """
    error = BookingError("UNAVAILABLE")

    assert str(error) == "UNAVAILABLE"
    assert repr(error) == "BookingError('UNAVAILABLE')"
    assert error.args == ("UNAVAILABLE",)
    with pytest.raises(ValueError):
        BookingError("SENTINEL-not-a-code")


async def test_the_fake_covers_the_contracts_read_endpoints():
    """docs/booking-contract.md's read side, endpoint by endpoint.

    The write side (holds, appointments, reschedule, cancel) is VS-007's and is
    deliberately absent: a method that existed but did nothing would be
    something hard rule 5 could be broken through.
    """
    client = demo()
    mapping = {
        "GET /clinic": "get_clinic",
        "GET /doctors": "list_doctors",
        "GET /slots?doctor_id=&service_id=&from=&to=": "search_slots",
    }

    for endpoint, method in mapping.items():
        assert callable(getattr(client, method, None)), endpoint
    for absent in ("create_hold", "create_appointment", "reschedule", "cancel"):
        assert not hasattr(client, absent), absent


async def test_the_spy_records_the_tenant_and_the_window_it_was_given():
    """What every later tool and loop test asserts through."""
    spy = RecordingBooking(demo())
    window = (local(2026, 9, 30, 12), local(2026, 9, 30, 17))

    await spy.list_doctors(TENANT)
    slots = await spy.search_slots(TENANT, "doc_karim", *window)

    assert spy.methods == ["list_doctors", "search_slots"]
    assert spy.tenants == [TENANT, TENANT]
    assert spy.calls[1].kwargs["doctor_id"] == "doc_karim"
    assert (spy.calls[1].kwargs["start"], spy.calls[1].kwargs["end"]) == window
    assert len(slots) == 4


async def test_the_spy_can_inject_a_booking_failure():
    spy = RecordingBooking(demo(), raises=BookingError("UNAVAILABLE"))

    with pytest.raises(BookingError) as raised:
        await spy.list_doctors(TENANT)

    assert raised.value.code == "UNAVAILABLE"
    assert spy.methods == ["list_doctors"]  # recorded before it raised
