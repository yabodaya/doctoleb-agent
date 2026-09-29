"""Clinic time (decision D3). No database, no network, and a frozen clock.

Dates are where this slice is most likely to be quietly wrong: a DST mistake is
an hour off twice a year and right the rest of the time. Every test here pins a
specific 2026 date against the transitions confirmed in plan check U10.
"""

import ast
import pathlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.agent.clock import (
    CLINIC_TIMEZONE,
    CLINIC_TZ,
    CLOCK_TEMPLATE,
    clock_message,
    day_name,
    format_local,
    local_to_aware,
    long_date,
    to_clinic,
    utc_now,
)

AGENT = pathlib.Path(__file__).resolve().parents[2] / "app" / "agent"
BEIRUT = ZoneInfo("Asia/Beirut")


def test_the_clock_message_names_today_tomorrow_and_the_next_seven_days():
    """The exact text, pinned. The model reads this and does date arithmetic
    from it, so a stray change is a wrong answer rather than a formatting nit."""
    message = clock_message(datetime(2026, 9, 29, 7, tzinfo=UTC))  # Tue 10:00 local

    assert message.role == "system"
    assert message.content == (
        "Current date and time at the clinic (Asia/Beirut): Tuesday 29 September 2026, 10:00.\n"
        "Tomorrow is Wednesday 30 September 2026.\n"
        "The next seven days: Wed 2026-09-30, Thu 2026-10-01, Fri 2026-10-02, "
        "Sat 2026-10-03, Sun 2026-10-04, Mon 2026-10-05, Tue 2026-10-06.\n"
        "Every date and time you send to a tool or tell the patient is clinic local time."
    )


def test_the_clock_message_is_a_system_message_not_a_user_one():
    """It is the clinic speaking, not the patient. A `user` message here would
    be one more thing a patient could try to imitate."""
    assert clock_message(datetime(2026, 9, 29, 7, tzinfo=UTC)).role == "system"


def test_tomorrow_on_the_eve_of_spring_forward_is_the_next_calendar_day():
    """ "Tomorrow" is calendar arithmetic, never `now + 24h`.

    Lebanon springs forward at 2026-03-28T22:00Z. At 23:30 local on Saturday
    the 28th, `now + 24h` lands on MONDAY 30 March, because an hour vanishes.
    The calendar's tomorrow is Sunday the 29th, and that is what a patient
    means.
    """
    message = clock_message(datetime(2026, 3, 28, 21, 30, tzinfo=UTC))

    assert "Current date and time at the clinic (Asia/Beirut): Saturday 28 March 2026, 23:30." in (
        message.content
    )
    assert "Tomorrow is Sunday 29 March 2026." in message.content
    assert "Monday 30 March" not in message.content


def test_the_repeated_hour_on_the_autumn_night_is_still_saturday():
    """Lebanon falls back at 2026-10-24T21:00Z, so 23:00-23:59 local happens
    twice. Both times are Saturday the 24th, and the message must say so."""
    message = clock_message(datetime(2026, 10, 24, 21, 30, tzinfo=UTC))

    assert "Saturday 24 October 2026, 23:30." in message.content
    assert "Tomorrow is Sunday 25 October 2026." in message.content


def test_the_next_seven_days_span_a_dst_change_without_repeating_a_date():
    """Calendar dates, not 24-hour steps: across a transition the latter would
    repeat or skip a day, and the model would be told Monday twice."""
    message = clock_message(datetime(2026, 10, 22, 7, tzinfo=UTC))

    line = [row for row in message.content.splitlines() if row.startswith("The next seven")][0]
    dates = [part.split()[1] for part in line.split(": ", 1)[1].rstrip(".").split(", ")]
    assert dates == [
        "2026-10-23",
        "2026-10-24",
        "2026-10-25",
        "2026-10-26",
        "2026-10-27",
        "2026-10-28",
        "2026-10-29",
    ]
    assert len(set(dates)) == 7


def test_local_times_take_the_offset_of_their_own_date():
    """The whole reason `local_to_aware` exists.

    Saturday 24 October is +03:00 and Monday 26 October is +02:00. A window
    spanning the change that reused one offset would be an hour wrong on one
    side - and right on the other, which is what makes it hard to notice.
    """
    saturday = local_to_aware(datetime(2026, 10, 24, 12, 0))
    monday = local_to_aware(datetime(2026, 10, 26, 12, 0))

    assert saturday.utcoffset().total_seconds() == 3 * 3600
    assert monday.utcoffset().total_seconds() == 2 * 3600
    assert saturday.astimezone(UTC).hour == 9
    assert monday.astimezone(UTC).hour == 10


