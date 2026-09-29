"""The tenant boundary, made structural."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.tenants.ids import TenantId


class Repository:
    """A repository with no tenant dimension.

    Only webhook_inbox and dead_letter_jobs qualify: both are written before
    (or instead of) tenant resolution.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session


class TenantScopedRepository(Repository):
    """Every statement this emits filters on tenant_id.

    Hard rule 4: tenant_id is resolved by our backend from the receiving
    phone_number_id, never taken from LLM output. Taking it in __init__ rather
    than per method means there is no call site that can forget it and no method
    signature the agent layer could be tempted to expose as a tool argument.

    The check runs before the session is touched, which is what lets the
    acceptance test for it run with no database.
    """

    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        # isinstance, not a truthiness check: decision D1 made the tenant a
        # TEXT column, so a uuid.UUID object left behind by stale code would be
        # truthy here and then stringified by the driver into a tenant nobody
        # configured. This is the one place that can still catch it.
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("tenant_id is required")
        super().__init__(session)
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id
