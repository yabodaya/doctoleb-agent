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
