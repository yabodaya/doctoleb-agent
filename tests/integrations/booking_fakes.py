"""Test doubles that wrap a `BookingClient`.

`FakeBookingClient` deliberately keeps no call log: it is shared by every
concurrently running job, so a growing list would be both a race and a leak.
Everything a test needs to SEE, or to make happen mid-call, lives here instead -
outside `app/`, where it can be as stateful as it likes.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.integrations.booking import (
    Appointment,
    BookingClient,
    ClinicInfo,
    Doctor,
    Hold,
    PatientBookingClient,
    PatientRef,
    Slot,
)
from app.tenants.ids import TenantId


@dataclass(frozen=True)
class BookingCall:
    """One call, recorded. Tests assert on these instead of on the fake.

    `kwargs` may hold a `PatientRef`, which is safe: its own repr hides its value,
    so even a failed assertion that prints the whole call list reveals nothing
    (hard rule 8). It never holds a `full_name`, because no recorded call passes
    one by keyword - see `create_appointment` below.
    """

    method: str
    tenant_id: TenantId
    kwargs: dict[str, Any] = field(default_factory=dict)


class RecordingBooking:
    """A spy around a `BookingClient`, a `PatientBookingClient`, or one object
    that is both (VS-007's `InMemoryBookingService`).

    `hook` runs BEFORE the wrapped call and is how a test makes something happen
    while a tool is in flight - a staff takeover, say, which is the only way to
    prove hard rule 7 holds across a tool call rather than only across the model
    call.

    `after_hook` runs AFTER the wrapped call returned. That is the harder half of
    hard rule 7 for VS-007: the booking already exists at the service, and the
    reply must still be dropped and the outcome still recorded (plan section
    5.12). A test cannot arrange that with `hook`, which fires too early.

    `raises` injects a failure: a `BookingError` to exercise the tool's error
    mapping, or any other exception to exercise the crash path. It fires in place
    of the wrapped call, so `after_hook` does not run.
    """

    def __init__(
        self,
        inner: BookingClient | PatientBookingClient,
        *,
        hook: Callable[[], Awaitable[None]] | None = None,
        after_hook: Callable[[], Awaitable[None]] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.inner: Any = inner
        self.calls: list[BookingCall] = []
        self._hook = hook
        self._after_hook = after_hook
        self._raises = raises

    async def _record(self, method: str, tenant_id: TenantId, **kwargs: Any) -> None:
        self.calls.append(BookingCall(method, tenant_id, kwargs))
        if self._hook is not None:
            await self._hook()
        if self._raises is not None:
            raise self._raises

    async def _after(self) -> None:
        if self._after_hook is not None:
            await self._after_hook()

    @property
    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    @property
    def tenants(self) -> list[TenantId]:
        return [call.tenant_id for call in self.calls]

    @property
    def keys(self) -> list[str | None]:
        """The idempotency key of each recorded call, `None` for a read.

        How a test proves hard rule 6: the same intent retried carries the same
        key, and a different intent a different one.
        """
        return [call.kwargs.get("idempotency_key") for call in self.calls]

    async def get_clinic(self, tenant_id: TenantId) -> ClinicInfo:
        await self._record("get_clinic", tenant_id)
        result = await self.inner.get_clinic(tenant_id)
        await self._after()
        return result

    async def list_doctors(self, tenant_id: TenantId) -> tuple[Doctor, ...]:
        await self._record("list_doctors", tenant_id)
        result = await self.inner.list_doctors(tenant_id)
        await self._after()
        return result

    async def search_slots(
        self,
        tenant_id: TenantId,
        doctor_id: str,
        start: datetime,
        end: datetime,
        service_id: str | None = None,
    ) -> tuple[Slot, ...]:
        await self._record(
            "search_slots",
            tenant_id,
            doctor_id=doctor_id,
            start=start,
            end=end,
            service_id=service_id,
        )
        result = await self.inner.search_slots(tenant_id, doctor_id, start, end, service_id)
        await self._after()
        return result

    # --- the patient side (VS-007) ------------------------------------------

    async def list_appointments(
        self, tenant_id: TenantId, patient: PatientRef
    ) -> tuple[Appointment, ...]:
        await self._record("list_appointments", tenant_id, patient=patient)
        result = await self.inner.list_appointments(tenant_id, patient)
        await self._after()
        return result

    async def create_hold(
        self, tenant_id: TenantId, patient: PatientRef, slot_id: str, *, idempotency_key: str
    ) -> Hold:
        await self._record(
            "create_hold",
            tenant_id,
            patient=patient,
            slot_id=slot_id,
            idempotency_key=idempotency_key,
        )
        result = await self.inner.create_hold(
            tenant_id, patient, slot_id, idempotency_key=idempotency_key
        )
        await self._after()
        return result

    async def create_appointment(
        self,
        tenant_id: TenantId,
        patient: PatientRef,
        hold_id: str,
        full_name: str,
        *,
        idempotency_key: str,
    ) -> Appointment:
        # `full_name` is recorded as its LENGTH, never its text. A test needs to
        # know a name was passed and that it arrived normalised; it does not need
        # the name, and an assertion that failed while printing this list would
        # otherwise put a patient's name in the output (hard rule 8).
        await self._record(
            "create_appointment",
            tenant_id,
            patient=patient,
            hold_id=hold_id,
            full_name_chars=len(full_name),
            idempotency_key=idempotency_key,
        )
        result = await self.inner.create_appointment(
            tenant_id, patient, hold_id, full_name, idempotency_key=idempotency_key
        )
        await self._after()
        return result

    async def reschedule_appointment(
        self,
        tenant_id: TenantId,
        patient: PatientRef,
        appointment_id: str,
        new_hold_id: str,
        *,
        idempotency_key: str,
    ) -> Appointment:
        await self._record(
            "reschedule_appointment",
            tenant_id,
            patient=patient,
            appointment_id=appointment_id,
            new_hold_id=new_hold_id,
            idempotency_key=idempotency_key,
        )
        result = await self.inner.reschedule_appointment(
            tenant_id, patient, appointment_id, new_hold_id, idempotency_key=idempotency_key
        )
        await self._after()
        return result

    async def cancel_appointment(
        self, tenant_id: TenantId, patient: PatientRef, appointment_id: str, *, idempotency_key: str
    ) -> Appointment:
        await self._record(
            "cancel_appointment",
            tenant_id,
            patient=patient,
            appointment_id=appointment_id,
            idempotency_key=idempotency_key,
        )
        result = await self.inner.cancel_appointment(
            tenant_id, patient, appointment_id, idempotency_key=idempotency_key
        )
        await self._after()
        return result
