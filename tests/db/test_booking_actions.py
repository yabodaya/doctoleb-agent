"""`booking_actions`: the model, and the repository that reads the gate (VS-007).

Two halves. The first needs no database and reads `Base.metadata`: the column set,
the absence of any free-text column, the partial unique index, and the two
vocabularies. The second needs PostgreSQL, because the confirmation gate is computed
in SQL and a Python re-implementation of it here would prove nothing.

The gate (plan V3) is what makes hard rule 5 structural: the model can ask to book
whenever it likes, and the answer to "may this change be executed now?" comes from
the database, not from the model's confidence.
"""

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa

from app.db.base import Base
from app.db.enums import (
    BookingActionKind,
    BookingActionStatus,
    MessageStatus,
    ToolExecutionStatus,
)
from app.db.models import BookingAction
from app.db.repositories.booking_actions import (
    EXECUTED,
    FAILED,
    PROPOSED,
    SUCCESS,
    UNCERTAIN,
    BookingActionRepository,
    BookingOutcomeRow,
)
from app.db.repositories.errors import BookingStateNotRecordedError
from tests.db import factories as f

# The injected clock's "now" for every hold-expiry assertion. 2026, deliberately
# far from PostgreSQL's real now(), so a test that accidentally compared the two
# would fail loudly rather than pass until the calendar moved (plan risk R5).
CLOCK_NOW = dt.datetime(2026, 9, 29, 7, tzinfo=dt.UTC)

# Four moments in PostgreSQL's own time, for the gate. They are ORDERED and set by
# hand, and they are never compared with CLOCK_NOW: the gate reads message
# timestamps (PostgreSQL's clock) and hold expiry reads the injected clock, and
# mixing the two is exactly what plan risk R5 warns about.
DB_T0 = dt.datetime(2025, 3, 1, 10, 0, tzinfo=dt.UTC)  # the patient's first message
DB_T1 = dt.datetime(2025, 3, 1, 10, 1, tzinfo=dt.UTC)  # the hold is prepared
DB_T2 = dt.datetime(2025, 3, 1, 10, 2, tzinfo=dt.UTC)  # our reply is SENT
DB_T3 = dt.datetime(2025, 3, 1, 10, 3, tzinfo=dt.UTC)  # the patient answers


# --- the model: no database --------------------------------------------------


def test_the_booking_actions_table_has_exactly_these_columns():
    """Pinned, so adding a column that could hold content means editing this test.

    Nothing here may be a name, an appointment or slot time, a doctor, a tool
    result, a patient reference or an argument value (hard rule 8). `hold_expires_at`
    is the only timestamp beyond the mixins', and it is an operational deadline from
    the Booking Service.
    """
    assert set(BookingAction.__table__.c.keys()) == {
        "id",
        "created_at",
        "updated_at",
        "tenant_id",
        "conversation_id",
        "kind",
        "status",
        "hold_id",
        "hold_expires_at",
        "appointment_id",
        "created_by_inbox_event_id",
        "created_by_inbound_message_id",
        "decided_by_inbound_message_id",
        "last_idempotency_key",
        "error_code",
    }


def test_the_booking_actions_table_has_no_free_text_column():
    """Every string column is bounded except `tenant_id`.

    An unbounded TEXT column is where content ends up. A `VARCHAR(64)` for an error
    code or a `VARCHAR(128)` for a service id says out loud that nothing longer
    belongs there, and PostgreSQL enforces it.
    """
    for column in BookingAction.__table__.c:
        if not isinstance(column.type, sa.String):
            continue
        if column.name == "tenant_id":
            # Decision D1: an opaque string of a format the Booking Service owner
            # has not settled, so it cannot be given a length.
            assert isinstance(column.type, sa.Text)
            continue
        assert column.type.length is not None, column.name
        assert column.type.length <= 128, column.name


