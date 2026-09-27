"""POST a synthetic WhatsApp webhook to a local instance, correctly signed.

    uv run python scripts/sign_webhook.py "hello from the test phone"

Hard rule 9: the secret is read from .env through app.config, the same place the
app reads it, and never from an argument or from a shell you typed it into. A
command line is visible to other processes, and on Windows PowerShell's
PSReadLine writes every command to a plaintext history file that survives the
session - so `$env:META_APP_SECRET = "..."` would persist the real App Secret to
disk in a file nobody remembers to clean.

Hard rule 8: the text is whatever you pass; keep it synthetic, and never paste a
real patient message here.
"""

import hashlib
import hmac
import json
import os
import sys
import urllib.request

from app.config import get_settings

URL = os.environ.get("WEBHOOK_URL", "http://localhost:8000/webhooks/whatsapp")


def main() -> int:
    # Same source as the running app, so a mismatch between what signs the
    # request and what verifies it is impossible by construction.
    secret = get_settings().meta_app_secret
    if not secret:
        print("META_APP_SECRET is empty in .env; the endpoint would answer 401", file=sys.stderr)
        return 2

    text = sys.argv[1] if len(sys.argv) > 1 else "hello from scripts/sign_webhook.py"
    body = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "200000000000002",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "96170999999",
                                "phone_number_id": os.environ.get(
                                    "META_PHONE_NUMBER_ID", "100000000000001"
                                ),
                            },
                            "contacts": [
                                {"profile": {"name": "Local Test"}, "wa_id": "96170000001"}
                            ],
                            "messages": [
                                {
                                    "from": "96170000001",
                                    # New on every run: reuse the same id and the
                                    # second run is correctly deduplicated and
                                    # stores nothing, which looks like a bug.
                                    "id": f"wamid.LOCAL{os.urandom(4).hex()}",
                                    "timestamp": "1730000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    raw = json.dumps(body).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        URL,
        data=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310  (a localhost URL we built)
        print(response.status, response.read().decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
