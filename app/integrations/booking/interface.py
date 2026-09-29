"""The Booking Service, behind an interface (`docs/booking-contract.md`).

Like `ChatClient` and `TenantResolver`: the agent's tools depend on this
Protocol, and the HTTP client sits behind it in one module (VS-011). Until the
Booking Service exists, `FakeBookingClient` implements the same interface with
seeded in-memory data, so this slice is not blocked on it.

This module imports no HTTP stack and no SDK. That is what lets `app/agent/`
import it (hard rule 3: the model can only reach a tool, and a tool can only
reach this).

**The shapes below are a PROPOSAL.** `docs/booking-contract.md` lists endpoints
but no response bodies for `/clinic`, `/doctors` or `/slots`, so the DTOs here
are this repo's suggestion, to be agreed with the Booking Service owner (plan
conflict C4a, recorded in the contract under "Proposal").
"""

from datetime import datetime, time
from typing import Protocol, runtime_checkable

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.tenants.ids import TenantId

# Frozen, and extra="ignore" on purpose. Frozen because one client instance is
# shared by every concurrently running job (arq runs several in one process), so
# anything reachable from it must be immutable. extra="ignore" because VS-011
# parses these straight from the Booking Service's JSON, and a service that adds
# a field must not break this one.
_DTO = ConfigDict(frozen=True, extra="ignore")


class Location(BaseModel):
    model_config = _DTO

    name: str
    address: str


class OpeningHours(BaseModel):
    """One weekday's hours. `opens is None` means the clinic is closed."""

    model_config = _DTO

    weekday: int  # 0 = Monday, matching datetime.date.weekday()
    opens: time | None = None
    closes: time | None = None

    @property
    def closed(self) -> bool:
        return self.opens is None or self.closes is None


class Service(BaseModel):
    model_config = _DTO

    service_id: str
    name: str
    duration_minutes: int


class Doctor(BaseModel):
    model_config = _DTO

    doctor_id: str
    name: str
    specialty: str
    services: tuple[Service, ...] = ()


class ClinicInfo(BaseModel):
    model_config = _DTO

    name: str
    # An IANA name, from the clinic. `docs/booking-contract.md` open question 5
    # is unanswered, so carrying it explicitly is cheaper than assuming
    # Asia/Beirut forever - see Follow-up 3.
    timezone: str
    locations: tuple[Location, ...] = ()
    opening_hours: tuple[OpeningHours, ...] = ()
    policies: tuple[str, ...] = ()
    # Q7: the demo clinic carries none, and the prompt says facts come only from
    # tool results, so in practice the model states no price. The field exists
    # because the contract's /clinic mentions pricing info.
    pricing: tuple[str, ...] = ()


class Slot(BaseModel):
    model_config = _DTO

    slot_id: str
    doctor_id: str
    # Aware, always. A naive datetime here would be an hour wrong twice a year
    # and silently right the rest of the time, which is the worst failure mode
    # available. On the wire this is ISO 8601 with an offset.
    start: AwareDatetime
    end: AwareDatetime
    service_id: str | None = None


class BookingError(Exception):
    """A Booking Service failure, as a CODE and nothing else.

    `docs/booking-contract.md` gives error bodies as
    `{"error": {"code": ..., "message": ...}}`. The `message` is NEVER read: it
    comes from another system and can quote clinic or patient data, and this
    exception is logged and turned into a tool result (hard rule 8).

    Three codes in VS-006, mapping to the contract's read-side errors:
    404 -> NOT_FOUND, 422 -> VALIDATION, 5xx -> UNAVAILABLE. 409 SLOT_TAKEN and
    410 HOLD_EXPIRED belong to VS-007, where something can actually book.
    """

    CODES = ("NOT_FOUND", "VALIDATION", "UNAVAILABLE")

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            raise ValueError(f"unknown booking error code: {code!r}")
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return f"BookingError({self.code!r})"


@runtime_checkable
class BookingClient(Protocol):
    """What the agent's tools are allowed to know about the Booking Service.

    Read-only in VS-006. `tenant_id` is ALWAYS our resolved tenant, passed by
    our code from `ToolContext` - never a value the model supplied (hard rule
    4). It is the first parameter of every method precisely so that a call
    without one does not type-check and does not run.

    Every method raises `BookingError` for a service-side failure, never a
    transport exception: "what kind of failure was this" has one home, exactly
    as with `ChatClient`.
    """

    async def get_clinic(self, tenant_id: TenantId) -> ClinicInfo:
        """The clinic's name, timezone, locations, hours and policies."""
        ...

    async def list_doctors(self, tenant_id: TenantId) -> tuple[Doctor, ...]:
        """Every doctor, with the services each offers."""
        ...

    async def search_slots(
        self,
        tenant_id: TenantId,
        doctor_id: str,
        start: datetime,
        end: datetime,
        service_id: str | None = None,
    ) -> tuple[Slot, ...]:
        """Available slots for one doctor in `[start, end)`.

        `start` and `end` must be AWARE. A naive value is `VALIDATION`, not a
        guess about which zone was meant.

        `service_id` is accepted because `GET /slots` takes one (plan conflict
        C4b). No tool exposes it in VS-006 - the tool signature the brief fixes
        has no service - so the fake ignores it. Follow-up 2.
        """
        ...


__all__ = [
    "BookingClient",
    "BookingError",
    "ClinicInfo",
    "Doctor",
    "Location",
    "OpeningHours",
    "Service",
    "Slot",
]