def test_one_pending_action_per_conversation_is_declared():
    """The partial unique index, declared exactly like `uq_conversations_open` -
    the pattern the drift test already accepts."""
    index = next(
        each
        for each in BookingAction.__table__.indexes
        if each.name == "uq_booking_actions_one_pending"
    )

    assert index.unique is True
    assert [column.name for column in index.columns] == ["conversation_id"]
    assert "status = 'PENDING'" in str(index.dialect_options["postgresql"]["where"])


def test_booking_actions_are_indexed_for_the_gates_lookup():
    """`state_for` reads the newest row of one conversation on every turn."""
    index_columns = [[c.name for c in index.columns] for index in BookingAction.__table__.indexes]
    assert ["tenant_id", "conversation_id", "created_at"] in index_columns


def test_deleting_a_conversation_deletes_its_booking_actions_by_declaration():
    """Operational state dies with its conversation. Unlike `agent_runs`, which has
    no FK at all so cost records outlive message retention."""
    fk = next(iter(BookingAction.__table__.c.conversation_id.foreign_keys))

    assert fk.column.table.name == "conversations"
    assert fk.ondelete == "CASCADE"


def test_booking_action_vocabularies_are_pinned():
    """These strings are the literal contents of rows and of two CHECK constraints.
    Renaming one is a data migration, not a refactor."""
    assert [member.value for member in BookingActionKind] == ["BOOK", "RESCHEDULE", "CANCEL"]
    assert [member.value for member in BookingActionStatus] == [
        "PENDING",
        "DONE",
        "FAILED",
        "UNCERTAIN",
        "SUPERSEDED",
        "EXPIRED",
    ]
    assert [member.value for member in ToolExecutionStatus] == [
        "OK",
        "INVALID_ARGUMENTS",
        "UNKNOWN_TOOL",
        "ERROR",
        "SKIPPED",
        "UNCERTAIN",
        "REFUSED",
    ]


def test_both_check_constraints_are_named():
    names = {
        c.name
        for c in Base.metadata.tables["booking_actions"].constraints
        if isinstance(c, sa.CheckConstraint)
    }

    assert names == {"ck_booking_actions_kind_valid", "ck_booking_actions_status_valid"}


def test_the_state_row_repr_shows_no_service_id():
    """`hold_id` never reaches the model, and `BookingStateRow` is the object the
    job turns into what the turn sees."""
    from app.db.repositories.booking_actions import BookingStateRow

    row = BookingStateRow(
        action_id=uuid.uuid4(),
        kind="BOOK",
        status="PENDING",
        confirmable=True,
        hold_id="hold_SENTINEL",
        appointment_id="apt_SENTINEL",
    )

    assert "SENTINEL" not in repr(row)


def test_the_outcome_row_repr_shows_no_ids_and_no_key():
    """This object is built on the path that also writes dead letters and log
    lines, so its repr carries codes only (hard rule 8)."""
    row = BookingOutcomeRow(
        kind="BOOK",
        phase=EXECUTED,
        status=SUCCESS,
        hold_id="hold_SENTINEL",
        appointment_id="apt_SENTINEL",
        idempotency_key="keySENTINEL",
    )

    assert "SENTINEL" not in repr(row)
    assert "BOOK" in repr(row)


def test_booking_state_not_recorded_carries_the_class_name_only():
    """The same shape as `RunNotRecordedError`: a class name, nothing chained."""
    error = BookingStateNotRecordedError("IntegrityError")

    assert error.error_class == "IntegrityError"
    assert str(error) == "booking action not recorded: IntegrityError"
    assert error.__cause__ is None


# --- the repository and the constraints: PostgreSQL --------------------------

database = pytest.mark.db


async def _conversation_with_message(session):
    contact = f.make_contact()
    session.add(contact)
    await session.flush()
    conversation = f.make_conversation(contact)
    session.add(conversation)
    await session.flush()
    inbound = f.make_message(conversation)
    session.add(inbound)
    await session.flush()
    return conversation, inbound


