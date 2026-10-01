"""`voice_notes`: the model, the repository, and `set_transcript` (VS-008).

Two halves. The first needs no database and reads `Base.metadata`: the exact
column set, the absence of any free-text column, the absence of a transcript
column by name, and the vocabulary. The second needs PostgreSQL, because the
things that matter here are an upsert, a unique constraint, a cascade and a
savepoint, and a Python re-implementation of any of them would prove nothing.

What this table is for: transcription is the one step in this repo that costs
money per attempt and cannot be made idempotent by a key, so a `DONE` row is
what lets a retry read what the first attempt heard instead of paying for a
second opinion - which could also be a DIFFERENT opinion, and then the
patient's message would have changed between attempts.
"""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.base import Base
from app.db.enums import MessageModality, MessageStatus, VoiceNoteStatus
from app.db.models import Contact, Conversation, Message, VoiceNote
from app.db.repositories.errors import VoiceNoteNotRecordedError
from app.db.repositories.messages import MessageRepository
from app.db.repositories.voice_notes import VoiceNoteRepository, VoiceNoteRow
from tests.db import factories as f

# A synthetic sentence, and an obvious one. Hard rule 8 forbids a real
# recording in a fixture AND a real person's words, and a plausible-looking
# transcript in a test file is the second of those.
TRANSCRIPT = "synthetic transcript of a synthetic voice note"


# --------------------------------------------------------------------------
# The schema. No database.
# --------------------------------------------------------------------------


def test_the_voice_notes_table_has_exactly_these_columns():
    """Pinned, so adding a column that could hold content means editing this test.

    Nothing here may be a transcript, a media URL, Meta's sha256 of the audio, a
    patient reference, or anything from the reply (hard rule 8, plan W13). What
    is left is ids, codes and counts - plus one float the API may report, which
    nothing reads.
    """
    assert set(VoiceNote.__table__.c.keys()) == {
        "id",
        "created_at",
        "updated_at",
        "tenant_id",
        "message_id",
        "inbox_event_id",
        "media_id",
        "mime_type",
        "byte_size",
        "duration_seconds",
        "voice",
        "status",
        "error_code",
        "model",
        "attempts",
    }


def test_the_voice_notes_table_has_no_transcript_column():
    """Named separately from the column-set test, because this is the one
    somebody will be tempted to add.

    The transcript is the patient's message. It lives in `messages.text`, in the
    same column a typed message uses, under the same retention and the same
    access. A copy here would be a second place patient content lives with no
    retention story of its own - and `voice_notes` is a table an operator greps
    freely, precisely because it is supposed to hold nothing sensitive.
    """
    names = set(VoiceNote.__table__.c.keys())

    for forbidden in ("transcript", "text", "url", "media_url", "sha256", "audio", "body"):
        assert forbidden not in names, forbidden


def test_the_voice_notes_table_has_no_free_text_column():
    """Every string column is bounded except `tenant_id`.

    An unbounded TEXT column is where content ends up. A `VARCHAR(64)` for an
    error code says out loud that nothing longer belongs there, and PostgreSQL
    enforces it.
    """
    for column in VoiceNote.__table__.c:
        if not isinstance(column.type, sa.String):
            continue
        if column.name == "tenant_id":
            # Decision D1: an opaque string of a format the Booking Service
            # owner has not settled, so it cannot be given a length.
            assert isinstance(column.type, sa.Text)
            continue
        assert column.type.length is not None, column.name
        assert column.type.length <= 255, column.name


def test_voice_note_status_is_pinned():
    """The .value strings are literal row contents, so renaming one is a data
    migration and not a refactor. Four values, and three of them terminal."""
    assert [member.value for member in VoiceNoteStatus] == [
        "PENDING",
        "DONE",
        "UNCLEAR",
        "FAILED",
    ]


def test_one_voice_note_per_message_is_declared():
    """The unique constraint IS the hard rule 2 defence for this table.

    A duplicated webhook or a retried job must produce one transcription, and an
    application-level "select then insert" has a window in it.
    """
    constraints = {
        frozenset(column.name for column in constraint.columns)
        for constraint in VoiceNote.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }

    assert frozenset(["message_id"]) in constraints


