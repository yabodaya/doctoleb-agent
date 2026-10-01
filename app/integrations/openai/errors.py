"""One OpenAI failure classifier, shared by every client in this package.

It lived in `chat.py` until VS-008, which added a second caller: the
transcription client needs exactly the same answers - a timeout is retryable, a
429 is retryable unless it is `insufficient_quota`, every other 4xx is
permanent - and two copies of that table is one copy that drifts.

`chat.py` re-exports `classify_openai_error`, so every existing import and test
is untouched, and a test asserts the two names are the SAME OBJECT so a future
tidy-up cannot quietly fork them.

Nothing about the behaviour changed in the move. Read `docs/plans/VS-005-plan.md`,
section "Classification: one function, one table", before changing any of it.
"""

import re

import openai

from app.integrations.openai.interface import ChatOutcome

# OpenAI error codes look like this (insufficient_quota, model_not_found).
# Anything else is left out of the reason rather than trusted into a log line or
# a dead letter (hard rule 8).
_CODE_SHAPE = re.compile(r"[a-z0-9_]{1,48}")

# The one 429 that retrying cannot fix: the account has no credit (VS-005
# requirement 3). Checked against both `code` and `type`, which OpenAI's
# documented body sets to the same value.
_NO_CREDIT = "insufficient_quota"


def classify_openai_error(error: Exception) -> tuple[ChatOutcome, str] | None:
    """The single place an OpenAI failure becomes a retry decision.

    Retryable: timeouts (ours and the SDK's, and a 408), connection errors, 5xx,
    and a 429 rate limit. Permanent: every other 4xx, and a 429 for no credit.

    Never reads error.message, and never str(error): both carry OpenAI's text,
    and for a 401 the masked key fragment OpenAI echoes back. Returns None for
    anything that is not an OpenAI API error, so a bug escapes instead of being
    retried five times and answered with the fallback as if OpenAI were down.
    """
    # Before APIConnectionError: APITimeoutError is a subclass of it. The
    # built-in TimeoutError is our own asyncio.timeout deadline firing.
    if isinstance(error, TimeoutError | openai.APITimeoutError):
        return ChatOutcome.RETRYABLE, "openai_timeout"
    if isinstance(error, openai.APIConnectionError):
        return ChatOutcome.RETRYABLE, "openai_connection"
    if isinstance(error, openai.APIStatusError):
        status = error.status_code
        if status == 429:
            if _NO_CREDIT in (error.code, error.type):
                return ChatOutcome.PERMANENT, "openai_insufficient_quota"
            return ChatOutcome.RETRYABLE, "openai_http_429"
        if status >= 500 or status == 408:
            return ChatOutcome.RETRYABLE, f"openai_http_{status}"
        code = error.code if error.code and _CODE_SHAPE.fullmatch(error.code) else None
        return ChatOutcome.PERMANENT, f"openai_http_{status}" + (f"_{code}" if code else "")
    if isinstance(error, openai.APIError):
        # APIResponseValidationError and anything else the SDK adds later. A
        # response we could not make sense of is most likely transient.
        return ChatOutcome.RETRYABLE, "openai_bad_response"
    return None
