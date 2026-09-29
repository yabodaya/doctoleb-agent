"""What time it is at the clinic, and how the model is told (decision D3).

Pure: `zoneinfo` and the standard library, nothing else. It imports no settings,
no database and no SDK, so the whole of date handling is provable with nothing
running.

Why the date is NOT in the system prompt: the prompt's SHA-256 is pinned to its
version, so a prompt that changed every day could not be pinned at all. The
clock is injected as a separate message each turn instead.

Why the model resolves "tomorrow" and our code validates it: only the model
understands "tomorrow afternoon" in four languages, but models are bad at date
arithmetic and do not know what day it is. So we tell it the clinic's date and
time, it turns words into explicit local times, and
`app/agent/tools/slots.py` checks and converts them.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.integrations.openai import ChatMessage

CLINIC_TIMEZONE = "Asia/Beirut"
# Built at import, deliberately. A missing IANA database then fails loudly when
# the worker starts rather than on the first patient who says "tomorrow"
# (plan check U3: a Windows host has no system database at all, which is why
# `tzdata` is a runtime dependency).
CLINIC_TZ = ZoneInfo(CLINIC_TIMEZONE)

# A clock returns an AWARE UTC datetime. Injected rather than read, so every
# test can freeze it - "tomorrow" and "already past" are only testable when
# "now" is a fact.
Clock = Callable[[], datetime]

# Fixed English tables, never strftime("%A") or "%B": those follow the process
# locale, so the message the model reads would change with the container's
# LC_TIME. A test pins that.
WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
SHORT_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# The clock message's shape, kept as a template so Q13's pin can hash it
# alongside the prompt version: it instructs the model as much as the prompt
# does, so changing it must be as deliberate as changing the prompt.
CLOCK_TEMPLATE = (
    "Current date and time at the clinic ({timezone}): {today}, {time}.\n"
    "Tomorrow is {tomorrow}.\n"
    "The next seven days: {next_seven}.\n"
    "Every date and time you send to a tool or tell the patient is clinic local time."
)


def utc_now() -> datetime:
    """The only wall-clock read in `app/agent/`.

    An AST test enforces that: no `datetime.now`, `date.today` or `time.time`
    anywhere else in the package. One doorway means one thing to freeze, and a
    turn that read the clock twice could answer "tomorrow" with two different
    dates.
    """
    return datetime.now(UTC)


def to_clinic(moment: datetime) -> datetime:
    """An aware instant, as clinic local time. Refuses a naive one.

    Naive is refused rather than assumed: guessing would be wrong for exactly
    one hour twice a year and right the rest of the time, which is the hardest
    kind of bug to find.
    """
    if moment.tzinfo is None:
        raise ValueError("clock returned a naive datetime; it must be timezone-aware")
    return moment.astimezone(CLINIC_TZ)


def local_to_aware(naive: datetime) -> datetime:
    """A naive clinic-local datetime as a real instant, with THAT date's offset.

    `replace(tzinfo=CLINIC_TZ)` rather than any arithmetic, so the offset comes
    from zoneinfo's rules for that particular date. Lebanon changes offset twice
    a year; reusing one offset across a multi-day window would be an hour wrong
    on one side of the change.

    Two edge cases are resolved rather than rejected, and tests pin both:

    - a time inside the spring-forward GAP (which never happens locally) takes
      the pre-transition offset, so 00:30 becomes 01:30+03:00;
    - a time in the repeated autumn hour resolves to its FIRST occurrence
      (`fold=0`).

    Neither is an error worth raising: a search-window boundary an hour off in
    the middle of the night changes nothing for a clinic that opens at 09:00.
    """
    if naive.tzinfo is not None:
        raise ValueError("local_to_aware takes a naive clinic-local datetime")
    return naive.replace(tzinfo=CLINIC_TZ)


def format_local(moment: datetime) -> str:
    """`YYYY-MM-DDTHH:MM` in clinic local time - the format the tools take.

    The same spelling in both directions: what the model is asked to send, and
    what it is shown in a result. One format is one thing to get wrong.
    """
    return to_clinic(moment).strftime("%Y-%m-%dT%H:%M")


def long_date(day: date) -> str:
    """ "Tuesday 29 September 2026", from the fixed tables."""
    return f"{WEEKDAY_NAMES[day.weekday()]} {day.day} {MONTH_NAMES[day.month - 1]} {day.year}"


def day_name(moment: datetime) -> str:
    """ "Wednesday", for a slot's day in a tool result."""
    return WEEKDAY_NAMES[to_clinic(moment).weekday()]


def clock_message(now: datetime) -> ChatMessage:
    """The separate `system` message that tells the model today's date (D3).

    The next-seven-days line with weekday names is there because models are bad
    at weekday arithmetic (Q12): asked for "next Monday" they will often pick
    the wrong one, and a wrong date is a wrong answer rather than an error.

    "Tomorrow" is `local_date + 1 day`, calendar arithmetic and never
    `now + 24h`. On the eve of spring-forward those differ: at 23:30 local on
    28 March 2026, `now + 24h` lands on MONDAY 30 March while the calendar's
    tomorrow is Sunday 29 (plan section 4.1).
    """
    local = to_clinic(now)
    today = local.date()
    tomorrow = today + timedelta(days=1)
    next_seven = ", ".join(
        f"{SHORT_WEEKDAY_NAMES[day.weekday()]} {day.isoformat()}"
        for day in (today + timedelta(days=offset) for offset in range(1, 8))
    )
    return ChatMessage(
        "system",
        CLOCK_TEMPLATE.format(
            timezone=CLINIC_TIMEZONE,
            today=long_date(today),
            time=local.strftime("%H:%M"),
            tomorrow=long_date(tomorrow),
            next_seven=next_seven,
        ),
    )
