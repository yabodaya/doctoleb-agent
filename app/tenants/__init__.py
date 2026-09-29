"""Which clinic a WhatsApp number belongs to.

Hard rule 4: tenant_id is resolved by our backend from the receiving
phone_number_id. It is never taken from LLM output, never a webhook field the
caller controls, and never a tool argument.

**This package deliberately re-exports NOTHING.** Import from the submodule:

    from app.tenants.ids import TenantId            # the type, no dependencies
    from app.tenants.resolver import ConfigTenantResolver, TenantResolver

Because Python executes a package's `__init__` before any submodule of it, a
re-export here would mean that `from app.tenants.ids import TenantId` also
imported `resolver`, and therefore `app.config`. `app/agent/` and `app/db/` both
name the tenant type and neither may depend on the configuration layer (plan
section 5.7's forbidden-import list, and the VS-006 execution amendment B1), so
the convenience is not worth what it costs.
"""
