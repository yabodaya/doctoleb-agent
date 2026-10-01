"""Schema assertions. No database: these read Base.metadata."""

import sqlalchemy as sa

from app.db.base import Base
from app.db.models import (
    AgentRun,
    BookingAction,
    Contact,
    ContactIdentity,
    Conversation,
    DeadLetterJob,
    Message,
    ToolExecution,
    VoiceNote,
    WebhookInbox,
)

ALL_MODELS = [
    WebhookInbox,
    Contact,
    ContactIdentity,
    Conversation,
    Message,
    DeadLetterJob,
    AgentRun,
    ToolExecution,
    BookingAction,
    VoiceNote,
]


def _unique_column_sets(table: sa.Table) -> set[frozenset[str]]:
    sets = {
        frozenset(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    sets |= {frozenset(c.name for c in index.columns) for index in table.indexes if index.unique}
    sets |= {frozenset([column.name]) for column in table.columns if column.unique}
    return sets


def test_the_slice_creates_exactly_these_tables():
    # Clinic, doctor, service, schedule and appointment tables belong to the
    # Booking Service (docs/architecture.md). If one appears here, the slice
    # has leaked out of scope.
    assert set(Base.metadata.tables) == {
        "webhook_inbox",
        "contacts",
        "contact_identities",
        "conversations",
        "messages",
        "dead_letter_jobs",
        # VS-006. Both hold codes, counts and ids only - never a clinic's
        # doctors, services or slots, which belong to the Booking Service.
        "agent_runs",
        "tool_executions",
        # VS-007. Ids, codes and one operational expiry - the conversation's
        # prepared change. Still no appointment data: the appointment itself
        # belongs to the Booking Service, and only its id is referenced here.
        "booking_actions",
        # VS-008. Ids, codes and counts - one voice note's bookkeeping. NOT the
        # transcript, which is the patient's message and lives in
        # messages.text, and NOT the audio, which is not stored anywhere at all.
        "voice_notes",
    }


def test_provider_event_id_is_unique():
    # Hard rule 2: the same Meta event delivered twice produces one stored
    # message and one reply. This constraint is what makes that true under
    # concurrency; an application-level "select then insert" does not.
    assert frozenset(["provider_event_id"]) in _unique_column_sets(WebhookInbox.__table__)


def test_provider_message_id_is_unique_and_nullable():
    column = Message.__table__.c.provider_message_id
    assert frozenset(["provider_message_id"]) in _unique_column_sets(Message.__table__)
    # Nullable: an outbound message that failed before Meta accepted it has no
    # provider id. PostgreSQL allows many NULLs under a unique constraint.
    assert column.nullable is True


def test_a_contact_identity_is_unique_per_tenant_channel_and_external_id():
    assert frozenset(["tenant_id", "channel", "external_id"]) in _unique_column_sets(
        ContactIdentity.__table__
    )


def test_only_one_open_conversation_exists_per_contact_and_channel():
    # Review Focus 3. Two concurrent jobs for the same patient must not each
    # create a conversation. Partial, so a closed conversation does not block
    # the next one.
    index = next(i for i in Conversation.__table__.indexes if i.name == "uq_conversations_open")
    assert index.unique is True
    assert [c.name for c in index.columns] == ["tenant_id", "contact_id", "channel"]
    assert index.dialect_options["postgresql"]["where"] is not None


def test_tenant_id_is_not_null_everywhere_a_tenant_is_knowable():
    # Review Focus 7. The webhook stores the raw event before anything resolves
    # a tenant (hard rule 1), and a dead-lettered job may have died before
    # resolution, so those two are nullable. Everything else is not.
    for model in (
        Contact,
        ContactIdentity,
        Conversation,
        Message,
        AgentRun,
        ToolExecution,
        BookingAction,
        VoiceNote,
    ):
        assert model.__table__.c.tenant_id.nullable is False, model.__tablename__
    assert WebhookInbox.__table__.c.tenant_id.nullable is True
    assert DeadLetterJob.__table__.c.tenant_id.nullable is True


def test_every_table_has_timezone_aware_timestamps():
    for model in ALL_MODELS:
        for name in ("created_at", "updated_at"):
            column = model.__table__.c[name]
            assert column.type.timezone is True, f"{model.__tablename__}.{name}"


def test_enum_backed_columns_carry_a_named_check_constraint():
    # Declared here, but NOT protected by the drift test: autogenerate never
    # compares CHECK constraints. tests/db/test_constraints.py proves at runtime
    # that they actually reject bad values.
    expected = {
        "conversations": "ck_conversations_state_valid",
        "messages": "ck_messages_direction_valid",
        "webhook_inbox": "ck_webhook_inbox_status_valid",
        "booking_actions": "ck_booking_actions_kind_valid",
        "voice_notes": "ck_voice_notes_status_valid",
    }
    for table_name, constraint_name in expected.items():
        table = Base.metadata.tables[table_name]
        names = {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
        assert constraint_name in names, f"{table_name}: {names}"


def test_messages_are_indexed_for_conversation_history():
    # VS-005 reads "the last N messages" on every turn. Without this index that
    # is a sequential scan of every message the clinic has ever exchanged.
    index_columns = [[c.name for c in index.columns] for index in Message.__table__.indexes]
    assert ["tenant_id", "conversation_id", "created_at"] in index_columns


def test_deleting_a_conversation_deletes_its_messages():
    fk = next(iter(Message.__table__.c.conversation_id.foreign_keys))
    assert fk.column.table.name == "conversations"
    assert fk.ondelete == "CASCADE"


def test_no_model_repr_leaks_content():
    # Hard rule 8, per model rather than on the shared base, because a future
    # model could override __repr__ without anyone noticing.
    samples = {
        Contact: {"display_name": "Reem Haddad"},
        ContactIdentity: {"external_id": "96170123456"},
        Message: {"text": "my knee hurts"},
        DeadLetterJob: {"error": "my knee hurts"},
    }
    for model, kwargs in samples.items():
        rendered = repr(model(**kwargs))
        for value in kwargs.values():
            assert value not in rendered, f"{model.__name__} leaked {value!r}"


def test_the_reply_link_points_at_a_message():
    """The self-FK, asserted in metadata so it cannot quietly become a loose UUID.

    A plain UUID column would let a reply point at a message that no longer
    exists, and VS-004's "has this already been answered?" check would then
    silently answer no.
    """
    column = Message.__table__.c.reply_to_message_id
    assert column.nullable is True
    targets = {fk.column for fk in column.foreign_keys}
    assert targets == {Message.__table__.c.id}
    assert all(fk.ondelete == "CASCADE" for fk in column.foreign_keys)


def test_status_rank_covers_every_message_status():
    """A new MessageStatus without a rank would silently rank 0.

    Rank 0 means "anything can overwrite it", so a forgotten entry here turns
    the forward-only guarantee off for that status instead of failing loudly.
    """
    from app.db.enums import STATUS_RANK, MessageStatus

    assert set(STATUS_RANK) == set(MessageStatus)
    # And the ordering VS-004 requirement 5 depends on.
    assert STATUS_RANK[MessageStatus.SENT] < STATUS_RANK[MessageStatus.DELIVERED]
    assert STATUS_RANK[MessageStatus.DELIVERED] < STATUS_RANK[MessageStatus.READ]
    # FAILED overwrites SENT but never DELIVERED: a delivered message did not fail.
    assert STATUS_RANK[MessageStatus.SENT] < STATUS_RANK[MessageStatus.FAILED]
    assert STATUS_RANK[MessageStatus.FAILED] < STATUS_RANK[MessageStatus.DELIVERED]


def test_the_agent_tables_have_exactly_these_columns():
    """Hard rule 8, pinned as a column set rather than as a rule.

    `agent_runs` and `tool_executions` exist to answer "what did this cost?"
    and "why did that turn fail?" without becoming a second copy of the
    conversation. Nothing here may hold content: no patient text, no model text,
    no tool arguments, no tool results, no doctor names, no slot times, no
    OpenAI tool-call ids, no unknown tool name and no undeclared argument name.

    Asserted as an exact SET, so adding a `result` or an `arguments` column
    means consciously editing this test. That deliberate edit is the whole
    control: a reviewer sees the rule being changed, not a column being added.
    """
    assert set(AgentRun.__table__.c.keys()) == {
        "id",
        "created_at",
        "updated_at",
        "tenant_id",
        "inbox_event_id",
        "conversation_id",
        "inbound_message_id",
        "reply_message_id",
        "job_try",
        "model",
        "prompt_version",
        "outcome",
        "reason",
        "model_calls",
        "prompt_tokens",
        "completion_tokens",
        "duration_ms",
    }
    assert set(ToolExecution.__table__.c.keys()) == {
        "id",
        "created_at",
        "updated_at",
        "agent_run_id",
        "tenant_id",
        "sequence",
        "model_call",
        "tool_name",
        "argument_names",
        "status",
        "error_code",
        "duration_ms",
    }


def test_the_agent_tables_have_no_free_text_column():
    """A second lock on the same rule, from the other direction.

    The set above says which columns exist; this says what they may be. Every
    column is an id, an integer, a timestamp, a short VARCHAR code, or the
    TEXT[] of declared argument names. A `sa.Text` column - the shape that could
    hold a message - exists only for `tenant_id`, which is a clinic identifier.
    """
    # sa.Text IS a subclass of sa.String, so "unbounded" is `length is None`,
    # not the class.
    for model in (AgentRun, ToolExecution):
        for column in model.__table__.columns:
            if not isinstance(column.type, sa.String):
                continue
            where = f"{model.__tablename__}.{column.name}"
            if column.type.length is None:
                assert column.name == "tenant_id", f"{where} is unbounded TEXT"
            else:
                assert column.type.length <= 100, where


def test_a_tool_execution_belongs_to_a_run_and_dies_with_it():
    """The FK is ON DELETE CASCADE, so no tool row can outlive its run.

    Without the cascade, deleting a run under a retention policy would leave
    orphan rows that no query scopes and nothing prunes.
    """
    (foreign_key,) = list(ToolExecution.__table__.c.agent_run_id.foreign_keys)

    assert foreign_key.column is AgentRun.__table__.c.id
    assert foreign_key.ondelete == "CASCADE"
    assert frozenset(["agent_run_id", "sequence"]) in _unique_column_sets(ToolExecution.__table__)


def test_the_declared_argument_names_column_has_no_server_default():
    """An empty list must be WRITTEN, never defaulted into existence.

    A server default would make "this tool call had no arguments" and "nobody
    recorded the arguments" the same row.
    """
    column = ToolExecution.__table__.c.argument_names

    assert column.server_default is None
    assert column.nullable is False