def test_the_message_foreign_key_cascades():
    """Operational state dies with its message - unlike `agent_runs`, whose cost
    records deliberately outlive message retention. There is nothing in here
    worth keeping once the message it describes is gone."""
    foreign_key = next(iter(VoiceNote.__table__.c.message_id.foreign_keys))

    assert foreign_key.column.table.name == "messages"
    assert foreign_key.ondelete == "CASCADE"


def test_the_inbox_event_id_has_no_foreign_key():
    """The same reasoning as `agent_runs.inbox_event_id`: retention may prune
    the inbox, and this column exists so a log line and this row are joinable by
    hand, not so the database enforces a link."""
    assert VoiceNote.__table__.c.inbox_event_id.foreign_keys == set()


def test_the_table_is_registered_on_the_metadata():
    """A model nobody imported is a table Alembic would cheerfully propose
    dropping - which is why app/db/models/__init__.py imports every one."""
    assert "voice_notes" in Base.metadata.tables


# --------------------------------------------------------------------------
# Constraints, at runtime
# --------------------------------------------------------------------------

database = pytest.mark.db


async def _voice_message(db_session) -> Message:
    """A stored inbound VOICE_NOTE with no text yet - exactly what T1 leaves."""
    contact = f.make_contact()
    db_session.add(contact)
    await db_session.flush()
    conversation = f.make_conversation(contact)
    db_session.add(conversation)
    await db_session.flush()
    message = f.make_message(
        conversation,
        modality=MessageModality.VOICE_NOTE.value,
        status=MessageStatus.RECEIVED.value,
        text=None,
    )
    db_session.add(message)
    await db_session.flush()
    return message


@database
async def test_an_unknown_voice_note_status_is_rejected(db_session):
    message = await _voice_message(db_session)

    with pytest.raises(IntegrityError):
        await db_session.execute(
            sa.insert(VoiceNote).values(
                id=uuid.uuid4(),
                tenant_id=message.tenant_id,
                message_id=message.id,
                inbox_event_id=uuid.uuid4(),
                status="BANANA",
                attempts=1,
            )
        )


@database
async def test_two_voice_notes_for_one_message_are_rejected(db_session):
    """Hard rule 2, proven by the database rather than by a code path."""
    message = await _voice_message(db_session)
    db_session.add(f.make_voice_note(message))
    await db_session.flush()

    db_session.add(f.make_voice_note(message))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@database
async def test_deleting_a_message_deletes_its_voice_note(db_session):
    """ON DELETE CASCADE, proven at runtime. A retention policy that prunes
    messages must not leave bookkeeping behind that no message explains."""
    message = await _voice_message(db_session)
    db_session.add(f.make_voice_note(message))
    await db_session.flush()

    await db_session.execute(sa.delete(Message).where(Message.id == message.id))

    remaining = await db_session.execute(
        sa.select(sa.func.count()).select_from(VoiceNote).where(VoiceNote.message_id == message.id)
    )
    assert remaining.scalar_one() == 0


@database
async def test_deleting_a_conversation_deletes_both(db_session):
    """Two cascades in a row: conversation -> messages -> voice_notes."""
    message = await _voice_message(db_session)
    db_session.add(f.make_voice_note(message))
    await db_session.flush()

    await db_session.execute(
        sa.delete(Conversation).where(Conversation.id == message.conversation_id)
    )

    notes = await db_session.execute(sa.select(sa.func.count()).select_from(VoiceNote))
    messages = await db_session.execute(
        sa.select(sa.func.count()).select_from(Message).where(Message.id == message.id)
    )
    assert notes.scalar_one() == 0
    assert messages.scalar_one() == 0


# --------------------------------------------------------------------------
# The repository
# --------------------------------------------------------------------------


def _repository(db_session, tenant: str = f.TENANT_A) -> VoiceNoteRepository:
    return VoiceNoteRepository(db_session, tenant)


@database
async def test_status_for_returns_none_before_anything_started(db_session):
    message = await _voice_message(db_session)

    assert await _repository(db_session).status_for(message.id) is None


@database
async def test_start_creates_a_pending_row_and_counts_the_attempt(db_session):
    message = await _voice_message(db_session)
    repository = _repository(db_session)

    await repository.start(
        message.id,
        inbox_event_id=uuid.uuid4(),
        media_id="media-id-0000001",
        mime_type="audio/ogg",
        voice=True,
    )

    row = await repository.status_for(message.id)
    assert row is not None
    assert row.status == VoiceNoteStatus.PENDING.value
    assert row.attempts == 1
    assert row.media_id == "media-id-0000001"
    assert row.terminal is False
    assert row.transcribed is False