async def _sent_reply(session, conversation, inbound, sent_at: dt.datetime):
    """An outbound reply that Meta accepted, with `sent_at` set by hand.

    `sent_at` is what the gate's third condition reads, and `attach_provider_id` and
    `mark_sent_without_id` are the only two repository methods that set it. Setting
    it explicitly here is what lets a test place a reply before, between or after a
    booking action in PostgreSQL's own time.
    """
    reply = f.make_reply(conversation, inbound, status=MessageStatus.SENT.value, sent_at=sent_at)
    session.add(reply)
    await session.flush()
    return reply


@database
async def test_an_unknown_booking_action_kind_or_status_is_rejected(db_session):
    """Alembic never compares CHECK constraints, so this is the only thing between a
    typo'd vocabulary and rows the app cannot read back."""
    conversation, inbound = await _conversation_with_message(db_session)

    for column in ("kind", "status"):
        values = {
            "id": uuid.uuid4(),
            "tenant_id": conversation.tenant_id,
            "conversation_id": conversation.id,
            "kind": BookingActionKind.BOOK.value,
            "status": BookingActionStatus.PENDING.value,
            "created_by_inbox_event_id": uuid.uuid4(),
            "created_by_inbound_message_id": inbound.id,
            column: "BANANA",
        }
        # Inserted through Core so the ORM does not coerce the value first: the
        # CHECK constraint is the thing under test, not Python.
        with pytest.raises(sa.exc.IntegrityError):
            async with db_session.begin_nested():
                await db_session.execute(sa.insert(BookingAction).values(**values))


@database
async def test_uncertain_and_refused_tool_statuses_are_accepted(db_session):
    """The widened CHECK, at runtime. `UNCERTAIN` is the status a booking-changing
    call gets when its outcome is unknown (V6); `REFUSED` is one our own code
    declined to run. Before migration b919820bf52e both were rejected."""
    conversation, inbound = await _conversation_with_message(db_session)
    run = f.make_agent_run(conversation, inbound)
    db_session.add(run)
    await db_session.flush()

    for index, status in enumerate(("UNCERTAIN", "REFUSED")):
        db_session.add(f.make_tool_execution(run, sequence=index, status=status))
        await db_session.flush()

    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            db_session.add(f.make_tool_execution(run, sequence=9, status="BANANA"))
            await db_session.flush()


@database
async def test_two_pending_actions_in_one_conversation_are_rejected(db_session):
    """V2's partial unique index. "The prepared change" is one thing, and two quick
    messages must not each get one (plan risk R1)."""
    conversation, inbound = await _conversation_with_message(db_session)
    db_session.add(f.make_booking_action(conversation, inbound))
    await db_session.flush()

    with pytest.raises(sa.exc.IntegrityError):
        async with db_session.begin_nested():
            db_session.add(f.make_booking_action(conversation, inbound, hold_id="hold_2"))
            await db_session.flush()


@database
async def test_a_done_and_a_pending_action_can_coexist(db_session):
    """Partial, so a conversation that has already booked once can prepare again."""
    conversation, inbound = await _conversation_with_message(db_session)
    db_session.add(
        f.make_booking_action(conversation, inbound, status=BookingActionStatus.DONE.value)
    )
    await db_session.flush()
    db_session.add(f.make_booking_action(conversation, inbound, hold_id="hold_2"))
    await db_session.flush()

    count = await db_session.scalar(sa.select(sa.func.count()).select_from(BookingAction))
    assert count == 2


@database
async def test_deleting_a_conversation_deletes_its_booking_actions(db_session):
    conversation, inbound = await _conversation_with_message(db_session)
    db_session.add(f.make_booking_action(conversation, inbound))
    await db_session.flush()

    await db_session.execute(
        sa.delete(type(conversation)).where(type(conversation).id == conversation.id)
    )
    await db_session.flush()

    count = await db_session.scalar(sa.select(sa.func.count()).select_from(BookingAction))
    assert count == 0


