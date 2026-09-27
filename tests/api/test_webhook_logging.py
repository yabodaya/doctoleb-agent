"""Hard rule 8 at the endpoint: log identifiers, never content.

This is the first slice that handles a real patient's words, and the tempting log
line - "received: <body>" - breaks the rule on day one. The failure paths are the
sneaky ones: a 400 that echoes the body it could not parse, or a logger.exception
that carries the statement's parameters.
"""

import logging

import pytest

from tests.whatsapp_factories import (
    PATIENT_TEXT,
    PROFILE_NAME,
    envelope,
    phone,
    signed,
    status_update,
    text_message,
    wamid,
)

PATH = "/webhooks/whatsapp"


def assert_no_patient_content(caplog):
    """The four things that identify a person in a WhatsApp payload."""
    assert PATIENT_TEXT not in caplog.text
    assert PROFILE_NAME not in caplog.text
    assert phone(1) not in caplog.text
    assert "messaging_product" not in caplog.text  # i.e. no raw body anywhere


async def test_a_rejected_signature_logs_no_body(client, configure, no_database, caplog):
    raw, _ = signed(envelope(messages=[text_message(1)]))

    with caplog.at_level(logging.INFO):
        response = await client.post(
            PATH, content=raw, headers={"X-Hub-Signature-256": "sha256=00"}
        )

    assert response.status_code == 401
    assert_no_patient_content(caplog)


async def test_an_unparseable_body_logs_no_body(client, configure, no_database, caplog):
    # The 400 response and its log line must not echo what could not be parsed.
    broken = b'{"messaging_product": "whatsapp", "text": "' + PATIENT_TEXT.encode() + b'"'
    raw, headers = signed(broken)

    with caplog.at_level(logging.INFO):
        response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 400
    assert_no_patient_content(caplog)
    assert PATIENT_TEXT not in response.text


@pytest.mark.db
async def test_a_stored_webhook_logs_event_ids_and_never_content(
    client, configure, use_database, caplog
):
    """Hard rule 8 on the path that actually carries a patient's words.

    Logging the ids is the point - without them a production incident has nothing
    to correlate. Logging the body is the violation.
    """
    raw, headers = signed(envelope(messages=[text_message(1)], statuses=[status_update(2, "read")]))

    with caplog.at_level(logging.INFO):
        response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 200
    assert f"msg:{wamid(1)}" in caplog.text
    assert f"status:{wamid(2)}:read" in caplog.text
    assert_no_patient_content(caplog)
