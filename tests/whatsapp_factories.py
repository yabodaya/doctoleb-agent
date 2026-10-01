"""Synthetic Meta webhook payloads, and the signing helper tests use.

Hard rule 8: no fixture in this repo is built from a real delivery. Every value
here is derived from an integer, the phone numbers come from the same
documentation-safe range as tests/db/factories.py, and the secrets are obvious
fakes (hard rule 9 - a real one must never reach a test file).
"""

import hashlib
import hmac
import json
from typing import Any

APP_SECRET = "test-app-secret-not-a-real-one"
VERIFY_TOKEN = "test-verify-token-not-a-real-one"
PHONE_NUMBER_ID = "100000000000001"
WABA_ID = "200000000000002"

# The text a synthetic patient sends, and the synthetic profile name. Both are
# asserted ABSENT from every log line.
PATIENT_TEXT = "synthetic message body"
PROFILE_NAME = "Synthetic Patient"


def phone(n: int = 1) -> str:
    return f"96170{n:06d}"


def wamid(n: int = 1) -> str:
    return f"wamid.TEST{n:08d}"


def text_message(n: int = 1, body: str = PATIENT_TEXT, **extra: Any) -> dict[str, Any]:
    """One inbound text message, in Meta's shape."""
    message: dict[str, Any] = {
        "from": phone(n),
        "id": wamid(n),
        "timestamp": "1730000000",
        "type": "text",
        "text": {"body": body},
    }
    message.update(extra)
    return message


# --------------------------------------------------------------------------
# VS-008: voice notes. Synthetic, like everything else in this module.
# --------------------------------------------------------------------------

MEDIA_ID = "media-id-0000001"
OGG_MIME = "audio/ogg; codecs=opus"
# Not a real Ogg stream and not meant to be: no test decodes it, and a real
# recording in a fixture would be a real person's voice (hard rule 8). Four
# bytes of the Ogg magic so it is a distinguishable payload, and sixty zeros so
# it has a length worth asserting on.
SYNTHETIC_OGG = b"OggS" + bytes(60)
# What a lookup answers with. The hostname is the one Meta is observed to use
# and the one MEDIA_HOST_SUFFIXES allows; the query string is nonsense on
# purpose, because a test asserts it never reaches a log line.
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=synthetic&ext=1&hash=x"


def audio_message(
    n: int = 1,
    media_id: str = MEDIA_ID,
    mime_type: str = OGG_MIME,
    voice: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """One inbound voice note, in Meta's shape.

    Meta's `audio` object carries an id, a mime type, a sha256 and `voice` -
    and, notably, NO duration (plan conflict C15), which is why the cap this
    repo enforces is a byte cap. `voice` is true for a recorded note and false
    for an audio file the patient attached; both are a VOICE_NOTE to us.
    """
    message: dict[str, Any] = {
        "from": phone(n),
        "id": wamid(n),
        "timestamp": "1730000000",
        "type": "audio",
        "audio": {
            "id": media_id,
            "mime_type": mime_type,
            "voice": voice,
            # Deliberately NOT stored by us (W13): it is a fingerprint of the
            # patient's audio and buys nothing once the audio is gone.
            "sha256": f"sha256-of-nothing-{n:08d}",
        },
    }
    message.update(extra)
    return message


def media_lookup_body(
    url: str = MEDIA_URL,
    mime_type: str = OGG_MIME,
    file_size: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """What the Graph API answers a media lookup with."""
    body: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "url": url,
        "mime_type": mime_type,
        "sha256": "sha256-of-nothing-00000001",
        "file_size": len(SYNTHETIC_OGG) if file_size is None else file_size,
        "id": MEDIA_ID,
    }
    body.update(extra)
    return body


def status_update(n: int = 1, state: str = "sent", **extra: Any) -> dict[str, Any]:
    status: dict[str, Any] = {
        "id": wamid(n),
        "status": state,
        "timestamp": "1730000001",
        "recipient_id": phone(n),
        "conversation": {"id": f"conv-{n:08d}"},
    }
    status.update(extra)
    return status


def contact(n: int = 1) -> dict[str, Any]:
    return {"profile": {"name": PROFILE_NAME}, "wa_id": phone(n)}


def envelope(
    messages: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
    contacts: list[dict[str, Any]] | None = None,
    field: str = "messages",
    with_metadata: bool = True,
) -> dict[str, Any]:
    """A full webhook body. Only the keys Meta actually sends."""
    value: dict[str, Any] = {"messaging_product": "whatsapp"}
    if with_metadata:
        value["metadata"] = {
            "display_phone_number": phone(999),
            "phone_number_id": PHONE_NUMBER_ID,
        }
    if messages is not None:
        value["contacts"] = contacts if contacts is not None else [contact()]
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": WABA_ID, "changes": [{"field": field, "value": value}]}],
    }


def to_bytes(payload: dict[str, Any] | str | bytes) -> bytes:
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload).encode("utf-8")


def digest(payload: dict[str, Any] | str | bytes, secret: str = APP_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), to_bytes(payload), hashlib.sha256).hexdigest()


def signed(
    payload: dict[str, Any] | str | bytes, secret: str = APP_SECRET
) -> tuple[bytes, dict[str, str]]:
    """The exact bytes to send, and the header that signs those exact bytes.

    Returning both together is the point: a test that builds the body twice -
    once to sign, once to send - can pass while the endpoint re-serialises, which
    is the bug Review Focus 1 exists to catch.
    """
    raw = to_bytes(payload)
    return raw, {"X-Hub-Signature-256": f"sha256={digest(raw, secret)}"}
