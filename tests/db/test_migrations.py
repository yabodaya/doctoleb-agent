"""Acceptance: `alembic upgrade head` on an empty DB works, and downgrade works."""

import asyncio
import uuid
from urllib.parse import urlparse, urlunparse

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
from tests.db.conftest import _ensure_database, alembic_config

pytestmark = pytest.mark.db

LIFECYCLE_DATABASE_NAME = "doctoleb_test_migrations"

# Explicit revision ids, never "-1" or "head": these tests step across a
# specific type change, and a later slice adding a newer head must not silently
# move what they are testing (plan Task 1, Step 1).
VS004_REVISION = "22a816a5a08d"
TENANT_TEXT_REVISION = "4f3c8d21a90e"

EXPECTED_TABLES = {
    "webhook_inbox",
    "contacts",
    "contact_identities",
    "conversations",
    "messages",
    "dead_letter_jobs",
    "alembic_version",
}


@pytest.fixture
def lifecycle_url(test_database_url: str) -> str:
    """A throwaway database, so upgrading and downgrading here cannot disturb
    the shared doctoleb_test that the other database tests rely on."""
    parsed = urlparse(test_database_url)
    url = urlunparse(parsed._replace(path=f"/{LIFECYCLE_DATABASE_NAME}"))
    asyncio.run(_ensure_database(url))
    command.downgrade(alembic_config(url), "base")
    return url


def _table_names(url: str) -> set[str]:
    async def _read() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                return set(
                    await connection.run_sync(lambda sync: sa.inspect(sync).get_table_names())
                )
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def _execute(url: str, *statements: str) -> None:
    """Run raw SQL against the lifecycle database, each statement committed.

    Raw SQL rather than the ORM on purpose: these tests insert rows that exist
    at an OLD revision, where the models no longer describe the columns.
    """

    async def _run() -> None:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                for statement in statements:
                    await connection.execute(sa.text(statement))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _rows(url: str, statement: str) -> list[tuple]:
    async def _run() -> list[tuple]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                return list((await connection.execute(sa.text(statement))).all())
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _seed_uuid_tenant_rows(url: str, tenant: uuid.UUID) -> None:
    """One row in each of the six tenant-bearing tables, at VS004_REVISION.

    Synthetic throughout (hard rule 8): the ids are generated and the only text
    is the word "seed".
    """
    contact, identity = uuid.uuid4(), uuid.uuid4()
    conversation, message = uuid.uuid4(), uuid.uuid4()
    inbox, dead_letter = uuid.uuid4(), uuid.uuid4()
    _execute(
        url,
        f"insert into contacts (id, tenant_id) values ('{contact}', '{tenant}')",
        "insert into contact_identities (id, tenant_id, contact_id, channel, external_id) "
        f"values ('{identity}', '{tenant}', '{contact}', 'whatsapp', '96170000001')",
        "insert into conversations (id, tenant_id, contact_id, channel, state) "
        f"values ('{conversation}', '{tenant}', '{contact}', 'whatsapp', 'AI_ACTIVE')",
        "insert into messages (id, tenant_id, conversation_id, direction, modality, status) "
        f"values ('{message}', '{tenant}', '{conversation}', 'INBOUND', 'TEXT', 'RECEIVED')",
        "insert into webhook_inbox (id, provider, provider_event_id, tenant_id, payload, "
        f"status, attempts) values ('{inbox}', 'WHATSAPP', 'seed', '{tenant}', '{{}}', "
        "'RECEIVED', 0)",
        "insert into dead_letter_jobs (id, tenant_id, job_name, payload, error, attempts) "
        f"values ('{dead_letter}', '{tenant}', 'seed', '{{}}', 'seed', 1)",
    )


def test_upgrade_head_on_an_empty_database_creates_every_table(lifecycle_url: str):
    assert _table_names(lifecycle_url) <= {"alembic_version"}
    command.upgrade(alembic_config(lifecycle_url), "head")
    assert _table_names(lifecycle_url) == EXPECTED_TABLES


def test_downgrade_base_leaves_nothing_behind(lifecycle_url: str):
    # Review Focus 4. "downgrade works" usually means "did not raise". A leftover
    # index or constraint makes the NEXT upgrade fail on a database that looks
    # empty.
    command.upgrade(alembic_config(lifecycle_url), "head")
    command.downgrade(alembic_config(lifecycle_url), "base")
    assert _table_names(lifecycle_url) == {"alembic_version"}


