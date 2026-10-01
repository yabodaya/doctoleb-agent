"""The stateful in-memory Booking Service (VS-007, plan section 5.3).

No database and no network. Every test builds **its own** service, with
`counter_ids()` for readable ids, a fixed `id_secret` for stable slot tokens, and a
clock it controls. Its own, because an `asyncio.Lock` binds to the loop it is first
contended in (Appendix C / U5), so one shared instance across tests would raise
`RuntimeError: ... bound to a different event loop` the moment two tests contended
on it.

What this file is really testing is the write side's *rules*, because those rules
are what the tools will lean on: which failure means "nothing changed", which
repeat returns the existing result rather than doing it twice, and who is allowed
to see whose appointment.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.integrations.booking import (
    BookingClient,
    BookingError,
    PatientBookingClient,
    PatientRef,
)
from app.integrations.booking.fake import DEMO_CLINIC, FakeBookingClient, FakeClinic
from app.integrations.booking.memory import (
    REFERENCE_ALPHABET,
    FailureScript,
    InMemoryBookingService,
    Limits,
)
from tests.integrations.booking_fakes import counter_ids

BEIRUT = ZoneInfo("Asia/Beirut")
# Tuesday 29 September 2026, 10:00 Beirut, as everywhere else in the suite.
TUESDAY_MORNING = datetime(2026, 9, 29, 7, tzinfo=UTC)
TENANT = "clinic-alpha"
OTHER_TENANT = "clinic-beta"
RAMI = PatientRef("contact-rami")
ZEINA = PatientRef("contact-zeina")
SECRET = b"a fixed secret, so slot tokens are stable across a test run"


class Clock:
    """A clock a test moves by hand."""

    def __init__(self, moment: datetime = TUESDAY_MORNING) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> None:
        self.moment += timedelta(**delta)


def build(
    clock: Clock | None = None,
    *,
    catalogue: FakeBookingClient | None = None,
    **options: object,
) -> tuple[InMemoryBookingService, Clock]:
    clock = clock or Clock()
    catalogue = catalogue or FakeBookingClient.demo(clock=clock)
    service = InMemoryBookingService(
        catalogue, clock, id_secret=SECRET, new_id=counter_ids(), **options
    )
    return service, clock


async def karim_slots(service: InMemoryBookingService, tenant: str = TENANT) -> tuple:
    """Dr. Karim's Wednesday afternoon: 14:00, 14:20, 15:40, 16:20 (VS-006's fake)."""
    return await service.search_slots(
        tenant,
        "doc_karim",
        datetime(2026, 9, 30, 12, tzinfo=BEIRUT),
        datetime(2026, 9, 30, 17, tzinfo=BEIRUT),
    )


def test_the_service_satisfies_both_protocols():
    """One object is the whole Booking Service as far as the tools are concerned:
    the catalogue (delegated to the frozen fake) and the patient's own actions."""
    service, _ = build()

    assert isinstance(service, BookingClient)
    assert isinstance(service, PatientBookingClient)


async def test_the_catalogue_is_delegated_to_the_frozen_fake():
    service, _ = build()

    clinic = await service.get_clinic(TENANT)
    doctors = await service.list_doctors(TENANT)

    assert clinic.name == "Doctoleb Demo Clinic"
    assert [doctor.doctor_id for doctor in doctors] == ["doc_karim", "doc_rania", "doc_samir"]