@database
async def test_expire_pending_uses_the_clock_it_is_given_not_the_database_clock(db_session):
    """Plan risk R5, pinned.

    The hold's expiry came from the Booking Service's clock, so it is compared with
    the clock the rest of the turn uses. Here that clock sits in 2026 while
    PostgreSQL's now() is today: a row whose expiry is in 2026 is NOT lapsed on the
    injected clock, and would be lapsed on any real clock before 2026 - or the
    reverse, depending on the day. Reading now() here would make this test's result
    depend on the calendar.
    """
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)
    db_session.add(
        f.make_booking_action(
            conversation, inbound, hold_expires_at=CLOCK_NOW + dt.timedelta(minutes=5)
        )
    )
    await db_session.flush()

    assert await repository.expire_pending(conversation.id, now=CLOCK_NOW) == 0
    assert (
        await repository.expire_pending(conversation.id, now=CLOCK_NOW + dt.timedelta(minutes=6))
        == 1
    )

    status = await db_session.scalar(sa.select(BookingAction.status))
    assert status == BookingActionStatus.EXPIRED.value


@database
async def test_expire_pending_leaves_a_prepared_cancellation_alone(db_session):
    """A prepared cancellation has no hold and nothing that lapses."""
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)
    db_session.add(
        f.make_booking_action(
            conversation,
            inbound,
            kind=BookingActionKind.CANCEL.value,
            hold_id=None,
            appointment_id="apt_1",
        )
    )
    await db_session.flush()

    assert await repository.expire_pending(conversation.id, now=CLOCK_NOW) == 0
    assert await db_session.scalar(sa.select(BookingAction.status)) == "PENDING"


@database
async def test_supersede_pending_voids_every_pending_row(db_session):
    """Both hard-rule-7 drop paths call this (plan section 5.12): a staff message
    sent in between would otherwise satisfy the gate, and the patient's next "yes"
    would confirm something a human never saw."""
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)
    db_session.add(f.make_booking_action(conversation, inbound))
    await db_session.flush()

    assert await repository.supersede_pending(conversation.id) == 1
    assert await db_session.scalar(sa.select(BookingAction.status)) == "SUPERSEDED"
    assert await repository.supersede_pending(conversation.id) == 0


@database
@pytest.mark.parametrize(
    "case",
    [
        "the same message",
        "a later message with no reply sent",
        "a reply sent before the action",
        "a reply sent in between",
        "a reply that failed",
        "an expired row",
        "another tenant's row",
    ],
)
async def test_state_for_is_confirmable_only_after_a_reply_was_sent_in_between(db_session, case):
    """The confirmation gate, case by case (plan V3).

    Only one of these is confirmable: the case where a reply of ours actually went
    out after the change was prepared and before the patient's next message was
    stored. Everything else is a way the patient could not have been told what they
    are supposedly confirming.
    """
    # Every timestamp is set BY HAND. Everything in one test shares one
    # transaction, and PostgreSQL's now() is constant inside a transaction, so the
    # server defaults would make the action, the reply and the second message
    # simultaneous - and the gate's strict "sent_at < N.created_at" would then be
    # false in every case, including the one that must be true. The conftest says
    # the same thing about any assertion that a timestamp moved.
    conversation, first = await _conversation_with_message(db_session)
    await db_session.execute(
        sa.update(type(first)).where(type(first).id == first.id).values(created_at=DB_T0)
    )

    action = f.make_booking_action(conversation, first, created_at=DB_T1)
    if case == "an expired row":
        action.status = BookingActionStatus.EXPIRED.value
    db_session.add(action)
    await db_session.flush()

    if case == "a reply sent before the action":
        await _sent_reply(db_session, conversation, first, DB_T0)
    elif case == "a reply that failed":
        # A FAILED reply carries no sent_at at all: mark_failed does not set it.
        # So nothing described the hold to the patient, and nothing may confirm it.
        db_session.add(f.make_reply(conversation, first, status=MessageStatus.FAILED.value))
        await db_session.flush()
    elif case not in ("a later message with no reply sent", "the same message"):
        await _sent_reply(db_session, conversation, first, DB_T2)

    second = f.make_message(conversation, created_at=DB_T3)
    db_session.add(second)
    await db_session.flush()

    answering = first if case == "the same message" else second
    tenant = "someone-else" if case == "another tenant's row" else conversation.tenant_id
    state = await BookingActionRepository(db_session, tenant).state_for(
        conversation.id, answering.id
    )

    if case == "another tenant's row":
        assert state is None
        return
    assert state is not None
    assert state.confirmable is (case == "a reply sent in between"), case
    assert state.action_id == action.id
    assert state.hold_id == "hold_1"