def test_upgrade_downgrade_upgrade_is_repeatable(lifecycle_url: str):
    config = alembic_config(lifecycle_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert _table_names(lifecycle_url) == EXPECTED_TABLES


def test_models_and_migrations_do_not_drift(migrated_database: str):
    # Review Focus 5. A column added to a model and never migrated works on every
    # machine whose database someone fixed by hand, and fails on a fresh deploy.
    #
    # Covers: added/removed tables and columns, type changes, nullability,
    # indexes, unique constraints. Does NOT cover CHECK constraints — alembic
    # has no comparison for them at all. tests/db/test_constraints.py proves
    # those at runtime instead.
    async def _diff() -> list:
        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(_compare)
        finally:
            await engine.dispose()

    def _compare(sync_connection) -> list:
        context = MigrationContext.configure(
            sync_connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        return compare_metadata(context, Base.metadata)

    assert asyncio.run(_diff()) == []


def test_one_step_downgrade_and_upgrade_is_repeatable(lifecycle_url: str):
    """VS-004's migration, specifically: head -> -1 -> head.

    The base-to-head round trip above would still pass if the newest revision's
    downgrade dropped a table its upgrade never created, because everything is
    dropped by the end anyway. Stepping back exactly one revision and forward
    again is what proves THIS revision reverses itself - the CHECK swap
    included, which nothing else in this suite can see.
    """
    config = alembic_config(lifecycle_url)
    command.upgrade(config, "head")
    command.downgrade(config, "-1")
    command.upgrade(config, "head")
    assert _table_names(lifecycle_url) == EXPECTED_TABLES


def test_every_revision_steps_down_and_up(lifecycle_url: str):
    """The one-step check above, applied to EVERY revision rather than the newest.

    It generalises what VS-004's test proved for its own migration: stand on
    each revision in turn, step back one, and come forward again. A downgrade
    that forgets to reverse something is invisible to the base-to-head round
    trip, because everything is dropped by the end there anyway.

    Revisions are walked by id from the ScriptDirectory, never by "-1" from
    head, so adding a newer revision cannot move what this covers.
    """
    config = alembic_config(lifecycle_url)
    script = ScriptDirectory.from_config(config)
    revisions = [revision.revision for revision in script.walk_revisions("base", "heads")][::-1]

    assert len(revisions) >= 2, "a one-step walk needs at least two revisions"
    for index, revision in enumerate(revisions):
        command.upgrade(config, revision)
        previous = revisions[index - 1] if index else "base"
        command.downgrade(config, previous)
        command.upgrade(config, revision)
    command.upgrade(config, "head")
    assert _table_names(lifecycle_url) == EXPECTED_TABLES


def test_upgrade_keeps_existing_uuid_tenants_as_their_text_form(lifecycle_url: str):
    """D1's data question: what happens to the tenants already stored?

    `USING tenant_id::text` gives PostgreSQL's canonical spelling - lowercase
    and hyphenated. Plan conflict C3: if the configured tenant is spelled any
    other way, it is now a DIFFERENT tenant and its contacts become invisible.
    Pinning the conversion here is what makes that a known, checkable fact.
    """
    config = alembic_config(lifecycle_url)
    command.upgrade(config, VS004_REVISION)
    tenant = uuid.UUID("00000000-0000-4000-8000-00000000000a")
    _seed_uuid_tenant_rows(lifecycle_url, tenant)

    command.upgrade(config, TENANT_TEXT_REVISION)

    for table in ("contacts", "contact_identities", "conversations", "messages"):
        assert _rows(lifecycle_url, f"select tenant_id from {table}") == [(str(tenant),)]
    for table in ("webhook_inbox", "dead_letter_jobs"):
        assert _rows(lifecycle_url, f"select tenant_id from {table}") == [(str(tenant),)]


def test_every_tenant_index_and_unique_constraint_survives_the_type_change(lifecycle_url: str):
    """U4, proved on the real migration rather than on a scratch table.

    PostgreSQL rebuilds dependent indexes as part of ALTER COLUMN TYPE. If it
    ever stopped doing that, `uq_contact_identities_identity` would be gone and
    one patient could become two contacts - silently, and only under a race.
    """
    command.upgrade(alembic_config(lifecycle_url), TENANT_TEXT_REVISION)

    definitions = dict(
        _rows(
            lifecycle_url,
            "select indexname, indexdef from pg_indexes where schemaname = 'public'",
        )
    )
    for name in (
        "ix_contacts_tenant_id",
        "uq_contact_identities_identity",
        "uq_conversations_open",
        "ix_messages_tenant_id_conversation_id_created_at",
    ):
        assert name in definitions, f"{name} did not survive the type change"
        assert "tenant_id" in definitions[name]
    # The partial index is the fragile one: its predicate is not part of the
    # column, so a rebuild that dropped it would leave a working index that
    # enforces the wrong thing.
    assert "WHERE" in definitions["uq_conversations_open"].upper()
    assert "CLOSED" in definitions["uq_conversations_open"]

    types = dict(
        _rows(
            lifecycle_url,
            "select table_name, data_type from information_schema.columns "
            "where column_name = 'tenant_id' and table_schema = 'public'",
        )
    )
    assert set(types) == {
        "contacts",
        "contact_identities",
        "conversations",
        "messages",
        "webhook_inbox",
        "dead_letter_jobs",
    }
    assert set(types.values()) == {"text"}


def test_downgrade_refuses_a_tenant_id_that_is_not_a_uuid(lifecycle_url: str):
    """The downgrade fails loudly on an opaque tenant, and that is correct.

    The alternative would be inventing a UUID for a clinic, which is data loss
    dressed up as a migration. The whole downgrade runs in one transaction, so a
    refusal leaves the database exactly where it was.
    """
    config = alembic_config(lifecycle_url)
    command.upgrade(config, TENANT_TEXT_REVISION)
    contact = uuid.uuid4()
    _execute(
        lifecycle_url,
        f"insert into contacts (id, tenant_id) values ('{contact}', 'clinic-alpha')",
    )

    with pytest.raises(Exception):  # noqa: B017 - the driver's error type is not the point
        command.downgrade(config, VS004_REVISION)

    assert _rows(lifecycle_url, "select version_num from alembic_version") == [
        (TENANT_TEXT_REVISION,)
    ]
    assert _rows(
        lifecycle_url,
        "select data_type from information_schema.columns "
        "where table_name = 'contacts' and column_name = 'tenant_id'",
    ) == [("text",)]

    # With the offending row gone, the same downgrade succeeds: the refusal is
    # about the data, not about the migration being irreversible.
    _execute(lifecycle_url, "delete from contacts")
    command.downgrade(config, VS004_REVISION)
    assert _rows(lifecycle_url, "select version_num from alembic_version") == [(VS004_REVISION,)]