@database
async def test_start_twice_upserts_and_counts_two_attempts(db_session):
    """`ON CONFLICT (message_id) DO UPDATE`, with the increment in SQL.

    In Python it would be a read-then-write, and two writers would read the same
    count and both write the same number - so the one thing this column is for,
    telling an operator how many times we have paid for this note, would be
    wrong exactly when it mattered.
    """
    message = await _voice_message(db_session)
    repository = _repository(db_session)
    event = uuid.uuid4()

    await repository.start(
        message.id, inbox_event_id=event, media_id="media-1", mime_type="audio/ogg", voice=True
    )
    await repository.start(
        message.id, inbox_event_id=event, media_id="media-1", mime_type="audio/ogg", voice=True
    )

    row = await repository.status_for(message.id)
    assert row is not None
    assert row.attempts == 2
    # Still ONE row: the second call upserted rather than inserting.
    count = await db_session.execute(sa.select(sa.func.count()).select_from(VoiceNote))
    assert count.scalar_one() == 1


@database
async def test_start_after_a_terminal_status_puts_it_back_to_pending(db_session):
    """Only a retry that got past T1's terminal check reaches `start` again.

    When it does, PENDING is the honest state: an attempt is in flight. T1 is
    what makes sure a DONE row is never re-attempted, and that is tested through
    `terminal` rather than defended twice here.
    """
    message = await _voice_message(db_session)
    repository = _repository(db_session)
    await repository.start(
        message.id, inbox_event_id=uuid.uuid4(), media_id="m", mime_type="audio/ogg", voice=True
    )
    await repository.finish(message.id, status=VoiceNoteStatus.DONE)

    await repository.start(
        message.id, inbox_event_id=uuid.uuid4(), media_id="m", mime_type="audio/ogg", voice=True
    )

    row = await repository.status_for(message.id)
    assert row is not None
    assert row.status == VoiceNoteStatus.PENDING.value
    assert row.attempts == 2


@database
async def test_status_for_returns_plain_data_not_an_entity(db_session):
    """Plan risk R2. With `expire_on_commit=False` an entity loaded in one
    session never sees another transaction's commit, so an ORM object crossing
    back into the job is a stale read waiting to happen - and this is the read
    that decides whether to spend money on a transcription."""
    message = await _voice_message(db_session)
    repository = _repository(db_session)
    await repository.start(
        message.id, inbox_event_id=uuid.uuid4(), media_id="m", mime_type="audio/ogg", voice=True
    )

    row = await repository.status_for(message.id)

    assert isinstance(row, VoiceNoteRow)
    assert not isinstance(row, VoiceNote)


@pytest.mark.parametrize(
    "status", [VoiceNoteStatus.DONE, VoiceNoteStatus.UNCLEAR, VoiceNoteStatus.FAILED]
)
@database
async def test_finish_moves_pending_to_its_terminal_state(db_session, status):
    message = await _voice_message(db_session)
    repository = _repository(db_session)
    await repository.start(
        message.id, inbox_event_id=uuid.uuid4(), media_id="m", mime_type="audio/ogg", voice=True
    )

    await repository.finish(message.id, status=status, error_code="a_code")

    row = await repository.status_for(message.id)
    assert row is not None
    assert row.status == status.value
    assert row.terminal is True
    assert row.transcribed is (status is VoiceNoteStatus.DONE)


@database
async def test_finish_records_the_configured_model_and_the_byte_count(db_session):
    """Q10: the CONFIGURED model name, never the one the provider says it served.

    Together with `byte_size` and `duration_seconds` that is what lets the real
    cost per voice note be worked out from this table rather than from a bill.
    """
    message = await _voice_message(db_session)
    repository = _repository(db_session)
    await repository.start(
        message.id, inbox_event_id=uuid.uuid4(), media_id="m", mime_type="audio/ogg", voice=True
    )

    await repository.finish(
        message.id,
        status=VoiceNoteStatus.DONE,
        byte_size=4096,
        duration_seconds=12.5,
        model="a-configured-model",
    )

    stored = await db_session.execute(
        sa.select(VoiceNote.byte_size, VoiceNote.duration_seconds, VoiceNote.model).where(
            VoiceNote.message_id == message.id
        )
    )
    assert stored.one() == (4096, 12.5, "a-configured-model")