@database
async def test_state_for_returns_nothing_when_the_conversation_has_no_action(db_session):
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)

    assert await repository.state_for(conversation.id, inbound.id) is None


@database
async def test_state_for_returns_the_newest_row(db_session):
    """Only the latest action matters: an older one has been superseded, and the
    tools ask about "the prepared change", singular."""
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)
    older = f.make_booking_action(
        conversation,
        inbound,
        status=BookingActionStatus.SUPERSEDED.value,
        created_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
    )
    db_session.add(older)
    await db_session.flush()
    db_session.add(f.make_booking_action(conversation, inbound, hold_id="hold_2"))
    await db_session.flush()

    state = await repository.state_for(conversation.id, inbound.id)

    assert state is not None
    assert state.hold_id == "hold_2"


@database
@pytest.mark.parametrize(
    ("phase", "status", "confirmable", "expected_status", "expected_rows"),
    [
        (PROPOSED, SUCCESS, True, BookingActionStatus.PENDING.value, 1),
        (PROPOSED, SUCCESS, False, BookingActionStatus.SUPERSEDED.value, 1),
        (PROPOSED, UNCERTAIN, True, BookingActionStatus.UNCERTAIN.value, 1),
        (PROPOSED, FAILED, True, None, 0),
        (EXECUTED, SUCCESS, True, BookingActionStatus.DONE.value, 1),
        (EXECUTED, FAILED, True, BookingActionStatus.FAILED.value, 1),
        (EXECUTED, UNCERTAIN, True, BookingActionStatus.UNCERTAIN.value, 1),
    ],
    ids=[
        "proposed success, confirmable",
        "proposed success, not confirmable",
        "proposed uncertain",
        "proposed failed",
        "executed success",
        "executed failed",
        "executed uncertain",
    ],
)
async def test_apply_follows_the_transition_table(
    db_session, phase, status, confirmable, expected_status, expected_rows
):
    """Plan section 5.4's table, row by row.

    `confirmable=False` means the patient will not see this turn's own wording - the
    reply was dropped, replaced by the guard, or is the fallback - so a change
    prepared in that turn is inserted already SUPERSEDED. Nothing described it, so
    nothing may confirm it later (hard rule 5).
    """
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)
    existing: uuid.UUID | None = None
    if phase == EXECUTED:
        action = f.make_booking_action(conversation, inbound)
        db_session.add(action)
        await db_session.flush()
        existing = action.id

    outcome = BookingOutcomeRow(
        kind=BookingActionKind.BOOK.value,
        phase=phase,
        status=status,
        error_code=None if status == SUCCESS else "hold_expired",
        action_id=existing,
        hold_id="hold_new" if phase == PROPOSED else None,
        hold_expires_at=CLOCK_NOW + dt.timedelta(minutes=10) if phase == PROPOSED else None,
        appointment_id="apt_new" if phase == EXECUTED and status == SUCCESS else None,
        idempotency_key="a" * 64,
    )
    written = await repository.apply(
        outcome,
        conversation_id=conversation.id,
        inbox_event_id=uuid.uuid4(),
        inbound_message_id=inbound.id,
        confirmable=confirmable,
    )

    if expected_status is None:
        # PROPOSED + FAILED: the service changed nothing, so nothing is written.
        assert written is None
        assert await db_session.scalar(sa.select(sa.func.count()).select_from(BookingAction)) == 0
        return

    assert written is not None
    total = await db_session.scalar(sa.select(sa.func.count()).select_from(BookingAction))
    assert total == expected_rows
    row = await db_session.scalar(sa.select(BookingAction).where(BookingAction.id == written))
    assert row.status == expected_status
    if phase == EXECUTED:
        assert row.id == existing
        assert row.decided_by_inbound_message_id == inbound.id
        if status == SUCCESS:
            assert row.appointment_id == "apt_new"
    if status == UNCERTAIN:
        assert row.error_code == "hold_expired"
        if phase == PROPOSED:
            # We do not know whether a hold exists, so the row never names one: a
            # row that did would let the gate offer it for confirmation (plan V6).
            assert row.hold_id is None
    assert row.last_idempotency_key == "a" * 64


