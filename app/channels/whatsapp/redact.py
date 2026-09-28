"""Turning a Meta failure into something safe to keep.

Hard rule 8, applied at the boundary. Meta's error bodies are the one place in
this slice where a third party hands us a free-text string, and `error.message`
quotes the recipient's phone number often enough that "we only ever store codes"
needs a second line of defence behind it.
"""

import json
import re
from typing import Any

# Seven digits, not ten. E.164 numbers turn up with and without country codes,
# with punctuation stripped, and sometimes as a fragment. A genuine seven-digit
# identifier being redacted costs one log line's readability; a phone number
# surviving costs a hard rule.
_DIGIT_RUN = re.compile(r"\d{7,}")

REDACTED = "[redacted]"


def scrub(value: str) -> str:
    """Replace long digit runs, so no phone number survives in a string we keep.

    Belt and braces rather than the primary defence: `error_reason` below never
    reads a Meta-supplied string in the first place. This exists because the
    primary defence is a convention, and one future call site that formats a Meta
    string into a reason would otherwise leak silently.
    """
    return _DIGIT_RUN.sub(REDACTED, value)


def error_reason(status_code: int, body: bytes | None) -> str:
    """A short, safe description of a failed Meta call.

    Built ONLY from the numeric fields of Meta's error object - never from
    `error.message`, which routinely contains the recipient's number, and never
    from `error.error_user_msg`, which is written for an end user and can quote
    anything. The result looks like `http_400 code_131026 subcode_2494010`.

    Never raises. A sanitiser that blows up on a junk body is a sanitiser that
    gets bypassed by whoever is trying to log the junk body.
    """
    parts = [f"http_{status_code}"]
    error = _error_object(body)
    for key, label in (("code", "code"), ("error_subcode", "subcode")):
        value = error.get(key)
        if isinstance(value, int):
            parts.append(f"{label}_{value}")
    # Deliberately NOT passed through scrub(). Every part here is built from an
    # int that passed an isinstance check, so there is nothing to redact - and
    # scrubbing anyway is not free: Meta's error_subcode values are seven digits
    # (2494010 is the one for "recipient not in the allowed list"), so the digit
    # run this module redacts would eat the single most useful code in the whole
    # reason. scrub() is for Meta-supplied STRINGS; this function's job is to
    # make sure none of them ever gets this far.
    return " ".join(parts)


def _error_object(body: bytes | None) -> dict[str, Any]:
    if not body:
        return {}
    try:
        decoded = json.loads(body)
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    error = decoded.get("error")
    return error if isinstance(error, dict) else {}
