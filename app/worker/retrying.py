"""How long to wait before the next try.

Hard rule 11: bounded retries with backoff. The bound is `job_max_tries`; this is
the backoff.
"""

from app.config import Settings


def backoff_seconds(job_try: int, settings: Settings) -> float:
    """Exponential backoff, capped.

    `job_try` is the number of the try that just FAILED, so the first failure
    waits the base delay rather than zero. With the documented defaults (base 5,
    cap 300, max_tries 5) the deferrals are 5s, 10s, 20s, 40s and the fifth
    failure dead-letters - about 75 seconds of patience.

    A job_try below 1 is treated as the first. It comes from arq's ctx, and a 0
    would otherwise produce a half-second retry storm against a Meta outage.

    No jitter, deliberately (plan assumption A9): these jobs are keyed per
    message and do not stampede a shared resource. A thundering herd needs many
    jobs failing on the same tick, which Meta's per-number rate limit would cause
    and which the 429 path plus the cap already blunt.
    """
    exponent = max(job_try, 1) - 1
    return min(settings.job_backoff_base_seconds * (2**exponent), settings.job_backoff_max_seconds)