@database
async def test_a_replayed_hold_is_not_recorded_twice(db_session):
    """The Booking Service returned the hold this conversation already has (V13).
    A second row would trip the partial unique index for no gain."""
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)
    action = f.make_booking_action(conversation, inbound, hold_id="hold_1")
    db_session.add(action)
    await db_session.flush()

    written = await repository.apply(
        BookingOutcomeRow(
            kind=BookingActionKind.BOOK.value, phase=PROPOSED, status=SUCCESS, hold_id="hold_1"
        ),
        conversation_id=conversation.id,
        inbox_event_id=uuid.uuid4(),
        inbound_message_id=inbound.id,
        confirmable=True,
    )

    assert written == action.id
    count = await db_session.scalar(sa.select(sa.func.count()).select_from(BookingAction))
    assert count == 1


@database
async def test_a_second_prepared_hold_supersedes_the_first(db_session):
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)
    first = f.make_booking_action(conversation, inbound, hold_id="hold_1")
    db_session.add(first)
    await db_session.flush()

    await repository.apply(
        BookingOutcomeRow(
            kind=BookingActionKind.BOOK.value, phase=PROPOSED, status=SUCCESS, hold_id="hold_2"
        ),
        conversation_id=conversation.id,
        inbox_event_id=uuid.uuid4(),
        inbound_message_id=inbound.id,
        confirmable=True,
    )

    statuses = dict(
        (await db_session.execute(sa.select(BookingAction.hold_id, BookingAction.status))).all()
    )
    assert statuses == {"hold_1": "SUPERSEDED", "hold_2": "PENDING"}


@database
async def test_an_executed_outcome_on_an_already_decided_row_changes_nothing(db_session):
    """A concurrent job already decided it. `WHERE status = 'PENDING'` is exactly
    what makes the second decision a no-op rather than an overwrite (plan risk R1)."""
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)
    action = f.make_booking_action(
        conversation, inbound, status=BookingActionStatus.DONE.value, appointment_id="apt_first"
    )
    db_session.add(action)
    await db_session.flush()

    written = await repository.apply(
        BookingOutcomeRow(
            kind=BookingActionKind.BOOK.value,
            phase=EXECUTED,
            status=FAILED,
            action_id=action.id,
            error_code="hold_expired",
        ),
        conversation_id=conversation.id,
        inbox_event_id=uuid.uuid4(),
        inbound_message_id=inbound.id,
        confirmable=True,
    )

    assert written is None
    row = await db_session.scalar(sa.select(BookingAction))
    assert row.status == BookingActionStatus.DONE.value
    assert row.appointment_id == "apt_first"
    assert row.error_code is None


@database
async def test_an_executed_outcome_must_name_its_action(db_session):
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)

    with pytest.raises(ValueError):
        await repository.apply(
            BookingOutcomeRow(kind="BOOK", phase=EXECUTED, status=SUCCESS),
            conversation_id=conversation.id,
            inbox_event_id=uuid.uuid4(),
            inbound_message_id=inbound.id,
            confirmable=True,
        )


