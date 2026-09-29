"""AgentRunRepository: what a turn cost, written without what it said.

Every test here runs on `db_session`, which since Q11 uses production's
`SESSION_OPTIONS` - autoflush OFF included. That matters for this repository in
particular: it flushes explicitly inside a savepoint, and a method that relied
on an implicit flush would have passed under the old fixture and failed live.
"""

import uuid

import pytest
import sqlalchemy as sa

from app.db.enums import AgentRunOutcome, ToolExecutionStatus
from app.db.models import AgentRun, Message, ToolExecution
from app.db.repositories import (
    AgentRunRepository,
    AgentRunRow,
    RunNotRecordedError,
    ToolExecutionRow,
)
from tests.db import factories as f

pytestmark = pytest.mark.db


async def _conversation(db_session, tenant: str = f.TENANT_A):
    contact = f.make_contact(tenant_id=tenant)
    db_session.add(contact)
    conversation = f.make_conversation(contact)
    db_session.add(conversation)
    inbound = f.make_message(conversation)
    db_session.add(inbound)
    await db_session.flush()
    return conversation, inbound


def _row(conversation, inbound, **overrides) -> AgentRunRow:
    values = {
        "inbox_event_id": uuid.uuid4(),
        "conversation_id": conversation.id,
        "inbound_message_id": inbound.id,
        "job_try": 1,
        "prompt_version": "vs006-1",
        "outcome": AgentRunOutcome.SUCCESS.value,
        "reason": "ok",
        "model_calls": 3,
        "duration_ms": 1234,
        "model": "test-model",
        "prompt_tokens": 33,
        "completion_tokens": 21,
    }
    values.update(overrides)
    return AgentRunRow(**values)


