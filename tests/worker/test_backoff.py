"""Hard rule 11's backoff curve. No database, no Redis."""

from app.worker.retrying import backoff_seconds
from tests.worker.conftest import worker_settings


def test_the_backoff_curve_is_five_ten_twenty_forty():
    """Pinned, so a change to the curve is a decision rather than a side effect.

    With the documented defaults these are the four deferrals before the fifth
    failure dead-letters - about 75 seconds of patience in total.
    """
    settings = worker_settings()

    assert [backoff_seconds(n, settings) for n in (1, 2, 3, 4)] == [5.0, 10.0, 20.0, 40.0]


def test_the_first_try_waits_the_base_delay():
    """job_try is the number of the try that just FAILED, not an index.

    Off by one here and the first retry fires immediately, which against a Meta
    outage means five sends in the time one was meant to take.
    """
    assert backoff_seconds(1, worker_settings()) == 5.0


def test_the_backoff_is_capped():
    settings = worker_settings(job_backoff_base_seconds=5.0, job_backoff_max_seconds=30.0)

    assert backoff_seconds(10, settings) == 30.0


def test_a_zero_or_negative_job_try_is_treated_as_the_first():
    """Defensive: job_try comes from arq's ctx.

    A 0 would otherwise produce base * 2**-1 - a half-second retry storm against
    exactly the outage the backoff exists for.
    """
    settings = worker_settings()

    assert backoff_seconds(0, settings) == 5.0
    assert backoff_seconds(-3, settings) == 5.0
