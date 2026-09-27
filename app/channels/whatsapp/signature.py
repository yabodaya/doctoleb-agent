"""X-Hub-Signature-256 verification.

This is the only thing standing between our database and the public internet:
the webhook URL is reachable by anyone, so "Meta sent this" means exactly "the
digest matches, computed with our app secret, over the bytes we received".
"""

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="


def verify_signature(raw_body: bytes, header_value: str | None, app_secret: str) -> bool:
    """True only if `header_value` is Meta's HMAC-SHA256 of `raw_body`.

    Takes RAW bytes, never a parsed object. HMAC is defined over bytes, and
    re-serialising parsed JSON changes whitespace, escaping and key order - so a
    verifier that hashes `json.dumps(parsed)` rejects genuine deliveries. That is
    why the endpoint reads `await request.body()` before it parses anything, and
    why it declares no pydantic body parameter (FastAPI would parse first).

    Returns False instead of raising, for every failure: a malformed header is an
    invalid signature, not a server error. The caller turns False into 401.

    An empty `app_secret` rejects everything. hmac.new(b"", body) is a valid
    HMAC, so treating "unset" as "the key is the empty string" would let anyone
    who guessed the secret was missing forge a signature.
    """
    if not app_secret:
        return False
    if not header_value or not header_value.startswith(SIGNATURE_PREFIX):
        return False
    try:
        # Decoding to bytes, rather than comparing hex strings, handles upper and
        # lower case hex for free and rejects junk here instead of at compare
        # time. compare_digest on str also raises TypeError on a non-ASCII value,
        # which a hostile header supplies for free.
        received = bytes.fromhex(header_value.removeprefix(SIGNATURE_PREFIX))
    except ValueError:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    # compare_digest, never ==. A plain comparison returns as soon as two bytes
    # differ, so the time it takes measures how many leading bytes were right,
    # and an attacker recovers the digest one byte at a time.
    return hmac.compare_digest(expected, received)
