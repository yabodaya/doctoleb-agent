"""The enqueue at the VS-004 seam.

Needs a real PostgreSQL, because what is asserted is which row ids the endpoint
handed to the queue, and those ids come out of the database.
"""

import logging

import pytest
import sqlalchemy as sa

from app.db.models import WebhookInbox
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

pytestmark = pytest.mark.db

PATH = "/webhooks/whatsapp"


async def _post(client, body):
    raw, headers = signed(body)
    return await client.post(PATH, content=raw, headers=headers)


async def test_one_message_enqueues_one_job_carrying_its_row_id(
    client, configure, use_database, queue
):
    assert (await _post(client, envelope(messages=[text_message(1)]))).status_code == 200

    row = await use_database.scalar(sa.select(WebhookInbox))
    assert queue.enqueued == [row.id]


async def test_three_messages_and_two_statuses_enqueue_five_jobs(
    client, configure, use_database, queue
):
    """One POST is many events (VS-003's dedupe granularity), so one POST is
    many jobs."""
    body = envelope(
        messages=[text_message(n) for n in (1, 2, 3)],
        statuses=[status_update(4, "sent"), status_update(5, "delivered")],
    )

    assert (await _post(client, body)).status_code == 200

    stored = (await use_database.scalars(sa.select(WebhookInbox.id))).all()
    assert len(queue.enqueued) == 5
    assert set(queue.enqueued) == set(stored)


async def test_a_redelivered_event_that_is_already_stored_is_enqueued_again(
    client, configure, use_database, queue
):
    """Plan note C8, and requirement 1's explicit instruction.

    The tempting reading - "it is already stored, so it is already handled" -
    loses the message whenever the FIRST enqueue is what failed, which is one of
    the reasons Meta is redelivering in the first place. The repeat is harmless
    because the row id is the same one as last time, so arq refuses the job id
    and the worker's claim refuses an event already PROCESSED.
    """
    body = envelope(messages=[text_message(1)])

    assert (await _post(client, body)).status_code == 200
    assert (await _post(client, body)).status_code == 200

    rows = (await use_database.scalars(sa.select(WebhookInbox.id))).all()
    assert len(rows) == 1
    assert queue.enqueued == [rows[0], rows[0]]


async def test_the_row_id_for_a_duplicate_is_looked_up_not_invented(
    client, configure, use_database, queue
):
    """The duplicate path's whole reason for existing.

    store_if_new returns None on conflict. A uuid4() fallback here would look
    fine and silently defeat both arq's dedup and the worker's lease, because
    every redelivery would arrive under a job id nobody had seen.
    """
    body = envelope(messages=[text_message(1)])
    await _post(client, body)
    await _post(client, body)

    assert queue.enqueued[0] == queue.enqueued[1]


async def test_the_duplicate_lookup_runs_only_for_duplicates(
    client, configure, use_database, queue, monkeypatch
):
    """One query on the unusual path, none on the normal one."""
    from app.db.repositories import WebhookInboxRepository

    calls = []
    original = WebhookInboxRepository.get_by_event_id

    async def counting(self, provider_event_id):
        calls.append(provider_event_id)
        return await original(self, provider_event_id)

    monkeypatch.setattr(WebhookInboxRepository, "get_by_event_id", counting)

    await _post(client, envelope(messages=[text_message(n) for n in (1, 2)]))
    assert calls == []

    await _post(client, envelope(messages=[text_message(1)]))
    assert calls == [f"msg:{wamid(1)}"]


async def test_nothing_is_enqueued_when_there_is_nothing_to_store(
    client, configure, no_database, queue
):
    """An unmodelled payload answers 200 and touches neither the database nor
    the queue. Hard rule 1: a shape Meta added is not a reason to do work."""
    response = await _post(client, {"object": "whatsapp_business_account", "entry": []})

    assert response.status_code == 200
    assert queue.enqueued == []


async def test_an_enqueue_failure_answers_503_and_does_not_lose_the_rows(
    client, configure, use_database, failing_queue
):
    """Plan note C1.

    200 here would be the silent loss the storage path's 503 exists to prevent,
    one layer further in: the rows are committed, nothing will ever process them,
    and 200 tells Meta to forget the event. 503 is retryable, so Meta comes back,
    the rows dedupe, their ids are looked up again, and the enqueue is retried.
    """
    response = await _post(client, envelope(messages=[text_message(1)]))

    assert response.status_code == 503
    assert response.json()["detail"] == "queue unavailable"
    # The rows stay: they are correct, and the redelivery will re-enqueue them.
    assert await use_database.scalar(sa.select(sa.func.count()).select_from(WebhookInbox)) == 1


async def test_the_enqueue_failure_log_carries_row_ids_and_no_wamid(
    client, configure, use_database, failing_queue, caplog
):
    """Plan note C2, on the line most likely to be read during an incident."""
    with caplog.at_level(logging.INFO):
        await _post(client, envelope(messages=[text_message(1)]))

    failure_lines = [r.getMessage() for r in caplog.records if "enqueue failed" in r.getMessage()]
    assert failure_lines, "the enqueue failure was not logged at all"
    rendered = "\n".join(failure_lines)
    row = await use_database.scalar(sa.select(WebhookInbox))
    assert str(row.id) in rendered
    assert wamid(1) not in rendered
    assert PATIENT_TEXT not in rendered
    assert phone(1) not in rendered


async def test_the_enqueue_happens_after_the_commit(
    client, configure, use_database, app, monkeypatch
):
    """A job that started before the commit would find no row, and would
    dead-letter a message that was never actually lost.

    Asserted as an ORDERING, by spying on the session's commit, rather than by
    reading the row from a second connection: `use_database` wraps everything in
    one transaction that is rolled back, with join_transaction_mode=
    "create_savepoint", so the endpoint's commit releases a savepoint and is by
    design invisible to any other connection. A visibility check here would fail
    against correct code.
    """
    from app.queue import get_job_queue

    events: list[str] = []
    original_commit = type(use_database).commit

    async def spying_commit(self):
        events.append("commit")
        return await original_commit(self)

    monkeypatch.setattr(type(use_database), "commit", spying_commit)

    class OrderRecordingQueue:
        async def enqueue_inbox_event(self, row_id):
            events.append("enqueue")

    app.dependency_overrides[get_job_queue] = lambda: OrderRecordingQueue()

    await _post(client, envelope(messages=[text_message(1)]))

    assert events == ["commit", "enqueue"]


async def test_no_log_line_from_the_enqueue_path_contains_patient_content(
    client, configure, use_database, caplog
):
    with caplog.at_level(logging.DEBUG):
        await _post(client, envelope(messages=[text_message(1)]))

    for value in (PATIENT_TEXT, PROFILE_NAME, phone(1)):
        assert value not in caplog.text
