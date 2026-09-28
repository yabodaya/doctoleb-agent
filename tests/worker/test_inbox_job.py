"""The job envelope: claim, dispatch, retry, dead letter.

Both handlers are stubbed here, so what is under test is the machinery and
nothing else. Tasks 7 and 8 test the handlers.
"""

import logging
import uuid

import pytest
import sqlalchemy as sa
from arq.worker import Retry

from app.db.enums import InboxStatus
from app.db.models import DeadLetterJob, WebhookInbox
from app.worker.errors import PermanentJobError, RetryableJobError
from app.worker.jobs import inbox as inbox_job
from app.worker.jobs.inbox import process_inbox_event
from tests.whatsapp_factories import PATIENT_TEXT, PHONE_NUMBER_ID, phone, wamid
from tests.worker.conftest import (
    Meta,
    job_context,
    message_payload,
    meta_client,
    status_payload,
    store_event,
    worker_settings,
)

pytestmark = pytest.mark.db


@pytest.fixture
def stub_handlers(monkeypatch):
    """Replace both handlers with recorders, and let a test script the outcome."""
    calls: list[str] = []

    def install(kind: str, result=None, error: Exception | None = None):
        async def handler(context):
            calls.append(kind)
            if error is not None:
                raise error
            return result or "stubbed"

        monkeypatch.setattr(inbox_job, f"handle_{kind}", handler)

    install("message")
    install("status")
    return calls, install


async def _row(sessionmaker, event_id: uuid.UUID) -> WebhookInbox:
    async with sessionmaker() as session:
        return await session.scalar(sa.select(WebhookInbox).where(WebhookInbox.id == event_id))


async def _dead_letters(sessionmaker) -> list[DeadLetterJob]:
    async with sessionmaker() as session:
        return list((await session.scalars(sa.select(DeadLetterJob))).all())


async def test_a_claimed_event_dispatches_on_the_payload_kind(sessionmaker_for, stub_handlers):
    calls, _ = stub_handlers
    event_id = await store_event(sessionmaker_for, message_payload())

    outcome = await process_inbox_event(
        job_context(sessionmaker_for, meta_client(Meta())), str(event_id)
    )

    assert calls == ["message"]
    assert outcome == "stubbed"


async def test_a_processed_event_is_skipped_without_calling_a_handler(
    sessionmaker_for, stub_handlers
):
    """Requirement 1's worker-side idempotency.

    arq's job-id dedup is short-lived - results expire after an hour by default -
    so this is the check that holds forever.
    """
    calls, _ = stub_handlers
    event_id = await store_event(
        sessionmaker_for, message_payload(), status=InboxStatus.PROCESSED.value
    )

    outcome = await process_inbox_event(
        job_context(sessionmaker_for, meta_client(Meta())), str(event_id)
    )

    assert outcome == "skipped"
    assert calls == []


async def test_a_locked_event_is_retried_without_calling_a_handler(sessionmaker_for, stub_handlers):
    """Plan note C3a. Another worker owns this event right now."""
    import datetime as dt

    calls, _ = stub_handlers
    future = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
    event_id = await store_event(
        sessionmaker_for,
        message_payload(),
        status=InboxStatus.PROCESSING.value,
        locked_until=future,
    )

    with pytest.raises(Retry):
        await process_inbox_event(job_context(sessionmaker_for, meta_client(Meta())), str(event_id))

    assert calls == []


async def test_a_locked_event_does_not_have_its_lease_cleared(sessionmaker_for, stub_handlers):
    """The one exit path that must NOT release.

    The lease belongs to the other worker. Clearing it would hand the row
    straight back out and reintroduce the duplicate the lease prevents.
    """
    import datetime as dt

    future = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
    event_id = await store_event(
        sessionmaker_for,
        message_payload(),
        status=InboxStatus.PROCESSING.value,
        locked_until=future,
    )

    with pytest.raises(Retry):
        await process_inbox_event(job_context(sessionmaker_for, meta_client(Meta())), str(event_id))

    row = await _row(sessionmaker_for, event_id)
    assert row.locked_until is not None


async def test_a_missing_row_is_retried(sessionmaker_for, stub_handlers):
    with pytest.raises(Retry):
        await process_inbox_event(
            job_context(sessionmaker_for, meta_client(Meta())), str(uuid.uuid4())
        )


async def test_an_unknown_kind_dead_letters_immediately(sessionmaker_for, stub_handlers):
    payload = message_payload()
    payload["kind"] = "carrier_pigeon"
    event_id = await store_event(sessionmaker_for, payload)

    outcome = await process_inbox_event(
        job_context(sessionmaker_for, meta_client(Meta())), str(event_id)
    )

    assert outcome == "dead_lettered"
    letters = await _dead_letters(sessionmaker_for)
    assert len(letters) == 1
    assert letters[0].error == "unknown_kind"
    assert letters[0].attempts == 1


