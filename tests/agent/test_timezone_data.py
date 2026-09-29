"""The clinic's timezone must load, wherever this runs.

Decision D3 resolves "tomorrow afternoon" in Asia/Beirut, so a missing IANA
time zone database is not a degraded feature - it is the slice not working. This
is a one-line test on purpose: it fails at import-time speed, with a clear name,
before any of the clock or slot tests produce a confusing cascade.

Q5 added `tzdata` as a runtime dependency because of exactly this (plan check
U3): Windows ships no system database at all, so on a developer host
`ZoneInfo("Asia/Beirut")` raised `ZoneInfoNotFoundError` while the Linux image
loaded it fine - the worst kind of split, passing in CI and failing locally.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def test_the_clinic_timezone_loads():
    beirut = ZoneInfo("Asia/Beirut")

    # Not just "it loaded": a database present but empty for this zone would
    # give a fixed UTC offset and every "afternoon" would be two or three hours
    # out. Lebanon is +03:00 in summer and +02:00 in winter.
    assert datetime(2026, 7, 1, 12, tzinfo=beirut).utcoffset().total_seconds() == 3 * 3600
    assert datetime(2026, 1, 1, 12, tzinfo=beirut).utcoffset().total_seconds() == 2 * 3600
    assert datetime(2026, 7, 1, 12, tzinfo=beirut).astimezone(UTC).hour == 9
