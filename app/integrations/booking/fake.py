"""An in-memory Booking Service with a clearly synthetic demo clinic.

`docs/booking-contract.md`: "Until it exists, FakeBookingClient implements the
same interface with seeded in-memory data, so work is not blocked."

Two properties are load-bearing and easy to lose:

**It is immutable and keeps no per-call state.** arq runs several jobs
concurrently in one process and they share one instance. A call log or a counter
would be both a race and a memory leak in a worker that runs for weeks. Tests
that need to see the calls wrap it in `tests/integrations/booking_fakes.py::
RecordingBooking` instead.

**Its data is obviously fake** (hard rule 8 forbids fixtures built from real
data, and hard rule 5's neighbourhood - fake availability reaching a real
patient - is the risk this slice actually carries). The clinic is "Doctoleb Demo
Clinic" on a street that does not exist, and the worker logs a loud warning at
startup saying so.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.integrations.booking.interface import (
    BookingError,
    ClinicInfo,
    Doctor,
    Location,
    OpeningHours,
    Service,
    Slot,
)
from app.tenants.ids import TenantId

# A weekly pattern: weekday (0 = Monday) -> the local clock times a doctor's
# appointments can start on that day. A pattern rather than a list of dates, so
# the fake answers any window without being reseeded.
WeeklyStarts = Mapping[int, tuple[time, ...]]


@dataclass(frozen=True)
class FakeDoctor:
    doctor: Doctor
    starts: WeeklyStarts
    slot_minutes: int


@dataclass(frozen=True)
class FakeClinic:
    """One tenant's data. Frozen: see the module docstring."""

    info: ClinicInfo
    doctors: tuple[FakeDoctor, ...] = ()

    def find(self, doctor_id: str) -> FakeDoctor:
        for candidate in self.doctors:
            if candidate.doctor.doctor_id == doctor_id:
                return candidate
        raise BookingError("NOT_FOUND")


def _hours(*, weekdays: range | tuple[int, ...], opens: str, closes: str) -> list[OpeningHours]:
    return [
        OpeningHours(
            weekday=day, opens=time.fromisoformat(opens), closes=time.fromisoformat(closes)
        )
        for day in weekdays
    ]


def _times(*values: str) -> tuple[time, ...]:
    return tuple(time.fromisoformat(value) for value in values)


DEMO_CLINIC = FakeClinic(
    info=ClinicInfo(
        name="Doctoleb Demo Clinic",
        timezone="Asia/Beirut",
        # A fictional address. Nothing here may be traceable to a real clinic.
        locations=(Location(name="Main branch", address="1 Demo Street, Beirut"),),
        opening_hours=(
            *_hours(weekdays=range(0, 5), opens="09:00", closes="17:00"),  # Mon-Fri
            *_hours(weekdays=(5,), opens="09:00", closes="13:00"),  # Sat
            OpeningHours(weekday=6),  # Sunday: closed
        ),
        policies=(
            "Please arrive 10 minutes before your appointment.",
            "Please tell us at least 24 hours ahead if you cannot come.",
        ),
        # No prices (Q7). The prompt says facts come only from tool results, so
        # with none here the model states none.
        pricing=(),
    ),
    doctors=(
        FakeDoctor(
            doctor=Doctor(
                doctor_id="doc_karim",
                name="Dr. Karim Haddad",
                specialty="General practice",
                services=(
                    Service(
                        service_id="svc_consultation", name="Consultation", duration_minutes=20
                    ),
                    Service(service_id="svc_followup", name="Follow-up visit", duration_minutes=20),
                ),
            ),
            starts={
                # Mon/Wed/Fri. The afternoon four (14:00, 14:20, 15:40, 16:20)
                # are what Task 9's acceptance test asserts.
                0: _times("09:00", "09:40", "11:20", "14:00", "14:20", "15:40", "16:20"),
                2: _times("09:00", "09:40", "11:20", "14:00", "14:20", "15:40", "16:20"),
                4: _times("09:00", "09:40", "11:20", "14:00", "14:20", "15:40", "16:20"),
                1: _times("10:00", "10:20", "13:00", "15:00"),  # Tue
                3: _times("10:00", "10:20", "13:00", "15:00"),  # Thu
                5: _times("09:20", "10:40", "12:00"),  # Sat
            },
            slot_minutes=20,
        ),
        FakeDoctor(
            doctor=Doctor(
                doctor_id="doc_rania",
                name="Dr. Rania Khoury",
                specialty="Dermatology",
                services=(
                    Service(service_id="svc_skin", name="Skin consultation", duration_minutes=30),
                ),
            ),
            starts={1: _times("14:00", "14:30", "16:00"), 3: _times("14:00", "14:30", "16:00")},
            slot_minutes=30,
        ),
        FakeDoctor(
            doctor=Doctor(
                doctor_id="doc_samir",
                name="Dr. Samir Nassar",
                specialty="Pediatrics",
                services=(
                    Service(service_id="svc_child", name="Child check-up", duration_minutes=20),
                ),
            ),
            starts={
                0: _times("09:00", "09:20", "10:00", "11:40"),
                2: _times("09:00", "09:20", "10:00", "11:40"),
                4: _times("09:00", "09:20", "10:00", "11:40"),
            },
            slot_minutes=20,
        ),
    ),
)


