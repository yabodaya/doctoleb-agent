"""Repository construction rules. No database and no marker: the slice's
"repository queries require tenant_id" criterion has to be provable with
nothing running."""

import uuid

import pytest

from app.db.repositories import (
    ContactRepository,
    ConversationRepository,
    MessageRepository,
)
from app.db.repositories.base import Repository, TenantScopedRepository


def test_a_tenant_scoped_repository_cannot_be_built_without_a_tenant():
    # Hard rule 4 made structural: there is no call site that can forget it.
    # `None` for the session is deliberate — __init__ must reject the missing
    # tenant before it ever looks at a connection.
    #
    # uuid.uuid4() is in the list because of decision D1: tenant ids are opaque
    # strings now, and the columns are TEXT. Stale code that still passes a UUID
    # object must fail here, loudly, rather than have SQLAlchemy stringify it
    # into a tenant nobody configured.
    for repository in (ContactRepository, ConversationRepository, MessageRepository):
        for missing in (None, "", uuid.uuid4()):
            with pytest.raises(ValueError, match="tenant_id"):
                repository(None, missing)


def test_only_the_pre_tenant_tables_use_the_unscoped_repository():
    # webhook_inbox and dead_letter_jobs are written before, or instead of,
    # tenant resolution. Nothing else may opt out of the tenant filter.
    for repository in (ContactRepository, ConversationRepository, MessageRepository):
        assert issubclass(repository, TenantScopedRepository)
    assert issubclass(TenantScopedRepository, Repository)