@database
async def test_a_failed_apply_rolls_back_only_its_savepoint(db_session):
    """PostgreSQL aborts the WHOLE transaction on any failed statement, and `apply`
    runs in T1b AFTER the reply was reserved. Without the savepoint, bookkeeping
    that failed would cost the patient their reply (plan risk R4).

    Made to fail by naming a conversation that does not exist, so the foreign key
    rejects the insert.
    """
    conversation, inbound = await _conversation_with_message(db_session)
    repository = BookingActionRepository(db_session, conversation.tenant_id)
    reply = f.make_reply(conversation, inbound)
    db_session.add(reply)
    await db_session.flush()

    with pytest.raises(BookingStateNotRecordedError) as raised:
        await repository.apply(
            BookingOutcomeRow(
                kind=BookingActionKind.BOOK.value, phase=PROPOSED, status=SUCCESS, hold_id="hold_1"
            ),
            conversation_id=uuid.uuid4(),  # no such conversation
            inbox_event_id=uuid.uuid4(),
            inbound_message_id=inbound.id,
            confirmable=True,
        )

    assert raised.value.error_class == "IntegrityError"
    assert raised.value.__cause__ is None
    # The transaction is still usable, and the reply is still there.
    await db_session.commit()
    survivor = await db_session.scalar(
        sa.select(sa.func.count()).select_from(type(reply)).where(type(reply).id == reply.id)
    )
    assert survivor == 1


@database
async def test_two_writers_on_one_conversation_serialise_on_the_row_lock(
    db_session, second_session_factory
):
    """U8. T1b and T1r take the conversation row lock FIRST, inside the
    transaction, so two turns for one conversation queue instead of deadlocking.

    Two genuinely independent sessions, each locking the conversation row before it
    writes. The second waits for the first's commit, then succeeds. If the lock were
    taken AFTER the insert, each would hold a row the other needed.
    """
    import asyncio

    # Built through an INDEPENDENT session and really committed. `db_session` runs
    # inside an outer transaction with join_transaction_mode="create_savepoint", so
    # its own commit() only releases a savepoint: rows written through it are
    # invisible to any other connection, and the two writers below would fail on the
    # foreign key rather than on the lock.
    async with second_session_factory() as setup:
        conversation, inbound = await _conversation_with_message(setup)
        await setup.commit()

    async def writer(hold_id: str, delay: float) -> str:
        async with second_session_factory() as session:
            await session.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
            await session.execute(
                sa.select(type(conversation).id)
                .where(type(conversation).id == conversation.id)
                .with_for_update()
            )
            await asyncio.sleep(delay)
            repository = BookingActionRepository(session, conversation.tenant_id)
            await repository.apply(
                BookingOutcomeRow(
                    kind=BookingActionKind.BOOK.value,
                    phase=PROPOSED,
                    status=SUCCESS,
                    hold_id=hold_id,
                ),
                conversation_id=conversation.id,
                inbox_event_id=uuid.uuid4(),
                inbound_message_id=inbound.id,
                confirmable=True,
            )
            await session.commit()
            return hold_id

    try:
        done = await asyncio.gather(writer("hold_a", 0.2), writer("hold_b", 0.0))
        assert sorted(done) == ["hold_a", "hold_b"]

        async with second_session_factory() as session:
            rows = dict(
                (
                    await session.execute(sa.select(BookingAction.hold_id, BookingAction.status))
                ).all()
            )
        # Both wrote; the partial unique index guarantees one PENDING survivor.
        assert len(rows) == 2
        assert sorted(rows.values()) == ["PENDING", "SUPERSEDED"]
    finally:
        # This test commits, so `db_session`'s rollback cannot undo it: it cleans up
        # after itself, as every `second_session_factory` test does. Deleting the
        # contact cascades to the conversation, its messages and its booking
        # actions.
        async with second_session_factory() as session:
            await session.execute(
                sa.text("DELETE FROM contacts WHERE id = :contact_id").bindparams(
                    contact_id=conversation.contact_id
                )
            )
            await session.commit()
