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

from app.integrations.booking import BookingClient, ClinicInfo, Doctor, Slot
from app.tenants.ids import TenantId


@dataclass(frozen=True)
class BookingCall:
    """One call, recorded. Tests assert on these instead of on the fake."""

    method: str
    tenant_id: TenantId
    kwargs: dict[str, Any] = field(default_factory=dict)


class RecordingBooking:
    """A spy around a real `BookingClient`.

    `hook` runs BEFORE the wrapped call and is how a test makes something happen
    while a tool is in flight - a staff takeover, say, which is the only way to
    prove hard rule 7 holds across a tool call rather than only across the model
    call.

    `raises` injects a failure: a `BookingError` to exercise the tool's error
    mapping, or any other exception to exercise the crash path.
    """

    def __init__(
        self,
        inner: BookingClient,
        *,
        hook: Callable[[], Awaitable[None]] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.inner = inner
        self.calls: list[BookingCall] = []
        self._hook = hook
        self._raises = raises

    async def _record(self, method: str, tenant_id: TenantId, **kwargs: Any) -> None:
        self.calls.append(BookingCall(method, tenant_id, kwargs))
        if self._hook is not None:
            await self._hook()
        if self._raises is not None:
            raise self._raises

    @property
    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    @property
    def tenants(self) -> list[TenantId]:
        return [call.tenant_id for call in self.calls]

    async def get_clinic(self, tenant_id: TenantId) -> ClinicInfo:
        await self._record("get_clinic", tenant_id)
        return await self.inner.get_clinic(tenant_id)

    async def list_doctors(self, tenant_id: TenantId) -> tuple[Doctor, ...]:
        await self._record("list_doctors", tenant_id)
        return await self.inner.list_doctors(tenant_id)

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
        return await self.inner.search_slots(tenant_id, doctor_id, start, end, service_id)
