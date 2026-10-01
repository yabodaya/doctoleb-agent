"""The receipt line (G1, plan section 5.9).

Pure: no database, no network, no model. Each test pins one row of the plan's
table, because the symbols are the only thing a patient can rely on and a wrong
one is a lie in one character.
"""

import datetime as dt
from zoneinfo import ZoneInfo

from app.agent.tools.receipts import (
    BOOKED,
    CANCELLED,
    HELD,
    MOVED,
    booked_receipt,
    cancelled_receipt,
    hold_receipt,
    local_stamp,
    move_receipt,
    moved_receipt,
    prepared_cancellation_receipt,
)
from app.integrations.booking import Appointment, Hold

BEIRUT = ZoneInfo("Asia/Beirut")
KARIM = "Dr. Karim Haddad"


def hold(start: dt.datetime, doctor_name: str = KARIM) -> Hold:
    return Hold(
        hold_id="hold_1",
        slot_id="slot_abcdef",
        doctor_id="doc_karim",
        doctor_name=doctor_name,
        start=start,
        end=start + dt.timedelta(minutes=20),
        expires_at=start - dt.timedelta(hours=1),
    )


def appointment(start: dt.datetime, status: str = "CONFIRMED", reference: str = "K7Q2M9"):
    return Appointment(
        appointment_id="apt_1",
        reference=reference,
        doctor_id="doc_karim",
        doctor_name=KARIM,
        start=start,
        end=start + dt.timedelta(minutes=20),
        status=status,
    )


WEDNESDAY_14 = dt.datetime(2026, 9, 30, 14, tzinfo=BEIRUT)
THURSDAY_10 = dt.datetime(2026, 10, 1, 10, tzinfo=BEIRUT)


def test_each_outcome_has_its_receipt_line():
    """The seven rows of plan section 5.9, in one place so the set is visible.

    The symbols are doing the work of four languages: ⏳ means "waiting for you",
    ✅ means "the clinic's system says booked", 🔁 moved, ❌ cancelled. A patient
    learns them once.
    """
    assert hold_receipt(hold(WEDNESDAY_14)) == "⏳ Dr. Karim Haddad · 2026-09-30 14:00"
    assert move_receipt(hold(THURSDAY_10), appointment(WEDNESDAY_14)) == (
        "⏳ Dr. Karim Haddad · 2026-09-30 14:00 → Dr. Karim Haddad · 2026-10-01 10:00"
    )
    assert prepared_cancellation_receipt(appointment(WEDNESDAY_14)) == (
        "⏳ ❌ Dr. Karim Haddad · 2026-09-30 14:00"
    )
    assert booked_receipt(appointment(WEDNESDAY_14)) == (
        "✅ Dr. Karim Haddad · 2026-09-30 14:00 · #K7Q2M9"
    )
    assert booked_receipt(appointment(WEDNESDAY_14, status="PENDING_APPROVAL")) == (
        "⏳ Dr. Karim Haddad · 2026-09-30 14:00 · #K7Q2M9"
    )
    assert moved_receipt(appointment(THURSDAY_10)) == (
        "🔁 Dr. Karim Haddad · 2026-10-01 10:00 · #K7Q2M9"
    )
    assert cancelled_receipt(appointment(WEDNESDAY_14, status="CANCELLED")) == (
        "❌ Dr. Karim Haddad · 2026-09-30 14:00 · #K7Q2M9"
    )


def test_a_pending_approval_booking_is_never_given_a_tick():
    """Contract open question 3, and the one place it could go wrong quietly.

    `PENDING_APPROVAL` means the clinic has not confirmed it. A ✅ there would be
    the exact false claim hard rule 5 forbids, and the patient would arrive to an
    appointment nobody accepted.
    """
    requested = booked_receipt(appointment(WEDNESDAY_14, status="PENDING_APPROVAL"))

    assert requested.startswith(HELD)
    assert BOOKED not in requested


def test_a_prepared_change_never_looks_like_a_completed_one():
    """Both halves of the ⏳ rule: a hold and a prepared cancellation carry NO
    reference code, because a reference is the receipt for something that
    happened."""
    for line in (
        hold_receipt(hold(WEDNESDAY_14)),
        move_receipt(hold(THURSDAY_10), appointment(WEDNESDAY_14)),
        prepared_cancellation_receipt(appointment(WEDNESDAY_14)),
    ):
        assert line.startswith(HELD)
        assert "#" not in line
        assert BOOKED not in line
        assert MOVED not in line


def test_a_prepared_cancellation_says_it_has_not_happened_yet():
    """A bare ❌ would read as "cancelled" to a patient who had learned what ❌
    means, so the ⏳ comes FIRST and the ❌ only says which way this is going."""
    line = prepared_cancellation_receipt(appointment(WEDNESDAY_14))

    assert line.index(HELD) < line.index(CANCELLED)


def test_receipts_use_clinic_local_time_across_the_autumn_change():
    """Lebanon leaves DST at 2026-10-25T00:00 local (VS-006's Appendix D).

    Both of these are the same UTC hour on consecutive days; only the offset
    differs. A receipt built from a UTC time, or from a fixed offset, would tell
    the patient the wrong hour for half the year.
    """
    before = dt.datetime(2026, 10, 24, 8, tzinfo=dt.UTC)  # +03:00 -> 11:00 local
    after = dt.datetime(2026, 10, 26, 8, tzinfo=dt.UTC)  # +02:00 -> 10:00 local

    assert local_stamp(before) == "2026-10-24 11:00"
    assert local_stamp(after) == "2026-10-26 10:00"
    assert booked_receipt(appointment(after)).endswith("2026-10-26 10:00 · #K7Q2M9")


def test_a_receipt_is_built_only_from_the_services_own_answer():
    """Hard rule 5's positive half. The doctor's name is spelled as the SERVICE
    spelled it - not as the model wrote it - so a model that got the name wrong in
    its own sentence is contradicted by the line underneath."""
    theirs = hold_receipt(hold(WEDNESDAY_14, doctor_name="Dr. Rania Khoury"))

    assert "Dr. Rania Khoury" in theirs
    assert KARIM not in theirs


def test_failures_and_unknown_outcomes_have_no_receipt():
    """There is no function that could build one.

    That is the design, not an omission: a receipt is produced from an
    `Appointment` or a `Hold`, and a failed or unknown write returns neither. So
    "nothing happened" and "we do not know" cannot acquire a line that says
    something did.
    """
    import app.agent.tools.receipts as receipts

    builders = [name for name in receipts.__all__ if name.endswith("_receipt")]
    assert sorted(builders) == [
        "booked_receipt",
        "cancelled_receipt",
        "hold_receipt",
        "move_receipt",
        "moved_receipt",
        "prepared_cancellation_receipt",
    ]
    for name in builders:
        parameters = receipts.__dict__[name].__code__.co_varnames[:1]
        assert parameters[0] in ("hold", "appointment"), name