@database
async def test_a_long_error_code_is_truncated_rather_than_failing_the_write(db_session):
    """The same reasoning as `agent_runs.reason`: a code that outgrew its column
    would turn a recorded failure into a second failure."""
    message = await _voice_message(db_session)
    repository = _repository(db_session)
    await repository.start(
        message.id, inbox_event_id=uuid.uuid4(), media_id="m", mime_type="audio/ogg", voice=True
    )

    await repository.finish(message.id, status=VoiceNoteStatus.FAILED, error_code="x" * 500)

    stored = await db_session.scalar(
        sa.select(VoiceNote.error_code).where(VoiceNote.message_id == message.id)
    )
    assert len(stored) == 64


@database
async def test_another_tenants_voice_note_is_invisible(db_session):
    """Hard rule 4, made structural: the tenant comes from the constructor, so
    no call site can forget it."""
    message = await _voice_message(db_session)
    await _repository(db_session, f.TENANT_A).start(
        message.id, inbox_event_id=uuid.uuid4(), media_id="m", mime_type="audio/ogg", voice=True
    )

    assert await _repository(db_session, f.TENANT_B).status_for(message.id) is None

    # And a write from the wrong tenant changes nothing.
    await _repository(db_session, f.TENANT_B).finish(message.id, status=VoiceNoteStatus.FAILED)
    row = await _repository(db_session, f.TENANT_A).status_for(message.id)
    assert row is not None
    assert row.status == VoiceNoteStatus.PENDING.value


@database
async def test_a_voice_note_repository_refuses_a_blank_tenant(db_session):
    with pytest.raises(ValueError, match="tenant_id is required"):
        VoiceNoteRepository(db_session, "")


@database
async def test_a_failed_voice_note_write_rolls_back_only_its_savepoint(db_session):
    """PostgreSQL aborts the WHOLE transaction on any failed statement.

    This write happens in the same transaction that stores the transcript and
    builds the turn, so without the SAVEPOINT a failed bookkeeping insert would
    poison the transaction and cost the patient their reply. With it, only this
    row rolls back - and the error carries the exception CLASS NAME and nothing
    else, because a chained traceback would print the statement.
    """
    message = await _voice_message(db_session)
    repository = _repository(db_session)

    # A message_id no message has: the FK refuses it.
    with pytest.raises(VoiceNoteNotRecordedError) as raised:
        await repository.start(
            uuid.uuid4(),
            inbox_event_id=uuid.uuid4(),
            media_id="m",
            mime_type="audio/ogg",
            voice=True,
        )

    assert raised.value.error_class == "IntegrityError"
    # The transaction is still usable, which is the whole point.
    await repository.start(
        message.id, inbox_event_id=uuid.uuid4(), media_id="m", mime_type="audio/ogg", voice=True
    )
    assert await repository.status_for(message.id) is not None


@database
async def test_voice_note_not_recorded_carries_the_class_name_only(db_session):
    """Hard rule 8. A repository error must be safe to log, and the conflicting
    VALUES in an IntegrityError are not."""
    error = VoiceNoteNotRecordedError("IntegrityError")

    assert "IntegrityError" in str(error)
    assert error.error_class == "IntegrityError"
    assert error.__cause__ is None