async def test_a_payload_with_no_phone_number_id_dead_letters(sessionmaker_for, stub_handlers):
    """Hard rule 4: no phone_number_id means no tenant, and there is nothing to
    guess with."""
    payload = message_payload()
    payload["metadata"] = {}
    event_id = await store_event(sessionmaker_for, payload)

    outcome = await process_inbox_event(
        job_context(sessionmaker_for, meta_client(Meta())), str(event_id)
    )

    assert outcome == "dead_lettered"
    assert (await _dead_letters(sessionmaker_for))[0].error == "no_phone_number_id"


async def test_an_unmapped_phone_number_id_dead_letters(sessionmaker_for, stub_handlers):
    payload = message_payload()
    payload["metadata"]["phone_number_id"] = "900000000000009"
    event_id = await store_event(sessionmaker_for, payload)

    outcome = await process_inbox_event(
        job_context(sessionmaker_for, meta_client(Meta())), str(event_id)
    )

    assert outcome == "dead_lettered"
    assert (await _dead_letters(sessionmaker_for))[0].error == "unknown_phone_number"


async def test_the_kind_is_read_from_the_payload_and_never_split_from_the_event_id(
    sessionmaker_for, stub_handlers
):
    """VS-003's note, made executable.

    The row is keyed `msg:<wamid>` while its payload says `kind: status`. A job
    that string-split the key would run the message handler. Doubly safe now -
    the job never even receives the key - but the assertion is cheap and the note
    is explicit.
    """
    calls, _ = stub_handlers
    event_id = await store_event(
        sessionmaker_for, status_payload(), provider_event_id=f"msg:{wamid(1)}"
    )

    await process_inbox_event(job_context(sessionmaker_for, meta_client(Meta())), str(event_id))

    assert calls == ["status"]


def test_the_worker_package_never_splits_a_provider_event_id():
    """Asserted against the source, because the failure is a habit, not a bug.

    `provider_event_id.split(":")` looks reasonable and would couple the worker to
    a key format that is ours and documented as changeable (VS-003's Notes).

    Comments and string literals are stripped before the check, with tokenize.
    A plain substring search over the file finds the sentence in the docstring
    explaining why not to do this, which would make the test permanently red for
    doing its job well.
    """
    import io
    import pathlib
    import token as token_module
    import tokenize

    for path in pathlib.Path("app/worker").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        code = " ".join(
            tok.string
            for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type not in (token_module.COMMENT, token_module.STRING)
        )
        assert "provider_event_id" not in code, path
        assert ".split" not in code, path


async def test_a_retryable_failure_defers_with_the_backoff_curve(sessionmaker_for, stub_handlers):
    _, install = stub_handlers
    install("message", error=RetryableJobError("meta_unreachable"))
    event_id = await store_event(sessionmaker_for, message_payload())

    with pytest.raises(Retry) as raised:
        await process_inbox_event(
            job_context(sessionmaker_for, meta_client(Meta()), job_try=2), str(event_id)
        )

    # arq stores the deferral in milliseconds.
    assert raised.value.defer_score == 10_000


async def test_a_retryable_failure_releases_the_lease(sessionmaker_for, stub_handlers):
    """Plan assumption A17.

    Holding the lease through the deferral would make the retry arrive, find its
    own stale lease, report `locked` against itself and defer again - one backoff
    curve turned into max_tries lease timeouts.
    """
    _, install = stub_handlers
    install("message", error=RetryableJobError("meta_unreachable"))
    event_id = await store_event(sessionmaker_for, message_payload())

    with pytest.raises(Retry):
        await process_inbox_event(job_context(sessionmaker_for, meta_client(Meta())), str(event_id))

    row = await _row(sessionmaker_for, event_id)
    assert row.locked_until is None
    # and the row stays claimable, which means PROCESSING, not RECEIVED
    assert row.status == InboxStatus.PROCESSING.value


async def test_a_retryable_failure_still_leaves_attempts_incremented(
    sessionmaker_for, stub_handlers
):
    """Plan amendment A1, and the reason the claim commits on its own.

    Folded into the job's own transaction, the claim - and attempts + 1 with it -
    would be rolled back by every retryable failure, so a job that failed five
    times would report one attempt in its dead letter and the retry curve would be
    invisible to whoever is triaging.
    """
    _, install = stub_handlers
    install("message", error=RetryableJobError("meta_unreachable"))
    event_id = await store_event(sessionmaker_for, message_payload())

    with pytest.raises(Retry):
        await process_inbox_event(job_context(sessionmaker_for, meta_client(Meta())), str(event_id))

    assert (await _row(sessionmaker_for, event_id)).attempts == 1


