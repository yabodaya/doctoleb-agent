"""The rows. Needs a real PostgreSQL: a unique constraint cannot be mocked.

Every test drives the endpoint over HTTP and then reads webhook_inbox through the
same session, so what is asserted is what a deployed app would have written.
"""

import logging

import pytest
import sqlalchemy as sa

from app.db.models import WebhookInbox
from tests.whatsapp_factories import (
    PATIENT_TEXT,
    PHONE_NUMBER_ID,
    envelope,
    signed,
    status_update,
    text_message,
    wamid,
)

pytestmark = pytest.mark.db

PATH = "/webhooks/whatsapp"


async def _post(client, body) -> int:
    raw, headers = signed(body)
    response = await client.post(PATH, content=raw, headers=headers)
    return response.status_code


async def _event_ids(session) -> list[str]:
    result = await session.scalars(
        sa.select(WebhookInbox.provider_event_id).order_by(WebhookInbox.provider_event_id)
    )
    return list(result.all())


async def test_one_message_becomes_one_inbox_row_with_no_tenant(client, configure, use_database):
    assert await _post(client, envelope(messages=[text_message(1)])) == 200

    row = await use_database.scalar(sa.select(WebhookInbox))
    assert row.provider_event_id == f"msg:{wamid(1)}"
    assert row.provider == "whatsapp"
    assert row.status == "RECEIVED"
    assert row.attempts == 0
    # Hard rule 4: the tenant is resolved by the worker from phone_number_id.
    # Resolving it here would mean doing work inside the webhook (hard rule 1).
    assert row.tenant_id is None


async def test_the_same_post_twice_stores_one_row_per_item(client, configure, use_database):
    """Hard rule 2, and the whole reason a 503 is safe.

    Meta redelivers on its own schedule and after any failure. Two identical
    deliveries must leave one row - enforced by the unique constraint, not by us
    checking first.
    """
    body = envelope(messages=[text_message(1)])

    assert await _post(client, body) == 200
    assert await _post(client, body) == 200

    assert await _event_ids(use_database) == [f"msg:{wamid(1)}"]


async def test_messages_and_statuses_in_one_post_become_one_row_each(
    client, configure, use_database
):
    """Review Focus 3. One row per request would lose four of these five."""
    body = envelope(
        messages=[text_message(1), text_message(2), text_message(3)],
        statuses=[status_update(8, "delivered"), status_update(9, "read")],
    )

    assert await _post(client, body) == 200

    assert await _event_ids(use_database) == sorted(
        [
            f"msg:{wamid(1)}",
            f"msg:{wamid(2)}",
            f"msg:{wamid(3)}",
            f"status:{wamid(8)}:delivered",
            f"status:{wamid(9)}:read",
        ]
    )


async def test_a_post_mixing_a_new_and_a_stored_message_stores_only_the_new_one(
    client, configure, use_database
):
    # Meta's retries are not always byte-identical: a redelivery can carry one
    # event we already have and one we do not. Per-item dedupe keeps both facts.
    assert await _post(client, envelope(messages=[text_message(1)])) == 200
    assert await _post(client, envelope(messages=[text_message(1), text_message(2)])) == 200

    assert await _event_ids(use_database) == [f"msg:{wamid(1)}", f"msg:{wamid(2)}"]


async def test_three_statuses_for_one_message_are_three_rows(client, configure, use_database):
    """Review Focus 6 and assumption A8 in one test.

    sent/delivered/read for one wamid are three events; the same status twice is
    one. Both halves matter to VS-004's message status column.
    """
    assert await _post(client, envelope(statuses=[status_update(1, "sent")])) == 200
    assert await _post(client, envelope(statuses=[status_update(1, "sent")])) == 200
    assert (
        await _post(
            client,
            envelope(statuses=[status_update(1, "delivered"), status_update(1, "read")]),
        )
        == 200
    )

    assert await _event_ids(use_database) == sorted(
        [f"status:{wamid(1)}:{s}" for s in ("sent", "delivered", "read")]
    )


async def test_the_stored_payload_is_what_vs004_needs(client, configure, use_database):
    """Review Focus 7, through JSONB and back.

    The worker reads this row and nothing else. Anything missing here is
    unrecoverable - the request is long gone.
    """
    body = envelope(messages=[text_message(1, context={"id": wamid(7)})])
    assert await _post(client, body) == 200

    row = await use_database.scalar(sa.select(WebhookInbox))
    assert row.payload["kind"] == "message"
    assert row.payload["metadata"]["phone_number_id"] == PHONE_NUMBER_ID
    assert row.payload["item"]["id"] == wamid(1)
    assert row.payload["item"]["text"]["body"] == PATIENT_TEXT
    # An undeclared key survived validation, JSONB and the round trip. VS-008's
    # audio.id arrives the same way.
    assert row.payload["item"]["context"] == {"id": wamid(7)}


async def test_a_storage_failure_answers_503_without_propagating_the_exception(
    client, configure, use_database, monkeypatch, caplog
):
    """Review Focus 4, all three halves of it.

    `except: return 200` would turn a database outage into permanently lost
    patient messages, because a 200 tells Meta to forget the event. Re-raising
    the original error would answer 500 and hand uvicorn a traceback whose
    message can quote the offending data. So: 503, and nothing about the cause
    leaves the process.

    The NORMAL client is the assertion, not a detail. ASGITransport re-raises
    application exceptions by default, so if `receive()` ever let the RuntimeError
    escape, this test would error out with that exception instead of reading a
    response - which is exactly the failure we want to be told about.
    """

    async def boom(self, provider_event_id, payload, provider="whatsapp"):
        # The message imitates a PostgreSQL error quoting the offending value.
        raise RuntimeError(f"invalid input syntax for type json, CONTEXT: {PATIENT_TEXT}")

    monkeypatch.setattr("app.api.whatsapp.WebhookInboxRepository.store_if_new", boom)
    raw, headers = signed(envelope(messages=[text_message(1)]))

    with caplog.at_level(logging.ERROR):
        response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 503
    assert await _event_ids(use_database) == []
    # The class name is in the log; the message the exception carried is not,
    # anywhere - not in the log, not in the response (hard rule 8).
    assert "RuntimeError" in caplog.text
    assert PATIENT_TEXT not in caplog.text
    assert PATIENT_TEXT not in response.text


async def test_every_row_from_one_post_lands_in_one_transaction(
    client, configure, use_database, monkeypatch
):
    """Half a delivery is not a correctness bug - Meta redelivers and dedupe
    absorbs it - but it makes "was this request stored?" unanswerable. One POST
    is one transaction."""
    from app.db.repositories import WebhookInboxRepository

    real = WebhookInboxRepository.store_if_new
    calls: list[str] = []

    async def fail_on_the_second(self, provider_event_id, payload, provider="whatsapp"):
        calls.append(provider_event_id)
        if len(calls) == 2:
            raise RuntimeError("second insert exploded")
        return await real(self, provider_event_id, payload, provider)

    monkeypatch.setattr("app.api.whatsapp.WebhookInboxRepository.store_if_new", fail_on_the_second)
    raw, headers = signed(envelope(messages=[text_message(1), text_message(2)]))

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 503
    assert len(calls) == 2
    # The first insert really happened, and was rolled back with the second.
    assert await _event_ids(use_database) == []
