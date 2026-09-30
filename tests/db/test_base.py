"""The foundation every model sits on.

No database: SQLAlchemy builds Table objects at import time, so metadata is
fully inspectable with nothing running.
"""

import datetime as dt
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm.attributes import instance_state

from app.db.base import NAMING_CONVENTION, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import (
    AgentRunOutcome,
    Channel,
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
    ToolExecutionStatus,
    check_constraint,
)


class _SampleBase(DeclarativeBase):
    """A separate registry, so the throwaway model below never lands on
    Base.metadata — tests/db/test_models.py asserts the exact set of tables the
    slice creates, and a leaked `_sample` would fail it whenever both modules
    are imported into the same process."""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    __repr__ = Base.__repr__


TENANT = "clinic-alpha"  # opaque (decision D1), never a UUID


class _Sample(UUIDPrimaryKeyMixin, TimestampMixin, _SampleBase):
    """A throwaway model used only to exercise the mixins."""

    __tablename__ = "_sample"

    # sa.Text, matching every real tenant column since decision D1.
    tenant_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    secret_text: Mapped[str | None] = mapped_column(sa.Text)


def test_enum_values_are_the_exact_strings_stored_in_the_database():
    # These strings end up in CHECK constraints and in rows. Renaming one is a
    # migration, not a refactor, so they are pinned here.
    assert [s.value for s in ConversationState] == [
        "AI_ACTIVE",
        "HUMAN_REQUESTED",
        "HUMAN_ACTIVE",
        "CLOSED",
    ]
    assert [d.value for d in MessageDirection] == ["INBOUND", "OUTBOUND"]
    # OTHER was added in VS-004 (plan note C4): "store all inbound message
    # types" needs a value for an image, a document or a location, and the CHECK
    # was widened by a hand-written migration to match.
    assert [m.value for m in MessageModality] == ["TEXT", "VOICE_NOTE", "OTHER"]
    assert [s.value for s in MessageStatus] == [
        "RECEIVED",
        "QUEUED",
        "SENT",
        "DELIVERED",
        "READ",
        "FAILED",
    ]
    assert [s.value for s in InboxStatus] == [
        "RECEIVED",
        "PROCESSING",
        "PROCESSED",
        "FAILED",
    ]
    assert [c.value for c in Channel] == ["whatsapp"]
    # VS-006.
    assert [o.value for o in AgentRunOutcome] == ["SUCCESS", "RETRYABLE", "PERMANENT"]
    assert [s.value for s in ToolExecutionStatus] == [
        "OK",
        "INVALID_ARGUMENTS",
        "UNKNOWN_TOOL",
        "ERROR",
        # A call the model asked for on the last allowed model response, or past
        # the per-turn cap: recorded, never executed. The model still gets a
        # tool message for it, because OpenAI requires one per tool_call_id.
        "SKIPPED",
        # VS-007. A booking-changing call whose outcome is UNKNOWN - it timed
        # out, lost its connection, or the turn deadline cut it. Deliberately
        # not ERROR: an error means "it did not happen", which is the one thing
        # we cannot say (hard rule 5). The CHECK was widened by the hand-written
        # migration b919820bf52e.
        "UNCERTAIN",
        # VS-007. OUR code declined to run it: the confirmation gate, one change
        # per message, or too little turn budget left.
        "REFUSED",
    ]


def test_agent_run_outcomes_are_the_chat_outcomes():
    """The database's vocabulary and the chat layer's must not drift.

    They are separate enums on purpose - app/db/ must not import the
    integrations layer - which is exactly why they need a test. If ChatOutcome
    ever grows a value, this fails, and widening the CHECK becomes a decision
    with a migration rather than a row PostgreSQL rejects inside a job.
    """
    from app.integrations.openai import ChatOutcome

    assert [o.value for o in AgentRunOutcome] == [o.value for o in ChatOutcome]
    assert {o.name for o in AgentRunOutcome} == {o.name for o in ChatOutcome}


def test_enums_are_plain_strings_so_they_bind_to_varchar_columns():
    assert ConversationState.AI_ACTIVE == "AI_ACTIVE"
    assert f"{MessageDirection.INBOUND}" == "INBOUND"


def test_check_constraint_lists_every_enum_value():
    constraint = check_constraint("state", ConversationState, "state_valid")
    rendered = str(constraint.sqltext)
    for state in ConversationState:
        assert state.value in rendered
    assert constraint.name == "state_valid"


def test_metadata_uses_a_deterministic_naming_convention():
    # Without this, PostgreSQL names CHECK constraints at random and every
    # autogenerate run produces a spurious diff.
    convention = Base.metadata.naming_convention
    assert convention["pk"] == "pk_%(table_name)s"
    assert convention["uq"] == "uq_%(table_name)s_%(column_0_N_name)s"
    assert convention["ck"] == "ck_%(table_name)s_%(constraint_name)s"
    assert convention["fk"] == "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    assert convention["ix"] == "ix_%(table_name)s_%(column_0_N_name)s"


def test_uuid_primary_key_is_assigned_at_construction():
    # mapped_column(default=...) is an INSERT-time default: with only that,
    # row.id is None until the row is flushed. Half this slice builds parent
    # and child rows in one go (a contact and its identity, a conversation and
    # its messages) and reads the parent's id before any flush, so the id has
    # to exist the moment the object does.
    row = _Sample(tenant_id=TENANT)
    assert isinstance(row.id, UUID)
    assert _Sample.__table__.c.id.primary_key is True

    # An explicitly supplied id must survive.
    fixed = uuid4()
    assert _Sample(id=fixed, tenant_id=TENANT).id == fixed

    # Two rows do not share one.
    assert _Sample(tenant_id=TENANT).id != _Sample(tenant_id=TENANT).id


def test_timestamps_are_timezone_aware_with_a_server_default():
    # A naive TIMESTAMP column silently drops the offset. The 24h WhatsApp
    # free-form window is computed from these values; an hour of drift is a
    # message Meta rejects.
    for name in ("created_at", "updated_at"):
        column = _Sample.__table__.c[name]
        assert isinstance(column.type, sa.DateTime)
        assert column.type.timezone is True
        assert column.nullable is False
        assert column.server_default is not None
    assert dt.datetime.now(dt.UTC).tzinfo is not None  # sanity: we write aware values


def test_repr_never_includes_column_content():
    # Hard rule 8. These objects appear in tracebacks and assertion failures.
    row = _Sample(tenant_id=TENANT, secret_text="my knee hurts")
    rendered = repr(row)
    assert "my knee hurts" not in rendered
    assert "_Sample" in rendered
    assert str(row.id) in rendered


def test_repr_on_an_expired_instance_does_not_trigger_attribute_loading():
    # A rollback expires every attribute, even with expire_on_commit=False.
    # InstanceState._expire deletes the values out of the instance __dict__
    # (orm/state.py: `for key in ...intersection(dict_): del dict_[key]`), so a
    # getattr inside __repr__ takes AttributeImpl.get's miss branch and fires
    # the expired loader. Under AsyncSession that raises MissingGreenlet --
    # while an exception is being formatted, which hides the original error.
    # __repr__ must therefore read only already-loaded values.
    row = _Sample(tenant_id=TENANT, secret_text="my knee hurts")
    state = instance_state(row)
    state._expire(row.__dict__, set())
    assert state.expired is True
    assert "id" not in row.__dict__  # the loader is the only way back to it

    rendered = repr(row)  # must not raise, must not emit IO

    assert "_Sample" in rendered
    assert "my knee hurts" not in rendered
    assert state.expired is True  # nothing was loaded to build the repr
