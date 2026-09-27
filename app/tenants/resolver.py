"""phone_number_id -> tenant, behind one small interface.

Hard rule 4 has exactly one implementation here, and the type of a tenant id is
named in exactly one place, because VS-002 left that question open and this is
the first slice that depends on the answer.
"""

import json
import logging
import uuid
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from app.config import Settings

logger = logging.getLogger(__name__)

# The one place this repo names the tenant id type. VS-002 left it unconfirmed
# (plan conflict note C11) and every table has it as sa.Uuid; if it ever becomes
# something else, this alias plus one migration are the change, rather than every
# signature in app/worker/.
TenantId = uuid.UUID


class UnknownPhoneNumberError(Exception):
    """No clinic is mapped to this phone_number_id.

    A permanent failure, never a guess. There is no default tenant: guessing
    would file one clinic's patient into another clinic's inbox, which is the
    single worst thing this system could do quietly.

    Carries the phone_number_id, which is safe to keep and to log - it is Meta's
    numeric identifier for a CLINIC's WhatsApp account, not a phone number and
    not a patient. display_phone_number, wa_id and recipient_id are the ones that
    are patient content.
    """

    def __init__(self, phone_number_id: str) -> None:
        self.phone_number_id = phone_number_id
        super().__init__(f"no tenant for phone_number_id {phone_number_id}")


class TenantMapError(Exception):
    """The configured tenant map could not be read.

    Carries a short code and never the offending value: the map's values are
    tenant ids and its keys are account ids, and an exception message is the
    least controlled place in the system.
    """

    def __init__(self, reason: str = "bad_tenant_map") -> None:
        self.reason = reason
        super().__init__(reason)


@runtime_checkable
class TenantResolver(Protocol):
    """phone_number_id -> tenant.

    A Protocol rather than a base class, so a test can pass a three-line fake and
    the worker never has to import the config-parsing path to be tested.

    Hard rule 4: this is the ONLY way a tenant_id enters the system. It is not an
    argument the LLM can supply, not a field of the webhook payload we trust, and
    not a job argument.
    """

    def resolve(self, phone_number_id: str) -> TenantId: ...


class ConfigTenantResolver:
    """The tenant map, read from settings once at worker startup.

    Parsed at construction rather than per call: a typo produces one startup
    failure per process instead of one per event, and `resolve` stays a dict
    lookup that cannot fail for an interesting reason.
    """

    def __init__(self, mapping: Mapping[str, TenantId]) -> None:
        self._mapping = dict(mapping)

    @classmethod
    def from_settings(cls, settings: Settings) -> "ConfigTenantResolver":
        """Build from WHATSAPP_TENANT_MAP, or from the single-tenant fallback.

        The map is a JSON string rather than a typed dict field (plan assumption
        A1), so a blank or broken value cannot stop the process from booting.
        What it does instead is raise here, at worker startup, where the reason
        is one log line rather than a container that will not start.

        The fallback (assumption A2) exists because the developer has one number
        and one clinic, and the live test should not need hand-written JSON.
        An explicit map always wins, and the log line names which source was used
        and how many entries it had - never a key and never a value.
        """
        raw = settings.whatsapp_tenant_map.strip()
        if raw:
            mapping = cls._parse(raw)
            source = "WHATSAPP_TENANT_MAP"
        elif settings.dev_tenant_id and settings.meta_phone_number_id:
            mapping = cls._parse_pair(settings.meta_phone_number_id, settings.dev_tenant_id)
            source = "DEV_TENANT_ID"
        else:
            mapping = {}
            source = "none"

        # Counts and a source name only (hard rule 8/9). An empty map is not an
        # error here: every event then dead-letters with unknown_phone_number,
        # which is loud in a table built for it, and /health stays up.
        logger.info("tenant map loaded source=%s entries=%d", source, len(mapping))
        return cls(mapping)

    @staticmethod
    def _parse(raw: str) -> dict[str, TenantId]:
        try:
            decoded = json.loads(raw)
        except ValueError:
            raise TenantMapError() from None
        if not isinstance(decoded, dict):
            raise TenantMapError() from None
        mapping: dict[str, TenantId] = {}
        for key, value in decoded.items():
            mapping.update(ConfigTenantResolver._parse_pair(key, value))
        return mapping

    @staticmethod
    def _parse_pair(phone_number_id: object, tenant_id: object) -> dict[str, TenantId]:
        if not isinstance(phone_number_id, str) or not isinstance(tenant_id, str):
            raise TenantMapError() from None
        try:
            return {phone_number_id: TenantId(tenant_id)}
        except (ValueError, AttributeError, TypeError):
            # ValueError covers a malformed uuid; the others cover a JSON value
            # that got through the isinstance check in some future edit. The
            # offending value is deliberately not in the exception.
            raise TenantMapError() from None

    def resolve(self, phone_number_id: str) -> TenantId:
        """The tenant for this number, or a permanent error.

        Raises rather than returning None, because there is no sensible thing for
        a caller to do with None that is not "guess" (hard rule 4).
        """
        if not phone_number_id:
            raise UnknownPhoneNumberError("")
        try:
            return self._mapping[phone_number_id]
        except KeyError:
            raise UnknownPhoneNumberError(phone_number_id) from None