async def test_search_hides_held_and_booked_slots():
    """The point of holds: nobody else is offered a slot somebody is deciding on."""
    service, _ = build()
    slots = await karim_slots(service)
    assert len(slots) == 4

    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    after_hold = await karim_slots(service)
    assert [slot.slot_id for slot in after_hold] == [slot.slot_id for slot in slots[1:]]

    await service.create_appointment(TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2")
    after_booking = await karim_slots(service)
    assert [slot.slot_id for slot in after_booking] == [slot.slot_id for slot in slots[1:]]


async def test_search_issues_opaque_slot_ids_that_stay_stable_within_the_service():
    """V10. The model is given these ids, so they must be unguessable, and stable
    enough that a patient can answer "the 14:00 one" after a second search."""
    service, _ = build()

    first = await karim_slots(service)
    again = await karim_slots(service)

    assert [slot.slot_id for slot in first] == [slot.slot_id for slot in again]
    for slot in first:
        assert slot.slot_id.startswith("slot_")
        assert len(slot.slot_id) == len("slot_") + 20
        # Nothing about the doctor or the time can be read out of it: the fake's
        # own id is doc_karim:2026-09-30T11:00:00+00:00, and this is not that.
        assert "doc_karim" not in slot.slot_id
        assert "2026" not in slot.slot_id
    assert len({slot.slot_id for slot in first}) == 4


async def test_the_slot_token_differs_per_tenant_and_per_secret():
    """Keyed on the tenant, so one clinic's token cannot resolve in another, and on
    a per-instance secret, so a token cannot be carried over from another process."""
    service, clock = build()
    other_secret = InMemoryBookingService(
        FakeBookingClient.demo(clock=clock), clock, id_secret=b"different", new_id=counter_ids()
    )

    mine = [slot.slot_id for slot in await karim_slots(service)]
    across_tenants = [slot.slot_id for slot in await karim_slots(service, OTHER_TENANT)]
    across_secrets = [slot.slot_id for slot in await karim_slots(other_secret)]

    assert mine != across_tenants
    assert mine != across_secrets


@pytest.mark.parametrize(
    "invented",
    [
        "slot_neverissuedbythisservice",
        "doc_karim:2026-09-30T11:00:00+00:00",  # the frozen fake's own transparent id
        "",
    ],
    ids=["invented", "the raw catalogue id", "empty"],
)
async def test_an_invented_slot_id_is_not_found(invented):
    """The third defence of V10: even a well-formed id that this service did not
    issue resolves to nothing."""
    service, _ = build()
    await karim_slots(service)  # tokens exist, just not this one

    with pytest.raises(BookingError) as raised:
        await service.create_hold(TENANT, RAMI, invented, idempotency_key="k1")

    assert raised.value.code == "NOT_FOUND"


async def test_a_hold_then_a_booking_confirms_one_appointment():
    """The happy path, and what the receipt is built from."""
    service, _ = build()
    slots = await karim_slots(service)

    hold = await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")

    assert hold.hold_id == "hold_1"
    assert hold.slot_id == slots[0].slot_id  # the opaque token, not the raw id
    assert hold.doctor_id == "doc_karim"
    assert hold.doctor_name == "Dr. Karim Haddad"
    assert hold.start == slots[0].start
    assert hold.expires_at == TUESDAY_MORNING + timedelta(minutes=10)

    appointment = await service.create_appointment(
        TENANT, RAMI, hold.hold_id, "Rami Khoury", idempotency_key="k2"
    )

    assert appointment.appointment_id == "apt_1"
    assert appointment.status == "CONFIRMED"
    assert appointment.doctor_name == "Dr. Karim Haddad"
    assert appointment.start == slots[0].start
    assert len(appointment.reference) == 6
    assert set(appointment.reference) <= set(REFERENCE_ALPHABET)

    assert await service.list_appointments(TENANT, RAMI) == (appointment,)


async def test_a_slot_held_by_another_patient_is_slot_taken():
    service, _ = build()
    slots = await karim_slots(service)

    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")

    with pytest.raises(BookingError) as raised:
        await service.create_hold(TENANT, ZEINA, slots[0].slot_id, idempotency_key="k2")

    assert raised.value.code == "SLOT_TAKEN"


async def test_a_slot_booked_by_another_patient_is_slot_taken():
    service, _ = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    await service.create_appointment(TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2")

    with pytest.raises(BookingError) as raised:
        await service.create_hold(TENANT, ZEINA, slots[0].slot_id, idempotency_key="k3")

    assert raised.value.code == "SLOT_TAKEN"


async def test_holding_the_same_slot_again_returns_the_same_hold():
    """V13, under a DIFFERENT key. A re-run of the turn regenerates the model's
    request, so the key can change; the hold must not."""
    service, _ = build()
    slots = await karim_slots(service)

    first = await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    again = await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="different")

    assert again == first
    assert again.hold_id == "hold_1"  # no second hold was made


async def test_a_new_hold_releases_the_patients_previous_hold():
    """One active hold per patient, so a patient who changes their mind does not
    quietly take two slots out of circulation."""
    service, _ = build()
    slots = await karim_slots(service)

    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    await service.create_hold(TENANT, RAMI, slots[1].slot_id, idempotency_key="k2")

    visible = {slot.slot_id for slot in await karim_slots(service)}
    assert slots[0].slot_id in visible  # released
    assert slots[1].slot_id not in visible  # held

    with pytest.raises(BookingError) as raised:
        await service.create_appointment(
            TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k3"
        )
    assert raised.value.code == "HOLD_EXPIRED"


async def test_a_hold_expires_on_the_injected_clock():
    """Plan risk R5: hold expiry is the injected clock's business, never the wall
    clock's. In tests the injected clock sits in 2026; if this read the real one,
    every hold would be expired or none would be, depending on the calendar."""
    service, clock = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")

    clock.advance(minutes=9)
    assert slots[0].slot_id not in {slot.slot_id for slot in await karim_slots(service)}

    clock.advance(minutes=2)  # 11 minutes: past the ten-minute TTL
    assert slots[0].slot_id in {slot.slot_id for slot in await karim_slots(service)}


async def test_booking_an_expired_hold_is_hold_expired():
    service, clock = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    clock.advance(minutes=11)

    with pytest.raises(BookingError) as raised:
        await service.create_appointment(
            TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2"
        )

    assert raised.value.code == "HOLD_EXPIRED"


async def test_booking_a_consumed_hold_again_returns_the_same_appointment():
    """V13 again, and the one that matters most: this is what stops a re-run of a
    turn that already booked from booking twice, even when the key changed because
    the model spelled the name differently."""
    service, _ = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")

    first = await service.create_appointment(
        TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2"
    )
    again = await service.create_appointment(
        TENANT, RAMI, "hold_1", "Rami  Khouri", idempotency_key="a different key"
    )

    assert again == first
    assert len(await service.list_appointments(TENANT, RAMI)) == 1


async def test_booking_a_hold_whose_appointment_was_cancelled_is_hold_expired():
    """A hold is spent once used. Reviving it would resurrect a slot the patient
    already gave up."""
    service, _ = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    await service.create_appointment(TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2")
    await service.cancel_appointment(TENANT, RAMI, "apt_1", idempotency_key="k3")

    with pytest.raises(BookingError) as raised:
        await service.create_appointment(
            TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k4"
        )

    assert raised.value.code == "HOLD_EXPIRED"


@pytest.mark.parametrize("outcome", ["success", "SLOT_TAKEN"])
async def test_the_same_key_and_body_replays_the_first_answer(outcome):
    """What the Idempotency-Key is for: the same request twice, one effect.

    A business error is remembered too. A retry of a request the service already
    refused must get the same refusal, not a second chance at a slot somebody else
    now holds.
    """
    service, _ = build(failures=FailureScript())
    slots = await karim_slots(service)

    if outcome == "success":
        first = await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="same")
        again = await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="same")
        assert again == first
        assert again.hold_id == "hold_1"
    else:
        service._failures.push("create_hold", "SLOT_TAKEN")  # noqa: SLF001
        for _ in range(2):
            with pytest.raises(BookingError) as raised:
                await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="same")
            assert raised.value.code == "SLOT_TAKEN"
        # The second call replayed: the scripted failure was consumed only once,
        # and no hold exists.
        assert service._failures.pending == 0  # noqa: SLF001
        assert slots[0].slot_id in {slot.slot_id for slot in await karim_slots(service)}


