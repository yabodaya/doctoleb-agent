"""Schema assertions. No database: these read Base.metadata."""

import sqlalchemy as sa

from app.db.base import Base
from app.db.models import (
    Contact,
    ContactIdentity,
    Conversation,
    DeadLetterJob,
    Message,
    WebhookInbox,
)

ALL_MODELS = [
    WebhookInbox,
    Contact,
    ContactIdentity,
    Conversation,
    Message,
    DeadLetterJob,
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
    for model in (Contact, ContactIdentity, Conversation, Message):
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
