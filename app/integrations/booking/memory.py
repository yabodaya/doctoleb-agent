"""A stateful in-memory Booking Service, beside the frozen fake (plan section 5.3).

`FakeBookingClient` is deliberately **stateless, immutable and read-only**, and
three tests pin it that way. The write side needs the opposite: a hold has to
still exist when the patient answers two minutes later. So the write side lives
here, in a separate class that *wraps* the fake for its catalogue (plan conflict
C2 and decision V8).

**This module is NOT re-exported by the package `__init__`,** exactly like
`fake.py`. The worker imports it by its full path, where the choice of a demo
backend is visible, and `app/agent/` may not import it at all (hard rule 3): the
Agent Core receives a `BookingClient` and a `PatientBookingClient` as Protocols and
must not be able to reach for demo data.

**What sharing one instance means.** The worker builds ONE instance in `startup()`,
inside arq's running loop (an `asyncio.Lock` binds to the loop it is first
contended in), and every job that process runs at once shares it. One lock guards
every read-modify-write, and nothing awaits I/O while holding it, so state changes
take microseconds. Three consequences the worker's startup warning has to say out
loud:

- **everything is lost on a worker restart** - holds, appointments and the replay
  store. `booking_actions` rows then point at holds this service no longer knows,
  and `create_appointment` answers `NOT_FOUND`, which the tool reports to the
  patient as "the hold ran out; let's search again";
- **several worker processes would each have their own state**, so the same patient
  could see different availability from one message to the next. Run exactly one
  worker while this is in use;
- **the data is demo data.** Fake availability reaching a real patient is the real
  danger, unchanged from VS-006.

**Opaque ids (V10).** The fake's slot ids are transparent
(`doc_karim:2026-09-30T11:00:00+00:00`) and a test pins that. The model may not be
given ids it could construct or guess, so `search_slots` here replaces each raw id
with `slot_` + a keyed hash, and resolves only tokens this instance issued. Hold
ids never reach the model at all.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import unicodedata
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from app.integrations.booking.fake import FakeBookingClient
from app.integrations.booking.interface import (
    Appointment,
    BookingError,
    ClinicInfo,
    Doctor,
    Hold,
    PatientRef,
    Slot,
)
from app.tenants.ids import TenantId

# Contract open question 4. Ten minutes is long enough for a WhatsApp round trip
# and short enough that an abandoned conversation does not hold a slot all day.
HOLD_TTL = timedelta(minutes=10)

# The reference code a patient is shown. No I, L, O, 0 or 1: those are the pairs
# people mistype when reading a code back over the phone.
REFERENCE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
REFERENCE_LENGTH = 6

HoldStatus = Literal["ACTIVE", "CONSUMED", "RELEASED", "EXPIRED"]

# Which failures may be scripted on a READ. A read that failed changed nothing, so
# "the answer is unknown" is not a state a read can be in - see BookingError's
# docstring for the read/write rule (V6). Enforced by `FailureScript.push`, so a
# test that tried to script one fails loudly instead of proving the wrong thing.
READ_OPERATIONS = frozenset({"get_clinic", "list_doctors", "search_slots", "list_appointments"})
READ_FAILURES = frozenset({"UNAVAILABLE", "HANG_BEFORE"})

Failure = Literal[
    "SLOT_TAKEN",
    "HOLD_EXPIRED",
    "NOT_FOUND",
    "VALIDATION",
    "UNAVAILABLE",
    "IDEMPOTENCY_CONFLICT",
    "UNKNOWN_BEFORE",
    "UNKNOWN_AFTER",
    "HANG_BEFORE",
    "HANG_AFTER",
]

# Business errors: the service refused, nothing changed, and the answer is worth
# remembering for a replay, because a retry of the same request must get the same
# refusal rather than a second chance.
_BUSINESS: frozenset[str] = frozenset({"SLOT_TAKEN", "HOLD_EXPIRED", "NOT_FOUND", "VALIDATION"})
# Failures that happen before any work and are NOT remembered: the request never
# reached a decision, so a retry deserves a real attempt.
_NOT_APPLIED: frozenset[str] = frozenset({"UNAVAILABLE", "IDEMPOTENCY_CONFLICT", "UNKNOWN_BEFORE"})

MAX_QUEUED_FAILURES = 32


@dataclass(frozen=True)
class Limits:
    """Every map is bounded, because a worker runs for weeks.

    An unbounded dict in a long-lived process is a memory leak with a polite name.
    When a map is full the service first drops what no longer matters, and only
    then refuses the write with `UNAVAILABLE` - which the tool reports as "nothing
    was changed", never as a failure to book.
    """

    tenants: int = 100
    holds_per_tenant: int = 1_000
    appointments_per_tenant: int = 5_000
    replays_per_tenant: int = 5_000
    replay_ttl: timedelta = timedelta(hours=24)
    slot_tokens_per_tenant: int = 10_000
    appointments_listed: int = 20


# A module-level singleton, so the default is not a call in an argument list
# (ruff B008). Frozen, so sharing one instance between services is safe.
DEFAULT_LIMITS = Limits()


class FailureScript:
    """Test-only failure injection. The worker passes none.

    Failures are queued FIFO **per operation**, so a test can say "the next
    `create_appointment` times out" without touching the search that precedes it.

    The two `HANG_*` failures wait on an `asyncio.Event` created lazily **in the
    running loop** (an Event built at import time would bind to the wrong loop) and
    awaited **outside** the service's lock. That is what makes them useful: a hang
    is how a test drives the turn deadline into a write in flight, and it must not
    freeze every other job in the process while it does.
    """

    def __init__(self) -> None:
        self._queued: dict[str, deque[Failure]] = {}
        self._event: asyncio.Event | None = None
        self._released = False

    def push(self, operation: str, failure: Failure) -> None:
        if operation in READ_OPERATIONS and failure not in READ_FAILURES:
            raise ValueError("a read can only be made to be unavailable or to hang")
        queue = self._queued.setdefault(operation, deque())
        if sum(len(each) for each in self._queued.values()) >= MAX_QUEUED_FAILURES:
            raise ValueError("too many queued failures")
        queue.append(failure)

    def take(self, operation: str) -> Failure | None:
        queue = self._queued.get(operation)
        return queue.popleft() if queue else None

    def release(self) -> None:
        """End every hang, now and later."""
        self._released = True
        if self._event is not None:
            self._event.set()

    async def wait(self) -> None:
        if self._released:
            return
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()

    @property
    def pending(self) -> int:
        return sum(len(each) for each in self._queued.values())


@dataclass
class _Hold:
    hold_id: str
    patient: PatientRef
    slot: Slot  # the RAW slot, with the catalogue's transparent id
    token: str  # the opaque id the model was given
    doctor_name: str
    expires_at: datetime
    status: HoldStatus = "ACTIVE"
    appointment_id: str | None = None


@dataclass
class _Appointment:
    appointment_id: str
    reference: str
    patient: PatientRef
    slot: Slot
    doctor_name: str
    status: Literal["CONFIRMED", "PENDING_APPROVAL", "CANCELLED"] = "CONFIRMED"


@dataclass
class _Replay:
    """One remembered answer. `answer` is the DTO we returned, or `None` when what
    we returned was a business error, whose code is kept instead.

    Nothing here holds the patient's name: only its hash, inside `body_hash`.
    """

    body_hash: str
    at: datetime
    answer: Hold | Appointment | None = None
    error_code: str | None = None


@dataclass
class _TenantState:
    holds: dict[str, _Hold] = field(default_factory=dict)
    appointments: dict[str, _Appointment] = field(default_factory=dict)
    # raw slot id -> ("hold" | "appointment", its id). One slot, one owner.
    occupied: dict[str, tuple[str, str]] = field(default_factory=dict)
    # Both are LRU caches: an OrderedDict with move_to_end on every touch.
    replays: "OrderedDict[str, _Replay]" = field(default_factory=OrderedDict)
    tokens: "OrderedDict[str, Slot]" = field(default_factory=OrderedDict)


def _body_hash(operation: str, body: Mapping[str, str]) -> str:
    """A hash of the operation and every argument except the key.

    The real service would compare request bodies to detect a key reused with a
    different payload. This does the same thing in one line - and it is also where
    `full_name` goes to die: the name enters the hash and is discarded, so this
    fake never keeps a patient's name anywhere (hard rule 8).
    """
    payload = {key: unicodedata.normalize("NFC", value) for key, value in body.items()}
    payload["__operation__"] = operation
    material = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class InMemoryBookingService:
    """Implements `BookingClient` (delegating the catalogue to the frozen fake) and
    `PatientBookingClient` (the write side, held in memory).

    `clock` is injected and returns an aware UTC datetime, so hold expiry is a fact
    a test can pin rather than a race against the wall clock. Message ordering in
    the confirmation gate uses PostgreSQL's clock instead, and the two are never
    compared with each other (plan risk R5).
    """

    def __init__(
        self,
        catalogue: FakeBookingClient,
        clock: Callable[[], datetime],
        *,
        hold_ttl: timedelta = HOLD_TTL,
        limits: Limits = DEFAULT_LIMITS,
        failures: FailureScript | None = None,
        id_secret: bytes | None = None,
        new_id: Callable[[str], str] | None = None,
    ) -> None:
        self._catalogue = catalogue
        self._clock = clock
        self._hold_ttl = hold_ttl
        self._limits = limits
        self._failures = failures
        # Random per instance, so a slot token cannot be guessed and cannot be
        # carried over from another process. Injectable so tests get stable ids.
        self._id_secret = id_secret if id_secret is not None else secrets.token_bytes(32)
        self._new_id = new_id if new_id is not None else _random_id
        self._tenants: dict[TenantId, _TenantState] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def demo(cls, clock: Callable[[], datetime], **options: object) -> "InMemoryBookingService":
        """The demo clinic's catalogue, with an empty booking state."""
        return cls(FakeBookingClient.demo(clock=clock), clock, **options)  # type: ignore[arg-type]

    def __repr__(self) -> str:
        # Counts only. A repr that listed holds would put doctor names and
        # appointment times into any traceback that printed it (hard rule 8).
        return f"InMemoryBookingService(tenants={len(self._tenants)})"

    # --- reads ---------------------------------------------------------------

    async def get_clinic(self, tenant_id: TenantId) -> ClinicInfo:
        await self._read_failure("get_clinic")
        return await self._catalogue.get_clinic(tenant_id)

    async def list_doctors(self, tenant_id: TenantId) -> tuple[Doctor, ...]:
        await self._read_failure("list_doctors")
        return await self._catalogue.list_doctors(tenant_id)

    async def search_slots(
        self,
        tenant_id: TenantId,
        doctor_id: str,
        start: datetime,
        end: datetime,
        service_id: str | None = None,
    ) -> tuple[Slot, ...]:
        """The catalogue's answer, minus what is taken, with opaque ids (V10).

        The catalogue's own `NOT_FOUND` (unknown doctor) and `VALIDATION` (a naive
        or reversed window) rules are unchanged: this only removes slots and
        rewrites ids.
        """
        await self._read_failure("search_slots")
        slots = await self._catalogue.search_slots(tenant_id, doctor_id, start, end, service_id)
        now = self._clock()
        async with self._lock:
            state = self._state(tenant_id)
            self._expire(state, now)
            visible: list[Slot] = []
            for slot in slots:
                if slot.slot_id in state.occupied:
                    continue
                token = self._token(tenant_id, slot.slot_id)
                self._remember_token(state, token, slot)
                visible.append(slot.model_copy(update={"slot_id": token}))
        return tuple(visible)

    async def list_appointments(
        self, tenant_id: TenantId, patient: PatientRef
    ) -> tuple[Appointment, ...]:
        """This patient's upcoming appointments, soonest first.

        At most `Limits.appointments_listed` of them.
        """
        await self._read_failure("list_appointments")
        names = await self._doctor_names(tenant_id)
        now = self._clock()
        async with self._lock:
            state = self._tenants.get(tenant_id)
            if state is None:
                return ()
            self._expire(state, now)
            found = [
                record
                for record in state.appointments.values()
                if record.patient == patient
                and record.status in ("CONFIRMED", "PENDING_APPROVAL")
                and record.slot.start >= now
            ]
            found.sort(key=lambda record: (record.slot.start, record.appointment_id))
            return tuple(
                self._appointment_dto(record, names)
                for record in found[: self._limits.appointments_listed]
            )

    # --- writes --------------------------------------------------------------

    async def create_hold(
        self, tenant_id: TenantId, patient: PatientRef, slot_id: str, *, idempotency_key: str
    ) -> Hold:
        names = await self._doctor_names(tenant_id)

        def apply(state: _TenantState, now: datetime) -> Hold:
            slot = state.tokens.get(slot_id)
            if slot is None:
                # An id this service never issued. The model cannot construct one,
                # so this is either an invented id or one from a previous process.
                raise BookingError("NOT_FOUND")
            state.tokens.move_to_end(slot_id)
            if slot.start < now:
                raise BookingError("NOT_FOUND")

            owner = state.occupied.get(slot.slot_id)
            if owner is not None:
                kind, owner_id = owner
                existing = state.holds.get(owner_id) if kind == "hold" else None
                if (
                    existing is not None
                    and existing.status == "ACTIVE"
                    and existing.patient == patient
                ):
                    # V13: holding a slot this patient already holds returns THAT
                    # hold. A re-run of the same turn must not pile up holds.
                    return self._hold_dto(existing)
                raise BookingError("SLOT_TAKEN")

            # One active hold per patient (a contract proposal): a new hold
            # releases the previous one, so an abandoned choice frees its slot.
            for held in state.holds.values():
                if held.status == "ACTIVE" and held.patient == patient:
                    held.status = "RELEASED"
                    self._free(state, held.slot.slot_id, ("hold", held.hold_id))

            self._room_for_hold(state)
            hold = _Hold(
                hold_id=self._new_id("hold"),
                patient=patient,
                slot=slot,
                token=slot_id,
                doctor_name=names.get(slot.doctor_id, slot.doctor_id),
                expires_at=now + self._hold_ttl,
            )
            state.holds[hold.hold_id] = hold
            state.occupied[slot.slot_id] = ("hold", hold.hold_id)
            return self._hold_dto(hold)

        return await self._write(  # type: ignore[return-value]
            tenant_id,
            "create_hold",
            {"patient_ref": patient.value, "slot_id": slot_id},
            idempotency_key,
            apply,
        )

    async def create_appointment(
        self,
        tenant_id: TenantId,
        patient: PatientRef,
        hold_id: str,
        full_name: str,
        *,
        idempotency_key: str,
    ) -> Appointment:
        names = await self._doctor_names(tenant_id)

        def apply(state: _TenantState, now: datetime) -> Appointment:
            hold = state.holds.get(hold_id)
            if hold is None or hold.patient != patient:
                # Another patient's hold is NOT_FOUND, never 403: a 403 would
                # confirm that it exists to somebody who guessed an id.
                raise BookingError("NOT_FOUND")
            if hold.status == "CONSUMED":
                existing = state.appointments.get(hold.appointment_id or "")
                if existing is not None and existing.status != "CANCELLED":
                    # V13: this hold was already converted. Return that
                    # appointment rather than failing a retry the patient already
                    # got an answer to.
                    return self._appointment_dto(existing, names)
                raise BookingError("HOLD_EXPIRED")
            if hold.status != "ACTIVE":
                raise BookingError("HOLD_EXPIRED")

            self._room_for_appointment(state, now)
            appointment = _Appointment(
                appointment_id=self._new_id("apt"),
                reference=_reference(),
                patient=patient,
                slot=hold.slot,
                doctor_name=hold.doctor_name,
            )
            state.appointments[appointment.appointment_id] = appointment
            hold.status = "CONSUMED"
            hold.appointment_id = appointment.appointment_id
            state.occupied[hold.slot.slot_id] = ("appointment", appointment.appointment_id)
            return self._appointment_dto(appointment, names)

        return await self._write(  # type: ignore[return-value]
            tenant_id,
            "create_appointment",
            {"patient_ref": patient.value, "hold_id": hold_id, "full_name": full_name},
            idempotency_key,
            apply,
        )

    async def reschedule_appointment(
        self,
        tenant_id: TenantId,
        patient: PatientRef,
        appointment_id: str,
        new_hold_id: str,
        *,
        idempotency_key: str,
    ) -> Appointment:
        names = await self._doctor_names(tenant_id)

        def apply(state: _TenantState, now: datetime) -> Appointment:
            appointment = state.appointments.get(appointment_id)
            if (
                appointment is None
                or appointment.patient != patient
                or appointment.status != "CONFIRMED"
                or appointment.slot.start < now
            ):
                raise BookingError("NOT_FOUND")
            hold = state.holds.get(new_hold_id)
            if hold is None or hold.patient != patient:
                raise BookingError("NOT_FOUND")
            if hold.status == "CONSUMED":
                if hold.appointment_id == appointment_id:
                    return self._appointment_dto(appointment, names)  # V13
                raise BookingError("HOLD_EXPIRED")
            if hold.status != "ACTIVE":
                raise BookingError("HOLD_EXPIRED")

            self._free(state, appointment.slot.slot_id, ("appointment", appointment_id))
            # Same appointment_id and same reference: the patient keeps the code
            # they were given (a contract proposal).
            appointment.slot = hold.slot
            appointment.doctor_name = hold.doctor_name
            hold.status = "CONSUMED"
            hold.appointment_id = appointment_id
            state.occupied[hold.slot.slot_id] = ("appointment", appointment_id)
            return self._appointment_dto(appointment, names)

        return await self._write(  # type: ignore[return-value]
            tenant_id,
            "reschedule_appointment",
            {
                "patient_ref": patient.value,
                "appointment_id": appointment_id,
                "new_hold_id": new_hold_id,
            },
            idempotency_key,
            apply,
        )

    async def cancel_appointment(
        self, tenant_id: TenantId, patient: PatientRef, appointment_id: str, *, idempotency_key: str
    ) -> Appointment:
        names = await self._doctor_names(tenant_id)

        def apply(state: _TenantState, now: datetime) -> Appointment:
            appointment = state.appointments.get(appointment_id)
            if appointment is None or appointment.patient != patient:
                raise BookingError("NOT_FOUND")
            if appointment.status == "CANCELLED":
                return self._appointment_dto(appointment, names)  # V13
            appointment.status = "CANCELLED"
            self._free(state, appointment.slot.slot_id, ("appointment", appointment_id))
            return self._appointment_dto(appointment, names)

        return await self._write(  # type: ignore[return-value]
            tenant_id,
            "cancel_appointment",
            {"patient_ref": patient.value, "appointment_id": appointment_id},
            idempotency_key,
            apply,
        )

    # --- the write wrapper ---------------------------------------------------

    async def _write(
        self,
        tenant_id: TenantId,
        operation: str,
        body: Mapping[str, str],
        idempotency_key: str,
        apply: Callable[[_TenantState, datetime], Hold | Appointment],
    ) -> Hold | Appointment:
        """One place for idempotency, failure injection and the lock.

        Every write goes through here, so "what does a repeated key do" has exactly
        one answer and cannot drift between the four operations.
        """
        failure = self._failures.take(operation) if self._failures is not None else None
        if failure == "HANG_BEFORE":
            # OUTSIDE the lock, so the turn deadline can cut this write while every
            # other job in the process keeps running.
            await self._failures.wait()  # type: ignore[union-attr]

        body_hash = _body_hash(operation, body)
        now = self._clock()
        async with self._lock:
            state = self._state(tenant_id)
            remembered = self._replay(state, idempotency_key, now)
            if remembered is not None:
                if remembered.body_hash != body_hash:
                    # Same key, different body. An earlier request did something
                    # and we cannot tell what, so this is handled exactly like an
                    # unknown outcome upstream.
                    raise BookingError("IDEMPOTENCY_CONFLICT")
                if remembered.error_code is not None:
                    raise BookingError(remembered.error_code)
                assert remembered.answer is not None
                return remembered.answer

            if failure in _NOT_APPLIED:
                raise BookingError(
                    "UNKNOWN_OUTCOME" if failure == "UNKNOWN_BEFORE" else str(failure)
                )

            self._expire(state, now)
            if failure in _BUSINESS:
                self._remember(state, idempotency_key, body_hash, now, error_code=str(failure))
                raise BookingError(str(failure))

            try:
                result = apply(state, now)
            except BookingError as error:
                if error.code in _BUSINESS:
                    self._remember(state, idempotency_key, body_hash, now, error_code=error.code)
                raise
            self._remember(state, idempotency_key, body_hash, now, answer=result)

        if failure == "HANG_AFTER":
            await self._failures.wait()  # type: ignore[union-attr]
        if failure == "UNKNOWN_AFTER":
            # The classic: it worked, and the answer was lost. The change IS
            # applied and IS remembered, so a retry under the same key returns it.
            raise BookingError("UNKNOWN_OUTCOME")
        return result

    async def _read_failure(self, operation: str) -> None:
        failure = self._failures.take(operation) if self._failures is not None else None
        if failure == "HANG_BEFORE":
            await self._failures.wait()  # type: ignore[union-attr]
        elif failure == "UNAVAILABLE":
            raise BookingError("UNAVAILABLE")

    # --- state helpers -------------------------------------------------------

    def _state(self, tenant_id: TenantId) -> _TenantState:
        """This tenant's state, created on first use.

        Exact, case-sensitive keying (decision D1): `"Clinic-Alpha"` and
        `"clinic-alpha"` are two clinics, and this never normalises either.
        """
        state = self._tenants.get(tenant_id)
        if state is None:
            if len(self._tenants) >= self._limits.tenants:
                # Nothing here can be evicted: another tenant's holds are not ours
                # to drop. Refusing is the honest answer.
                raise BookingError("UNAVAILABLE")
            state = self._tenants[tenant_id] = _TenantState()
        return state

    async def _doctor_names(self, tenant_id: TenantId) -> dict[str, str]:
        """doctor_id -> name, from the catalogue.

        Read BEFORE the lock on purpose. A receipt has to name the doctor, and
        looking the name up while holding the lock would put a coroutine we do not
        own inside the critical section.
        """
        doctors = await self._catalogue.list_doctors(tenant_id)
        return {doctor.doctor_id: doctor.name for doctor in doctors}

    def _token(self, tenant_id: TenantId, raw_slot_id: str) -> str:
        """`slot_` + a keyed hash of the tenant and the raw id.

        Unguessable (the secret is random per instance), stable for the life of the
        instance (so two searches give the same id, which is what lets a patient
        answer "the 14:00 one"), and resolvable only by the instance that issued it.
        The alphabet is lowercase base32, which fits V10's id pattern.
        """
        digest = hmac.new(
            self._id_secret, f"{tenant_id}\x1f{raw_slot_id}".encode(), hashlib.sha256
        ).digest()
        return "slot_" + base64.b32encode(digest).decode("ascii").lower()[:20]

    def _remember_token(self, state: _TenantState, token: str, slot: Slot) -> None:
        state.tokens[token] = slot
        state.tokens.move_to_end(token)
        while len(state.tokens) > self._limits.slot_tokens_per_tenant:
            state.tokens.popitem(last=False)

    def _replay(self, state: _TenantState, key: str, now: datetime) -> _Replay | None:
        remembered = state.replays.get(key)
        if remembered is None:
            return None
        if now - remembered.at > self._limits.replay_ttl:
            del state.replays[key]
            return None
        state.replays.move_to_end(key)
        return remembered

    def _remember(
        self,
        state: _TenantState,
        key: str,
        body_hash: str,
        now: datetime,
        *,
        answer: Hold | Appointment | None = None,
        error_code: str | None = None,
    ) -> None:
        state.replays[key] = _Replay(
            body_hash=body_hash, at=now, answer=answer, error_code=error_code
        )
        state.replays.move_to_end(key)
        for stale in [k for k, v in state.replays.items() if now - v.at > self._limits.replay_ttl]:
            del state.replays[stale]
        while len(state.replays) > self._limits.replays_per_tenant:
            state.replays.popitem(last=False)

    def _expire(self, state: _TenantState, now: datetime) -> None:
        """Lapse every hold whose moment has passed, on the INJECTED clock.

        Never SQL `now()` and never the wall clock: `booking_actions` compares the
        same injected value, and two clocks that disagree would expire a hold in
        one place and not the other (plan risk R5).
        """
        for hold in state.holds.values():
            if hold.status == "ACTIVE" and hold.expires_at <= now:
                hold.status = "EXPIRED"
                self._free(state, hold.slot.slot_id, ("hold", hold.hold_id))

    def _free(self, state: _TenantState, raw_slot_id: str, owner: tuple[str, str]) -> None:
        """Release a slot, but only if this owner still holds it."""
        if state.occupied.get(raw_slot_id) == owner:
            del state.occupied[raw_slot_id]

    def _room_for_hold(self, state: _TenantState) -> None:
        if len(state.holds) < self._limits.holds_per_tenant:
            return
        for hold_id, hold in list(state.holds.items()):
            if hold.status != "ACTIVE":
                del state.holds[hold_id]
        if len(state.holds) >= self._limits.holds_per_tenant:
            raise BookingError("UNAVAILABLE")

    def _room_for_appointment(self, state: _TenantState, now: datetime) -> None:
        if len(state.appointments) < self._limits.appointments_per_tenant:
            return
        for appointment_id, appointment in list(state.appointments.items()):
            if appointment.status == "CANCELLED" or appointment.slot.end < now:
                self._free(state, appointment.slot.slot_id, ("appointment", appointment_id))
                del state.appointments[appointment_id]
        if len(state.appointments) >= self._limits.appointments_per_tenant:
            raise BookingError("UNAVAILABLE")

    # --- DTOs ----------------------------------------------------------------

    def _hold_dto(self, hold: _Hold) -> Hold:
        # slot_id is the OPAQUE token, never the catalogue's raw id: the tool's
        # result is the one place a slot id reaches the model.
        return Hold(
            hold_id=hold.hold_id,
            slot_id=hold.token,
            doctor_id=hold.slot.doctor_id,
            doctor_name=hold.doctor_name,
            start=hold.slot.start,
            end=hold.slot.end,
            expires_at=hold.expires_at,
        )

    def _appointment_dto(self, record: _Appointment, names: Mapping[str, str]) -> Appointment:
        return Appointment(
            appointment_id=record.appointment_id,
            reference=record.reference,
            doctor_id=record.slot.doctor_id,
            doctor_name=names.get(record.slot.doctor_id, record.doctor_name),
            start=record.slot.start,
            end=record.slot.end,
            status=record.status,
        )


def _random_id(prefix: str) -> str:
    """`hold_…` / `apt_…`, with an alphabet that fits V10's id pattern."""
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _reference() -> str:
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


__all__ = [
    "HOLD_TTL",
    "REFERENCE_ALPHABET",
    "Failure",
    "FailureScript",
    "InMemoryBookingService",
    "Limits",
]