def test_a_time_in_the_spring_gap_is_shifted_not_rejected():
    """00:30 on 29 March 2026 never happens locally: the clock jumps 00:00 to
    01:00. zoneinfo takes the pre-transition offset, so it becomes 01:30+03:00.

    Pinned as BEHAVIOUR, not endorsed as correct: a search-window boundary an
    hour off in the middle of the night changes nothing for a clinic that opens
    at 09:00, and raising here would turn a harmless boundary into a failed
    turn.
    """
    inside_the_gap = local_to_aware(datetime(2026, 3, 29, 0, 30))

    assert inside_the_gap.utcoffset().total_seconds() == 2 * 3600
    assert inside_the_gap.astimezone(UTC) == datetime(2026, 3, 28, 22, 30, tzinfo=UTC)
    # The INSTANT it denotes, read back as clinic time. It has to go through UTC
    # to get there: the object still carries the wall time 00:30 that was asked
    # for, and `.astimezone(BEIRUT)` on a value already in Beirut is a no-op, so
    # it would just hand back the impossible 00:30 unchanged.
    assert inside_the_gap.astimezone(UTC).astimezone(BEIRUT).strftime("%H:%M") == "01:30"


def test_the_repeated_autumn_hour_resolves_to_its_first_occurrence():
    """23:30 on 24 October 2026 happens twice. `fold=0` is the default, so it
    resolves to the first (+03:00). Pinned for the same reason as the gap."""
    repeated = local_to_aware(datetime(2026, 10, 24, 23, 30))

    assert repeated.utcoffset().total_seconds() == 3 * 3600
    assert repeated.fold == 0


def test_local_to_aware_refuses_an_already_aware_datetime():
    """It takes a NAIVE clinic-local value. Handing it an aware one means the
    caller has already decided a zone, and silently re-stamping it would be a
    double conversion nobody could see."""
    with pytest.raises(ValueError, match="naive"):
        local_to_aware(datetime(2026, 9, 30, 12, tzinfo=UTC))


def test_a_naive_clock_is_refused():
    """A naive "now" has no meaning: assuming a zone would be wrong for exactly
    one hour twice a year, which is the hardest kind of bug to find."""
    with pytest.raises(ValueError, match="aware"):
        to_clinic(datetime(2026, 9, 29, 10, 0))


def test_utc_now_is_aware_and_utc():
    assert utc_now().tzinfo is UTC


def test_the_clinic_timezone_constants_agree():
    assert CLINIC_TZ.key == CLINIC_TIMEZONE == "Asia/Beirut"


def test_format_local_is_the_format_the_tools_take():
    """The same spelling in both directions - what the model is asked to send,
    and what it is shown in a result. One format is one thing to get wrong."""
    assert format_local(datetime(2026, 9, 30, 11, 0, tzinfo=UTC)) == "2026-09-30T14:00"
    assert format_local(local_to_aware(datetime(2026, 9, 30, 14, 0))) == "2026-09-30T14:00"


def test_day_and_long_date_names_come_from_the_fixed_tables():
    assert day_name(local_to_aware(datetime(2026, 9, 30, 14, 0))) == "Wednesday"
    assert long_date(datetime(2026, 9, 29).date()) == "Tuesday 29 September 2026"


def test_the_weekday_and_month_names_do_not_depend_on_the_locale():
    """`strftime("%A")` and `"%B"` follow the process locale, so the message the
    model reads would change with the container's LC_TIME - silently, and only
    on some deployments. The tables are fixed English, and this is the test that
    says so.
    """
    import app.agent.clock as clock_module

    tree = ast.parse(pathlib.Path(clock_module.__file__).read_text(encoding="utf-8"))
    formats: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "strftime"
        ):
            assert node.args, "strftime with no format"
            assert isinstance(node.args[0], ast.Constant), "a computed strftime format"
            formats.append(node.args[0].value)

    assert formats, "no strftime call found: this test would pass vacuously"
    for fmt in formats:
        for directive in ("%A", "%a", "%B", "%b", "%p", "%c", "%x", "%X"):
            assert directive not in fmt, f"{fmt} uses {directive}"
    # What IS used is locale-independent: digits only.
    assert set(formats) <= {"%Y-%m-%dT%H:%M", "%H:%M"}


def test_the_clock_template_is_a_module_constant():
    """Q13 hashes it alongside the prompt version, so it has to be reachable
    without rendering a message first."""
    assert "{timezone}" in CLOCK_TEMPLATE
    assert "{tomorrow}" in CLOCK_TEMPLATE
    assert "clinic local time" in CLOCK_TEMPLATE


def test_the_agent_never_reads_the_wall_clock_except_through_utc_now():
    """One doorway to "now", enforced by an AST sweep of the whole package.

    A turn that read the clock twice could answer "tomorrow" with two different
    dates, and a module that read it directly could not be frozen by a test -
    so the DST tests above would silently stop covering anything.
    """
    offenders: list[str] = []
    for path in sorted(AGENT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            rendered = ast.unparse(node.func)
            if rendered in ("datetime.now", "datetime.datetime.now", "date.today", "time.time"):
                if path.name == "clock.py" and rendered == "datetime.now":
                    continue  # utc_now itself: the one doorway
                offenders.append(f"{path.relative_to(AGENT.parent)}:{node.lineno} {rendered}")

    assert offenders == []
