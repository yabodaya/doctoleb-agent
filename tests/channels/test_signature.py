"""The entire authentication of this API. No database, no app, no network."""

import json

from app.channels.whatsapp.signature import SIGNATURE_HEADER, verify_signature
from tests.whatsapp_factories import APP_SECRET, digest, envelope, text_message, to_bytes

BODY = to_bytes(envelope(messages=[text_message()]))


def _header(value: str) -> str:
    return f"sha256={value}"


def test_the_header_name_is_the_one_meta_sends():
    # A typo here means every request arrives unsigned and every request is
    # rejected, which looks exactly like a wrong app secret.
    assert SIGNATURE_HEADER == "X-Hub-Signature-256"


def test_a_signature_from_the_right_secret_verifies():
    assert verify_signature(BODY, _header(digest(BODY)), APP_SECRET) is True


def test_a_signature_from_another_secret_is_rejected():
    forged = _header(digest(BODY, "someone-elses-secret"))

    assert verify_signature(BODY, forged, APP_SECRET) is False


def test_one_changed_byte_in_the_body_is_rejected():
    header = _header(digest(BODY))
    tampered = BODY.replace(b"synthetic", b"Synthetic")

    assert tampered != BODY
    assert verify_signature(tampered, header, APP_SECRET) is False


def test_a_missing_or_empty_header_is_rejected():
    for header in (None, "", "sha256=", "   "):
        assert verify_signature(BODY, header, APP_SECRET) is False, repr(header)


def test_a_header_without_the_sha256_prefix_is_rejected():
    # The bare digest is not accepted: tolerating it would mean guessing the
    # algorithm, and "sha1=" is a different, weaker one Meta also sends.
    for header in (digest(BODY), f"sha1={digest(BODY)}", f"SHA256={digest(BODY)}"):
        assert verify_signature(BODY, header, APP_SECRET) is False, header


def test_a_header_that_is_not_hex_is_rejected():
    # bytes.fromhex raises on junk; that must become False, not a 500.
    for header in (_header("not-hex-at-all"), _header("abc"), _header("é" * 64)):
        assert verify_signature(BODY, header, APP_SECRET) is False, header


def test_hex_case_does_not_matter():
    # Hex case carries no meaning. Comparing the decoded bytes rather than the
    # strings makes this true by construction instead of by a .lower() someone
    # can delete.
    assert verify_signature(BODY, _header(digest(BODY).upper()), APP_SECRET) is True


def test_an_unset_app_secret_rejects_even_a_correctly_computed_signature():
    """Review Focus 2.

    hmac.new(b"", body) is a perfectly valid HMAC. If an unset secret merely
    meant "the key is the empty string", anyone who guessed that META_APP_SECRET
    was missing could sign their own requests.
    """
    assert verify_signature(BODY, _header(digest(BODY, "")), "") is False
    assert verify_signature(BODY, _header(digest(BODY)), "") is False


def test_the_digest_is_over_the_exact_bytes_not_reserialised_json():
    """Review Focus 1.

    Meta's body is not what json.dumps() would produce: different spacing,
    different unicode escaping, a key order we do not control. HMAC is over
    bytes, so a verifier that re-serialises rejects genuine requests - and the
    usual "fix" for that is to stop verifying.
    """
    spaced = b'{"object": "whatsapp_business_account" ,   "entry": [] }'
    reserialised = to_bytes(json.loads(spaced))
    assert spaced != reserialised

    assert verify_signature(spaced, _header(digest(spaced)), APP_SECRET) is True
    # A digest over the re-serialised form must NOT verify against the original.
    assert verify_signature(spaced, _header(digest(reserialised)), APP_SECRET) is False
