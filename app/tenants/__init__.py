"""Which clinic a WhatsApp number belongs to.

Hard rule 4: tenant_id is resolved by our backend from the receiving
phone_number_id. It is never taken from LLM output, never a webhook field the
caller controls, and never a tool argument.
"""

from app.tenants.resolver import (
    ConfigTenantResolver,
    TenantId,
    TenantMapError,
    TenantResolver,
    UnknownPhoneNumberError,
)

__all__ = [
    "ConfigTenantResolver",
    "TenantId",
    "TenantMapError",
    "TenantResolver",
    "UnknownPhoneNumberError",
]
