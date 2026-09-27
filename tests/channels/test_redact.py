"""Hard rule 8 at the one boundary where a third party hands us free text."""

from app.channels.whatsapp.redact import REDACTED, error_reason, scrub
from tests.whatsapp_factories import phone


def test_a_long_digit_run_is_redacted():
    assert phone(1) not in scrub(f"Recipient {phone(1)} is not a valid WhatsApp user")
    assert REDACTED in scrub(f"Recipient {phone(1)} is not valid")


def test_short_numbers_survive():
    """Status codes, error codes and version numbers must stay readable.

    A scrubber that ate every digit would make every reason code identical, and
    the next person debugging a 429 storm would have nothing to go on.
    """
    assert scrub("http_429 code_131026 v21.0") == "http_429 code_131026 v21.0"


def test_an_error_reason_is_built_from_codes_only():
    body = (
        b'{"error": {"message": "Recipient phone number not in allowed list: '
        + phone(1).encode()
        + b'", "type": "OAuthException", "code": 131030, "error_subcode": 2494010, '
        b'"fbtrace_id": "Axyz"}}'
    )

    reason = error_reason(400, body)

    assert reason == "http_400 code_131030 subcode_2494010"


def test_an_error_reason_never_contains_the_message_field():
    """The specific leak VS-004 requirement 6 names.

    Meta's error.message quotes the recipient's number for the single most common
    test-number failure there is, and that string is exactly what a hurried
    implementation would put in dead_letter_jobs.error.
    """
    body = b'{"error": {"message": "Bad number ' + phone(2).encode() + b'", "code": 131030}}'

    reason = error_reason(400, body)

    assert phone(2) not in reason
    assert "Bad number" not in reason


def test_an_unparseable_error_body_still_produces_a_reason():
    """A sanitiser that raises on junk is a sanitiser that gets bypassed."""
    assert error_reason(502, b"<html>502 Bad Gateway</html>") == "http_502"
    assert error_reason(503, None) == "http_503"
    assert error_reason(500, b"[]") == "http_500"
    assert error_reason(500, b'{"error": "a string, not an object"}') == "http_500"


def test_a_seven_digit_error_subcode_survives_intact():
    """The reason a reason is NOT run through scrub().

    Meta's error_subcode values are seven digits - 2494010 is "recipient not in
    the allowed list", which is the single most likely failure on a test number -
    so scrubbing a reason built from ints would eat the one code worth having.
    error_reason never reads a Meta-supplied string, which is what makes that safe.
    """
    body = b'{"error": {"code": 131030, "error_subcode": 2494010}}'

    assert error_reason(400, body) == "http_400 code_131030 subcode_2494010"