async def test_a_run_and_its_tools_are_recorded_in_order(db_session):
    """The shape Task 9's acceptance test reads back.

    `sequence` is asserted as the ORDER BY key rather than `created_at`,
    because all of these rows are written in one transaction and PostgreSQL's
    now() is constant within a transaction.
    """
    conversation, inbound = await _conversation(db_session)
    tools = (
        ToolExecutionRow(0, 1, "list_doctors", (), ToolExecutionStatus.OK.value, None, 4),
        ToolExecutionRow(
            1,
            2,
            "search_available_slots",
            ("doctor_id", "end", "start"),
            ToolExecutionStatus.OK.value,
            None,
            9,
        ),
    )

    run_id = await AgentRunRepository(db_session, f.TENANT_A).add(
        _row(conversation, inbound, tool_executions=tools)
    )

    run = (await db_session.execute(sa.select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
    assert run.outcome == "SUCCESS"
    assert run.model_calls == 3
    assert (run.prompt_tokens, run.completion_tokens) == (33, 21)
    assert run.reply_message_id is None

    rows = (
        (
            await db_session.execute(
                sa.select(ToolExecution)
                .where(ToolExecution.agent_run_id == run_id)
                .order_by(ToolExecution.sequence)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.sequence, r.tool_name, r.argument_names, r.status) for r in rows] == [
        (0, "list_doctors", [], "OK"),
        (1, "search_available_slots", ["doctor_id", "end", "start"], "OK"),
    ]
    assert [r.model_call for r in rows] == [1, 2]


async def test_a_run_with_no_tool_calls_records_no_tool_rows(db_session):
    """A plain text turn is the common case and must not need a special path."""
    conversation, inbound = await _conversation(db_session)

    run_id = await AgentRunRepository(db_session, f.TENANT_A).add(_row(conversation, inbound))

    count = await db_session.execute(
        sa.select(sa.func.count())
        .select_from(ToolExecution)
        .where(ToolExecution.agent_run_id == run_id)
    )
    assert count.scalar_one() == 0


async def test_the_repository_writes_the_tenant_it_was_built_with(db_session):
    """Hard rule 4. The tenant comes from the constructor, never from the row.

    `AgentRunRow` deliberately has no `tenant_id` field, so there is nothing for
    a caller to pass and nothing a tool could be shaped to supply. The tool rows
    carry the same tenant, so a per-tenant query never has to trust a join.
    """
    conversation, inbound = await _conversation(db_session, tenant=f.TENANT_B)
    tools = (ToolExecutionRow(0, 1, "list_doctors", (), "OK", None, 1),)

    run_id = await AgentRunRepository(db_session, f.TENANT_B).add(
        _row(conversation, inbound, tool_executions=tools)
    )

    run = (await db_session.execute(sa.select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
    tool = (
        await db_session.execute(
            sa.select(ToolExecution).where(ToolExecution.agent_run_id == run_id)
        )
    ).scalar_one()
    assert run.tenant_id == f.TENANT_B
    assert tool.tenant_id == f.TENANT_B
    assert not hasattr(AgentRunRow, "tenant_id")


async def test_a_failed_run_insert_rolls_back_only_its_own_savepoint(db_session):
    """The reason the insert is wrapped in `begin_nested()`.

    PostgreSQL aborts the WHOLE transaction on any failed statement. In T1b this
    call happens AFTER the reply has been reserved, so without the savepoint a
    bookkeeping failure would poison the transaction and cost the patient their
    reply. Here a message stands in for the reservation: it must survive.
    """
    conversation, inbound = await _conversation(db_session)
    before = (
        await db_session.execute(
            sa.select(sa.func.count()).select_from(Message).where(Message.id == inbound.id)
        )
    ).scalar_one()
    assert before == 1

    with pytest.raises(RunNotRecordedError):
        await AgentRunRepository(db_session, f.TENANT_A).add(
            _row(conversation, inbound, outcome="BOGUS")  # trips the CHECK
        )

    # The transaction is still usable: this query would raise
    # InFailedSqlTransaction without the savepoint.
    assert (
        await db_session.execute(
            sa.select(sa.func.count()).select_from(Message).where(Message.id == inbound.id)
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(sa.select(sa.func.count()).select_from(AgentRun))
    ).scalar_one() == 0


async def test_a_failed_tool_insert_takes_the_run_with_it(db_session):
    """All or nothing: one savepoint covers the run AND its tool rows.

    A run recorded without its tool rows would read as "this turn called no
    tools", which is a wrong answer rather than a missing one.
    """
    conversation, inbound = await _conversation(db_session)
    tools = (
        ToolExecutionRow(0, 1, "list_doctors", (), "OK", None, 1),
        ToolExecutionRow(1, 1, "list_doctors", (), "BOGUS", None, 1),
    )

    with pytest.raises(RunNotRecordedError):
        await AgentRunRepository(db_session, f.TENANT_A).add(
            _row(conversation, inbound, tool_executions=tools)
        )

    assert (
        await db_session.execute(sa.select(sa.func.count()).select_from(AgentRun))
    ).scalar_one() == 0
    assert (
        await db_session.execute(sa.select(sa.func.count()).select_from(ToolExecution))
    ).scalar_one() == 0


async def test_run_not_recorded_carries_the_exception_class_only(db_session):
    """Hard rule 8. The caller logs this, and a statement is the wrong thing to
    log from a table built to hold no content.

    Raised `from None` on purpose: a chained traceback would print the statement
    even though the engine hides its parameters.
    """
    conversation, inbound = await _conversation(db_session)

    with pytest.raises(RunNotRecordedError) as raised:
        await AgentRunRepository(db_session, f.TENANT_A).add(
            _row(conversation, inbound, outcome="SENTINEL-OUTCOME")
        )

    assert raised.value.error_class == "IntegrityError"
    assert str(raised.value) == "agent run not recorded: IntegrityError"
    assert "SENTINEL" not in str(raised.value)
    assert "agent_runs" not in str(raised.value)
    # `from None` sets __cause__ to None and __suppress_context__ to True.
    # __context__ still holds the original - Python always sets it - but
    # __suppress_context__ is what stops the traceback printing it, which is the
    # guarantee that matters here.
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


async def test_a_long_reason_is_truncated_to_its_column(db_session):
    """A reason is a CODE, and codes are short. Truncating here rather than at
    the call site means a code that outgrows the column cannot turn one recorded
    failure into a second, unrecorded one."""
    conversation, inbound = await _conversation(db_session)

    run_id = await AgentRunRepository(db_session, f.TENANT_A).add(
        _row(conversation, inbound, reason="x" * 500, model="m" * 400)
    )

    run = (await db_session.execute(sa.select(AgentRun).where(AgentRun.id == run_id))).scalar_one()
    assert len(run.reason) == 100
    assert len(run.model) == 100


async def test_the_repository_is_tenant_scoped_like_every_other(db_session):
    """Hard rule 4 made structural: there is no call site that can forget it."""
    for missing in (None, "", uuid.uuid4()):
        with pytest.raises(ValueError, match="tenant_id"):
            AgentRunRepository(db_session, missing)