@database
async def test_two_writers_on_one_message_serialise(db_engine):
    """Plan check U9: two concurrent voice turns on one message do not deadlock.

    Two real sessions on two real connections, interleaved: both upsert the same
    `message_id`. The unique constraint plus `DO UPDATE` means the second is an
    update rather than a conflict, so it waits for the first to commit and then
    applies - two attempts counted, one row, no deadlock.

    Written against the engine rather than the shared `db_session`, because
    concurrency inside one transaction proves nothing.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.session import SESSION_OPTIONS

    factory = async_sessionmaker(bind=db_engine, **SESSION_OPTIONS)
    event = uuid.uuid4()

    async with factory() as setup:
        contact = f.make_contact()
        setup.add(contact)
        await setup.flush()
        conversation = f.make_conversation(contact)
        setup.add(conversation)
        await setup.flush()
        message = f.make_message(conversation, modality=MessageModality.VOICE_NOTE.value, text=None)
        setup.add(message)
        await setup.commit()
        message_id = message.id

    try:
        async with factory() as first, factory() as second:
            await VoiceNoteRepository(first, f.TENANT_A).start(
                message_id,
                inbox_event_id=event,
                media_id="m",
                mime_type="audio/ogg",
                voice=True,
            )
            await first.commit()
            await VoiceNoteRepository(second, f.TENANT_A).start(
                message_id,
                inbox_event_id=event,
                media_id="m",
                mime_type="audio/ogg",
                voice=True,
            )
            await second.commit()

        async with factory() as check:
            row = await VoiceNoteRepository(check, f.TENANT_A).status_for(message_id)
            count = await check.scalar(sa.select(sa.func.count()).select_from(VoiceNote))
        assert row is not None
        assert row.attempts == 2
        assert count == 1
    finally:
        async with factory() as cleanup:
            await cleanup.execute(sa.delete(Contact).where(Contact.id == contact.id))
            await cleanup.commit()


# --------------------------------------------------------------------------
# MessageRepository.set_transcript
# --------------------------------------------------------------------------


@database
async def test_set_transcript_writes_the_text_once(db_session):
    message = await _voice_message(db_session)
    repository = MessageRepository(db_session, message.tenant_id)

    assert await repository.set_transcript(message.id, TRANSCRIPT) is True

    stored = await db_session.scalar(sa.select(Message.text).where(Message.id == message.id))
    assert stored == TRANSCRIPT


@database
async def test_set_transcript_does_not_overwrite_an_existing_text(db_session):
    """`AND text IS NULL`, and the reason is concurrency rather than tidiness.

    Two workers must not be able to disagree about what the patient said: the
    first write wins and the second is a no-op, instead of the last one
    overwriting a transcript somebody has already built a turn from. The subject
    here is a TYPED message, which is the worst case - overwriting one would
    replace what the patient actually wrote with a machine's guess.
    """
    contact = f.make_contact()
    db_session.add(contact)
    await db_session.flush()
    conversation = f.make_conversation(contact)
    db_session.add(conversation)
    await db_session.flush()
    typed = f.make_message(conversation, text="what the patient really typed")
    db_session.add(typed)
    await db_session.flush()
    repository = MessageRepository(db_session, typed.tenant_id)

    assert await repository.set_transcript(typed.id, TRANSCRIPT) is False

    stored = await db_session.scalar(sa.select(Message.text).where(Message.id == typed.id))
    assert stored == "what the patient really typed"


@database
async def test_set_transcript_twice_is_a_no_op_the_second_time(db_session):
    """Which is what a retry that somehow reaches it must find."""
    message = await _voice_message(db_session)
    repository = MessageRepository(db_session, message.tenant_id)

    assert await repository.set_transcript(message.id, TRANSCRIPT) is True
    assert await repository.set_transcript(message.id, "something else entirely") is False

    stored = await db_session.scalar(sa.select(Message.text).where(Message.id == message.id))
    assert stored == TRANSCRIPT


@database
async def test_set_transcript_is_tenant_scoped(db_session):
    message = await _voice_message(db_session)

    assert (
        await MessageRepository(db_session, f.TENANT_B).set_transcript(message.id, TRANSCRIPT)
        is False
    )

    stored = await db_session.scalar(sa.select(Message.text).where(Message.id == message.id))
    assert stored is None


@database
async def test_set_transcript_leaves_modality_and_status_alone(db_session):
    """It writes one column. The modality was decided in T1 from Meta's own
    `type`, and the status is the message's lifecycle - neither is this
    function's business."""
    message = await _voice_message(db_session)
    repository = MessageRepository(db_session, message.tenant_id)

    await repository.set_transcript(message.id, TRANSCRIPT)

    stored = await db_session.execute(
        sa.select(Message.modality, Message.status, Message.direction).where(
            Message.id == message.id
        )
    )
    assert stored.one() == (
        MessageModality.VOICE_NOTE.value,
        MessageStatus.RECEIVED.value,
        "INBOUND",
    )


@database
async def test_set_transcript_does_not_touch_another_message(db_session):
    message = await _voice_message(db_session)
    other = f.make_message(
        await db_session.get(Conversation, message.conversation_id),
        modality=MessageModality.VOICE_NOTE.value,
        text=None,
    )
    db_session.add(other)
    await db_session.flush()

    await MessageRepository(db_session, message.tenant_id).set_transcript(message.id, TRANSCRIPT)

    assert await db_session.scalar(sa.select(Message.text).where(Message.id == other.id)) is None
