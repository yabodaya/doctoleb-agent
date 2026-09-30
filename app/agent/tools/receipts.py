"""The receipt line: what the clinic's own system said, in one line (G1, V4).

**Why receipts exist.** The model writes the reply, and it can misread a tool
result, answer before calling the tool, repeat what the patient hoped rather than
what happened, or be talked into it. So the claim "your appointment is booked"
must be tied to a fact OUR CODE saw - a success answer from the Booking Service in
this same turn - and not to the model's judgement. That is the whole of hard
rule 5, and this module is the positive half of it: the ✅ line is the proof a
patient can rely on, and the model is forbidden to write one itself.

Every line is built **only from the service's answer**: the doctor's name as the
service spells it, the time as the service returned it, and the reference code the
service issued. Nothing here reads the model's text, and nothing here can be
constructed from a value the model supplied.

The symbols carry the meaning that words would have to carry in four languages:

    ⏳   waiting for the patient - a hold, or a prepared cancellation.
         NOT booked, NOT changed, NOT cancelled.
    ✅   booked, confirmed by the clinic's system.
    🔁   moved.
    ❌   cancelled.

A `PENDING_APPROVAL` booking gets ⏳, not ✅: the clinic has not confirmed it, and
saying "booked" would be exactly the false claim hard rule 5 forbids.

Pure: `zoneinfo` through `app/agent/clock.py`, and nothing else. The receipt is
CONTENT - a doctor's name and an appointment time - so it lives in `messages.text`
(the reply the patient gets) and nowhere else: not in a log line, not in
`booking_actions`, not in a dead letter (hard rule 8, plan section 5.13).
"""

from datetime import datetime

from app.agent.clock import CLINIC_TZ
from app.integrations.booking import Appointment, Hold

HELD = "⏳"  # hourglass: waiting for the patient
BOOKED = "✅"  # white heavy check mark
MOVED = "\U0001f501"  # repeat
CANCELLED = "❌"  # cross mark

# A middle dot with spaces. It separates the parts without looking like a date
# separator, and WhatsApp renders it on both Android and iOS (UNVERIFIED until
# Task B6's live sitting, plan section 4.3).
SEPARATOR = " · "
ARROW = " → "


def local_stamp(moment: datetime) -> str:
    """`YYYY-MM-DD HH:MM`, clinic-local.

    A space rather than the `T` the tool results use: a result is read by a model
    and a receipt is read by a patient, and `2026-09-30T14:00` looks like a machine
    talking. The date is spelled out in full because a patient may be reading it
    days later.
    """
    return moment.astimezone(CLINIC_TZ).strftime("%Y-%m-%d %H:%M")


def _line(symbol: str, doctor_name: str, moment: datetime, reference: str | None = None) -> str:
    parts = [f"{symbol} {doctor_name}", local_stamp(moment)]
    if reference is not None:
        # The reference is shown ONLY here, and only for a change that actually
        # happened: it is the code a patient quotes to the clinic. It never reaches
        # the model, so the model cannot invent or repeat one.
        parts.append(f"#{reference}")
    return SEPARATOR.join(parts)


def hold_receipt(hold: Hold) -> str:
    """A slot held for a booking. ⏳, and no reference: nothing is booked."""
    return _line(HELD, hold.doctor_name, hold.start)


def move_receipt(hold: Hold, moving: Appointment) -> str:
    """A slot held for a MOVE: the old time, an arrow, the new one.

    Both halves, because "shall I move it?" is only answerable if the patient can
    see what it is being moved from.
    """
    old = _line(HELD, moving.doctor_name, moving.start)
    new = f"{hold.doctor_name}{SEPARATOR}{local_stamp(hold.start)}"
    return f"{old}{ARROW}{new}"


def prepared_cancellation_receipt(appointment: Appointment) -> str:
    """A cancellation the patient has not confirmed yet.

    ⏳ AND ❌: the ❌ says which way this is going, and the ⏳ in front of it says it
    has not gone that way yet. A bare ❌ here would read as "cancelled" to anybody
    who had learned what ❌ means.
    """
    return (
        f"{HELD} {CANCELLED} {appointment.doctor_name}{SEPARATOR}{local_stamp(appointment.start)}"
    )


def booked_receipt(appointment: Appointment) -> str:
    """A booking the service confirmed - or requested, if the clinic must approve.

    The ONE place ✅ is produced, and it is produced from `status == "CONFIRMED"`
    rather than from anything the model said (hard rule 5).
    """
    symbol = BOOKED if appointment.status == "CONFIRMED" else HELD
    return _line(symbol, appointment.doctor_name, appointment.start, appointment.reference)


def moved_receipt(appointment: Appointment) -> str:
    """A completed move: 🔁 and the NEW time, with the reference kept."""
    return _line(MOVED, appointment.doctor_name, appointment.start, appointment.reference)


def cancelled_receipt(appointment: Appointment) -> str:
    """A completed cancellation: ❌ and the time that is no longer happening."""
    return _line(CANCELLED, appointment.doctor_name, appointment.start, appointment.reference)


__all__ = [
    "ARROW",
    "BOOKED",
    "CANCELLED",
    "HELD",
    "MOVED",
    "SEPARATOR",
    "booked_receipt",
    "cancelled_receipt",
    "hold_receipt",
    "local_stamp",
    "move_receipt",
    "moved_receipt",
    "prepared_cancellation_receipt",
]