async def test_the_same_key_with_a_different_body_is_an_idempotency_conflict():
    """This can only come from a bug in our code, because the key covers the whole
    body (V1). The service detecting it is how we would find out."""
    service, _ = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="same")

    with pytest.raises(BookingError) as raised:
        await service.create_hold(TENANT, RAMI, slots[1].slot_id, idempotency_key="same")

    assert raised.value.code == "IDEMPOTENCY_CONFLICT"


async def test_a_key_is_scoped_to_its_tenant():
    service, _ = build()
    mine = await karim_slots(service)
    theirs = await karim_slots(service, OTHER_TENANT)

    first = await service.create_hold(TENANT, RAMI, mine[0].slot_id, idempotency_key="same")
    other = await service.create_hold(OTHER_TENANT, RAMI, theirs[0].slot_id, idempotency_key="same")

    assert other.hold_id != first.hold_id


@pytest.mark.parametrize(
    "operation",
    ["create_appointment", "reschedule_appointment", "cancel_appointment"],
)
async def test_another_patients_hold_or_appointment_is_not_found(operation):
    """404, never 403. A 403 would confirm that the id exists to somebody who
    guessed it (a contract proposal)."""
    service, _ = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    await service.create_appointment(TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2")
    await service.create_hold(TENANT, RAMI, slots[1].slot_id, idempotency_key="k3")

    with pytest.raises(BookingError) as raised:
        if operation == "create_appointment":
            await service.create_appointment(
                TENANT, ZEINA, "hold_2", "Zeina Saab", idempotency_key="z1"
            )
        elif operation == "reschedule_appointment":
            await service.reschedule_appointment(
                TENANT, ZEINA, "apt_1", "hold_2", idempotency_key="z1"
            )
        else:
            await service.cancel_appointment(TENANT, ZEINA, "apt_1", idempotency_key="z1")

    assert raised.value.code == "NOT_FOUND"


async def test_reschedule_moves_the_appointment_and_frees_the_old_slot():
    service, _ = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    booked = await service.create_appointment(
        TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2"
    )
    await service.create_hold(TENANT, RAMI, slots[2].slot_id, idempotency_key="k3")

    moved = await service.reschedule_appointment(
        TENANT, RAMI, booked.appointment_id, "hold_2", idempotency_key="k4"
    )

    # Same identity, new time: the patient keeps the code they were given.
    assert moved.appointment_id == booked.appointment_id
    assert moved.reference == booked.reference
    assert moved.start == slots[2].start
    assert moved.status == "CONFIRMED"

    visible = {slot.slot_id for slot in await karim_slots(service)}
    assert slots[0].slot_id in visible  # freed
    assert slots[2].slot_id not in visible  # now taken


async def test_rescheduling_onto_the_same_hold_again_returns_the_appointment():
    """V13 for reschedule."""
    service, _ = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    await service.create_appointment(TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2")
    await service.create_hold(TENANT, RAMI, slots[2].slot_id, idempotency_key="k3")

    first = await service.reschedule_appointment(
        TENANT, RAMI, "apt_1", "hold_2", idempotency_key="k4"
    )
    again = await service.reschedule_appointment(
        TENANT, RAMI, "apt_1", "hold_2", idempotency_key="another"
    )

    assert again == first


async def test_cancel_frees_the_slot_and_cancelling_twice_returns_it():
    """V13 for cancel: a re-run of a completed cancellation must not turn into an
    error the patient would be told about."""
    service, _ = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    await service.create_appointment(TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2")

    cancelled = await service.cancel_appointment(TENANT, RAMI, "apt_1", idempotency_key="k3")
    again = await service.cancel_appointment(TENANT, RAMI, "apt_1", idempotency_key="another")

    assert cancelled.status == "CANCELLED"
    assert again == cancelled
    assert slots[0].slot_id in {slot.slot_id for slot in await karim_slots(service)}
    assert await service.list_appointments(TENANT, RAMI) == ()


async def test_list_shows_only_this_patients_upcoming_appointments():
    service, clock = build()
    slots = await karim_slots(service)

    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    mine = await service.create_appointment(
        TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2"
    )
    await service.create_hold(TENANT, ZEINA, slots[1].slot_id, idempotency_key="z1")
    await service.create_appointment(TENANT, ZEINA, "hold_2", "Zeina Saab", idempotency_key="z2")

    assert await service.list_appointments(TENANT, RAMI) == (mine,)
    # Zeina's own appointment is hers, and a third patient sees nothing at all.
    assert [a.appointment_id for a in await service.list_appointments(TENANT, ZEINA)] == ["apt_2"]
    assert await service.list_appointments(TENANT, PatientRef("contact-nobody")) == ()

    # Once the appointment is in the past it is no longer "upcoming".
    clock.moment = mine.start + timedelta(minutes=1)
    assert await service.list_appointments(TENANT, RAMI) == ()


async def test_list_is_capped_and_sorted():
    service, _ = build(limits=Limits(appointments_listed=2))
    slots = await karim_slots(service)
    for index, slot in enumerate(slots[:3]):
        await service.create_hold(TENANT, RAMI, slot.slot_id, idempotency_key=f"h{index}")
        await service.create_appointment(
            TENANT, RAMI, f"hold_{index + 1}", "Rami Khoury", idempotency_key=f"b{index}"
        )

    listed = await service.list_appointments(TENANT, RAMI)

    assert len(listed) == 2
    assert [appointment.start for appointment in listed] == [slots[0].start, slots[1].start]


async def test_an_unknown_tenant_with_no_default_is_not_found():
    """The catalogue's rule, unchanged: serving somebody else's clinic is hard rule
    4's mistake."""
    clock = Clock()
    catalogue = FakeBookingClient(clinics={TENANT: DEMO_CLINIC}, clock=clock)
    service, _ = build(clock, catalogue=catalogue)

    with pytest.raises(BookingError) as raised:
        await service.list_appointments("nobody", RAMI)

    assert raised.value.code == "NOT_FOUND"


async def test_each_tenant_has_its_own_state():
    """Including two tenants that differ only in case: decision D1 says a tenant id
    is opaque and never case-folded, so these are two clinics."""
    service, _ = build()
    lower = await karim_slots(service, "clinic-alpha")
    upper = await karim_slots(service, "Clinic-Alpha")

    await service.create_hold("clinic-alpha", RAMI, lower[0].slot_id, idempotency_key="k1")

    # The same underlying 14:00, held in one clinic, still free in the other.
    still_free = {slot.slot_id for slot in await karim_slots(service, "Clinic-Alpha")}
    assert upper[0].slot_id in still_free
    assert await service.list_appointments("Clinic-Alpha", RAMI) == ()


@pytest.mark.parametrize(
    ("failure", "applied", "code"),
    [
        ("SLOT_TAKEN", False, "SLOT_TAKEN"),
        ("HOLD_EXPIRED", False, "HOLD_EXPIRED"),
        ("NOT_FOUND", False, "NOT_FOUND"),
        ("VALIDATION", False, "VALIDATION"),
        ("UNAVAILABLE", False, "UNAVAILABLE"),
        ("IDEMPOTENCY_CONFLICT", False, "IDEMPOTENCY_CONFLICT"),
        ("UNKNOWN_BEFORE", False, "UNKNOWN_OUTCOME"),
        ("UNKNOWN_AFTER", True, "UNKNOWN_OUTCOME"),
    ],
)
async def test_scripted_failures_apply_or_not_as_documented(failure, applied, code):
    """The table in plan section 5.3, one row at a time.

    `UNKNOWN_AFTER` is the row the whole of V6 exists for: the change DID happen
    and the answer was lost. A patient must be told neither "booked" nor "not
    booked", and a human must be able to find the request - which is why the change
    is applied and remembered under its key.
    """
    failures = FailureScript()
    service, _ = build(failures=failures)
    slots = await karim_slots(service)
    failures.push("create_hold", failure)

    with pytest.raises(BookingError) as raised:
        await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")

    assert raised.value.code == code
    taken = slots[0].slot_id not in {slot.slot_id for slot in await karim_slots(service)}
    assert taken is applied


async def test_an_unknown_outcome_after_applying_is_replayable_under_the_same_key():
    """Why `UNKNOWN_AFTER` is remembered: the retry our worker makes gets the hold
    that already exists, rather than a second one."""
    failures = FailureScript()
    service, _ = build(failures=failures)
    slots = await karim_slots(service)
    failures.push("create_hold", "UNKNOWN_AFTER")

    with pytest.raises(BookingError):
        await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")

    retry = await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    assert retry.hold_id == "hold_1"


async def test_a_failure_that_changed_nothing_is_not_remembered():
    """`UNAVAILABLE` and the two unknown-before failures leave no replay entry: the
    request never reached a decision, so a retry deserves a real attempt."""
    failures = FailureScript()
    service, _ = build(failures=failures)
    slots = await karim_slots(service)
    failures.push("create_hold", "UNAVAILABLE")

    with pytest.raises(BookingError):
        await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")

    hold = await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    assert hold.hold_id == "hold_1"


@pytest.mark.parametrize("failure", ["HANG_BEFORE", "HANG_AFTER"])
async def test_a_hang_waits_outside_the_lock_so_another_call_proceeds(failure):
    """The hang is what drives the turn deadline into a write in flight.

    It waits OUTSIDE the lock, which this proves: while one call hangs, a second
    call on the same service completes. If the hang were inside the lock, every
    other job in the worker process would freeze with it.
    """
    import asyncio

    failures = FailureScript()
    service, _ = build(failures=failures)
    slots = await karim_slots(service)
    failures.push("create_hold", failure)

    hanging = asyncio.create_task(
        service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    )
    await asyncio.sleep(0)
    assert not hanging.done()

    other = await service.create_hold(TENANT, ZEINA, slots[1].slot_id, idempotency_key="z1")
    assert other.hold_id in {"hold_1", "hold_2"}

    failures.release()
    result = await hanging
    assert result.slot_id == slots[0].slot_id


async def test_a_hang_can_be_cancelled_by_a_deadline():
    """What the turn deadline actually does to a write in flight. Nothing here
    catches `CancelledError`, which is what lets the deadline work at all."""
    import asyncio

    failures = FailureScript()
    service, _ = build(failures=failures)
    slots = await karim_slots(service)
    failures.push("create_hold", "HANG_BEFORE")

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")

    # HANG_BEFORE hangs before applying, so nothing was held.
    assert slots[0].slot_id in {slot.slot_id for slot in await karim_slots(service)}


async def test_reads_never_raise_unknown_outcome():
    """V6's read/write rule, made unscriptable. A read that failed changed nothing,
    so "the answer is unknown" is not a state a read can be in."""
    failures = FailureScript()
    for operation in ("get_clinic", "list_doctors", "search_slots", "list_appointments"):
        for failure in ("UNKNOWN_BEFORE", "UNKNOWN_AFTER", "SLOT_TAKEN", "HANG_AFTER"):
            with pytest.raises(ValueError):
                failures.push(operation, failure)
        failures.push(operation, "UNAVAILABLE")  # allowed

    service, _ = build(failures=failures)
    for call in (
        service.get_clinic(TENANT),
        service.list_doctors(TENANT),
        service.list_appointments(TENANT, RAMI),
    ):
        with pytest.raises(BookingError) as raised:
            await call
        assert raised.value.code == "UNAVAILABLE"


async def test_the_failure_script_is_bounded():
    failures = FailureScript()
    for _ in range(32):
        failures.push("create_hold", "SLOT_TAKEN")

    with pytest.raises(ValueError):
        failures.push("create_hold", "SLOT_TAKEN")


async def test_a_new_tenant_beyond_the_limit_is_unavailable():
    """Nothing here can be evicted - another tenant's holds are not ours to drop -
    so refusing is the honest answer."""
    service, _ = build(limits=Limits(tenants=1))
    await karim_slots(service, TENANT)

    with pytest.raises(BookingError) as raised:
        await karim_slots(service, OTHER_TENANT)

    assert raised.value.code == "UNAVAILABLE"


async def test_every_map_is_bounded_and_evicts_before_refusing():
    """A worker runs for weeks, so an unbounded dict is a memory leak with a polite
    name. Eviction first (what no longer matters), then `UNAVAILABLE`."""
    service, clock = build(limits=Limits(holds_per_tenant=1, slot_tokens_per_tenant=2))
    slots = await karim_slots(service)

    # tokens: only the two most recently issued survive.
    assert len(service._tenants[TENANT].tokens) == 2  # noqa: SLF001

    await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[3].slot_id, idempotency_key="k1")
    assert len(service._tenants[TENANT].holds) == 1  # noqa: SLF001

    # An ACTIVE hold cannot be evicted, so a second patient's hold is refused.
    with pytest.raises(BookingError) as raised:
        await service.create_hold(TENANT, ZEINA, slots[2].slot_id, idempotency_key="z1")
    assert raised.value.code == "UNAVAILABLE"

    # Once the first hold lapses it is no longer worth keeping, and there is room.
    clock.advance(minutes=11)
    await karim_slots(service)
    hold = await service.create_hold(TENANT, ZEINA, slots[2].slot_id, idempotency_key="z2")
    assert hold.hold_id == "hold_2"


async def test_the_replay_store_forgets_entries_older_than_its_ttl():
    service, clock = build(limits=Limits(replay_ttl=timedelta(hours=1)))
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")

    clock.advance(hours=2)
    await karim_slots(service)  # the hold lapsed; the slot is free again

    # The key is forgotten, so this is a real attempt rather than a replay - and it
    # produces a NEW hold.
    hold = await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    assert hold.hold_id == "hold_2"


async def test_twenty_concurrent_holds_on_one_slot_give_one_hold():
    """arq runs several jobs at once in one process, sharing one instance. One lock
    guards every read-modify-write, so the race has exactly one winner."""
    import asyncio

    service, _ = build()
    slots = await karim_slots(service)
    patients = [PatientRef(f"contact-{index}") for index in range(20)]

    results = await asyncio.gather(
        *(
            service.create_hold(TENANT, patient, slots[0].slot_id, idempotency_key=f"k{index}")
            for index, patient in enumerate(patients)
        ),
        return_exceptions=True,
    )

    held = [result for result in results if not isinstance(result, BaseException)]
    taken = [
        result
        for result in results
        if isinstance(result, BookingError) and result.code == "SLOT_TAKEN"
    ]
    assert len(held) == 1
    assert len(taken) == 19


async def test_the_frozen_catalogue_is_never_mutated():
    """The fake stays frozen, stateless and read-only (plan conflict C2). If this
    service ever wrote into it, `test_the_fake_keeps_no_per_call_state` would start
    failing in a way that looked unrelated."""
    clock = Clock()
    catalogue = FakeBookingClient.demo(clock=clock)
    before = repr(vars(catalogue))
    before_starts = [dict(entry.starts) for entry in DEMO_CLINIC.doctors]
    service = InMemoryBookingService(catalogue, clock, id_secret=SECRET, new_id=counter_ids())

    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    await service.create_appointment(TENANT, RAMI, "hold_1", "Rami Khoury", idempotency_key="k2")
    await service.create_hold(TENANT, RAMI, slots[1].slot_id, idempotency_key="k3")
    await service.reschedule_appointment(TENANT, RAMI, "apt_1", "hold_2", idempotency_key="k4")
    await service.cancel_appointment(TENANT, RAMI, "apt_1", idempotency_key="k5")

    assert repr(vars(catalogue)) == before
    assert [dict(entry.starts) for entry in DEMO_CLINIC.doctors] == before_starts
    assert isinstance(catalogue.default, FakeClinic)


async def test_the_service_keeps_no_name():
    """Hard rule 8. `full_name` enters the body hash and is discarded: a stand-in
    for the Booking Service is still a place a patient's name must not accumulate."""
    service, _ = build()
    slots = await karim_slots(service)
    await service.create_hold(TENANT, RAMI, slots[0].slot_id, idempotency_key="k1")
    await service.create_appointment(TENANT, RAMI, "hold_1", "SENTINELNAME", idempotency_key="k2")

    state = service._tenants[TENANT]  # noqa: SLF001
    records = [*state.holds.values(), *state.appointments.values(), *state.replays.values()]
    assert records  # there is something to check
    for record in records:
        assert "SENTINELNAME" not in repr(vars(record))
    assert "SENTINELNAME" not in repr(state)


async def test_the_service_repr_shows_only_a_count():
    """A repr that listed holds would put doctor names and appointment times into
    any traceback that printed it."""
    service, _ = build()
    await karim_slots(service)

    assert repr(service) == "InMemoryBookingService(tenants=1)"
