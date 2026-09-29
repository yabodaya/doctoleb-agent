"""The type of a tenant id, and nothing else.

A module of its own, with no imports at all, for one reason: `app/agent/` and
`app/db/` must both be able to name this type without importing `app.config`.
`app.tenants.resolver` reads Settings, so an import that reached it would drag
the whole configuration layer into the Agent Core's import graph - which plan
section 5.7's forbidden-import list exists to prevent.

This only works because `app/tenants/__init__.py` re-exports NOTHING: Python
executes a package's `__init__` before any submodule of it, so a convenience
re-export there would pull `resolver` in anyway, however carefully the importer
spelled its import. See that file.
"""

# Decision D1. A tenant id is an OPAQUE string - probably a clinic username at
# the Booking Service, whose owner has not decided the format yet. So this repo
# never parses it, never normalises it, never reformats it and never compares it
# case-insensitively: it travels from the resolver to the X-Tenant-Id header and
# to the tenant_id TEXT columns exactly as configured.
#
# It is a plain alias rather than a NewType or a str subclass on purpose: those
# would be places a format could be smuggled back in later.
TenantId = str
