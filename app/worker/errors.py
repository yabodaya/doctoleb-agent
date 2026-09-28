"""The two things that can go wrong in a job, and nothing else.

Hard rule 8: these strings reach log lines and `dead_letter_jobs.error`. A
formatted exception would carry whatever it was formatted from — a payload
excerpt, a phone number, a Meta error message — so every raise site passes a code
from a small vocabulary instead.
"""


class JobError(Exception):
    """Base. Carries a short reason CODE and nothing else.

    The vocabulary in use: unknown_kind, no_phone_number_id, unknown_phone_number,
    bad_tenant_map, unmodelled_message, unmodelled_status, conversation_race,
    status_before_wamid, inbox_row_missing, event_locked, and the Meta client's
    own http_<status> / transport_<Class> codes.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RetryableJobError(JobError):
    """Try again later: the same input can succeed on a later attempt."""


class PermanentJobError(JobError):
    """Do not try again: no number of attempts changes the answer."""