@dataclass(frozen=True)
class FakeBookingClient:
    """`BookingClient` over seeded in-memory data. Stateless and immutable.

    `clinics` maps a tenant to its data. An unknown tenant with no `default`
    raises `NOT_FOUND` rather than serving somebody else's clinic: that is hard
    rule 4's mistake, and a fake that gets it wrong teaches the wrong shape to
    the real client in VS-011.

    `clock` returns an aware UTC datetime and is injected so tests can freeze
    it. A slot in the past is never returned, and "the past" has to mean
    something a test can pin.
    """

    clinics: Mapping[TenantId, FakeClinic]
    clock: Callable[[], datetime]
    default: FakeClinic | None = field(default=None)

    @classmethod
    def demo(cls, clock: Callable[[], datetime]) -> "FakeBookingClient":
        """The demo clinic, for every tenant. What the worker runs on today."""
        return cls(clinics={}, clock=clock, default=DEMO_CLINIC)

    def _clinic(self, tenant_id: TenantId) -> FakeClinic:
        # Exact, case-sensitive lookup (decision D1, Q2). Never .lower(): two
        # spellings are two tenants.
        clinic = self.clinics.get(tenant_id, self.default)
        if clinic is None:
            raise BookingError("NOT_FOUND")
        return clinic

    async def get_clinic(self, tenant_id: TenantId) -> ClinicInfo:
        return self._clinic(tenant_id).info

    async def list_doctors(self, tenant_id: TenantId) -> tuple[Doctor, ...]:
        return tuple(entry.doctor for entry in self._clinic(tenant_id).doctors)

    async def search_slots(
        self,
        tenant_id: TenantId,
        doctor_id: str,
        start: datetime,
        end: datetime,
        service_id: str | None = None,
    ) -> tuple[Slot, ...]:
        """Slots in `[start, end)` that have not already passed.

        `service_id` is accepted and ignored (plan conflict C4b).
        """
        if start.tzinfo is None or end.tzinfo is None or end <= start:
            raise BookingError("VALIDATION")
        clinic = self._clinic(tenant_id)
        entry = clinic.find(doctor_id)
        # The clinic's OWN zone, not a module constant: every day then gets its
        # own UTC offset, which is what makes a window spanning a DST change
        # come out right. It also keeps this module independent of app/agent/.
        zone = ZoneInfo(clinic.info.timezone)
        now = self.clock()

        slots: list[Slot] = []
        for day in _local_dates(start, end, zone):
            for local_time in entry.starts.get(day.weekday(), ()):
                # Built from a naive local datetime and the clinic's zone, so
                # the offset is the one in force on THAT date.
                slot_start = datetime.combine(day, local_time).replace(tzinfo=zone)
                if not (start <= slot_start < end) or slot_start < now:
                    continue
                slot_end = slot_start + timedelta(minutes=entry.slot_minutes)
                slots.append(
                    Slot(
                        # Deterministic, and built from the UTC instant so two
                        # local spellings of the repeated autumn hour cannot
                        # collide.
                        slot_id=f"{doctor_id}:{slot_start.astimezone(UTC).isoformat()}",
                        doctor_id=doctor_id,
                        start=slot_start,
                        end=slot_end,
                    )
                )
        return tuple(sorted(slots, key=lambda slot: slot.start))


def _local_dates(start: datetime, end: datetime, zone: ZoneInfo) -> list:
    """Every local calendar date the window touches, inclusive at both ends.

    Both ends inclusive because a slot's local date is decided by the clinic's
    zone, and `end` can land on the next local day even when `start` did not.
    """
    first = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