async def test_a_permanent_failure_releases_the_lease_and_marks_the_row_failed(
    sessionmaker_for, stub_handlers
):
    _, install = stub_handlers
    install("message", error=PermanentJobError("unmodelled_message"))
    event_id = await store_event(sessionmaker_for, message_payload())

    outcome = await process_inbox_event(
        job_context(sessionmaker_for, meta_client(Meta())), str(event_id)
    )

    assert outcome == "dead_lettered"
    row = await _row(sessionmaker_for, event_id)
    assert row.status == InboxStatus.FAILED.value
    assert row.locked_until is None
    assert row.last_error == "unmodelled_message"


async def test_the_last_try_dead_letters_instead_of_retrying(sessionmaker_for, stub_handlers):
    _, install = stub_handlers
    install("message", error=RetryableJobError("meta_unreachable"))
    event_id = await store_event(sessionmaker_for, message_payload())
    settings = worker_settings()

    outcome = await process_inbox_event(
        job_context(
            sessionmaker_for, meta_client(Meta()), settings=settings, job_try=settings.job_max_tries
        ),
        str(event_id),
    )

    assert outcome == "dead_lettered"
    letters = await _dead_letters(sessionmaker_for)
    assert len(letters) == 1
    assert letters[0].attempts == settings.job_max_tries
    assert letters[0].error == "meta_unreachable"


async def test_a_successful_job_leaves_no_lease_behind(sessionmaker_for, monkeypatch):
    """A handler that returns is a handler that finished.

    The handler owns marking the row PROCESSED (it is the one that knows when its
    own last commit happened), and mark() clears the lease in the same UPDATE. A
    lease left on a finished row is pure delay for whoever touches it next.
    """
    from app.db.repositories import WebhookInboxRepository

    async def handler(context):
        async with context.sessionmaker() as session:
            await WebhookInboxRepository(session).mark(context.event_id, InboxStatus.PROCESSED)
            await session.commit()
        return "replied"

    monkeypatch.setattr(inbox_job, "handle_message", handler)
    event_id = await store_event(sessionmaker_for, message_payload())

    outcome = await process_inbox_event(
        job_context(sessionmaker_for, meta_client(Meta())), str(event_id)
    )

    assert outcome == "replied"
    row = await _row(sessionmaker_for, event_id)
    assert row.status == InboxStatus.PROCESSED.value
    assert row.locked_until is None


async def test_a_dead_letter_payload_carries_no_patient_content(sessionmaker_for, stub_handlers):
    """Plan notes C7 and C2, in one assertion set.

    The payload is a REFERENCE to the event, not the event - source_event_id
    points at the inbox row that holds the whole thing. And the reference is our
    row id, not the provider_event_id, because a wamid decodes to include a phone
    number and this table is read casually.
    """
    _, install = stub_handlers
    install("message", error=PermanentJobError("unmodelled_message"))
    event_id = await store_event(
        sessionmaker_for, message_payload(), provider_event_id=f"msg:{wamid(1)}"
    )

    await process_inbox_event(job_context(sessionmaker_for, meta_client(Meta())), str(event_id))

    letter = (await _dead_letters(sessionmaker_for))[0]
    assert set(letter.payload) == {"inbox_row_id", "kind", "phone_number_id", "job_try"}
    assert letter.payload["inbox_row_id"] == str(event_id)
    assert letter.source_event_id == str(event_id)

    rendered = f"{letter.payload}{letter.error}{letter.source_event_id}"
    for forbidden in (PATIENT_TEXT, phone(1), wamid(1)):
        assert forbidden not in rendered
    # The clinic's own account id IS kept: it is what makes a dead letter triable.
    assert letter.payload["phone_number_id"] == PHONE_NUMBER_ID


async def test_the_dead_letter_has_no_tenant_when_resolution_is_what_failed(
    sessionmaker_for, stub_handlers
):
    """VS-002's reason for making dead_letter_jobs.tenant_id nullable, exercised."""
    payload = message_payload()
    payload["metadata"]["phone_number_id"] = "900000000000009"
    event_id = await store_event(sessionmaker_for, payload)

    await process_inbox_event(job_context(sessionmaker_for, meta_client(Meta())), str(event_id))

    assert (await _dead_letters(sessionmaker_for))[0].tenant_id is None


async def test_no_log_line_from_the_envelope_contains_a_wamid(
    sessionmaker_for, stub_handlers, caplog
):
    """Plan note C2, on the lines a real outage repeats five times."""
    _, install = stub_handlers
    install("message", error=RetryableJobError("meta_unreachable"))
    event_id = await store_event(
        sessionmaker_for, message_payload(), provider_event_id=f"msg:{wamid(1)}"
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(Retry):
        await process_inbox_event(job_context(sessionmaker_for, meta_client(Meta())), str(event_id))

    assert str(event_id) in caplog.text
    for forbidden in (wamid(1), PATIENT_TEXT, phone(1)):
        assert forbidden not in caplog.text
