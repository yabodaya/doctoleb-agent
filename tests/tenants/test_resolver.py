"""Hard rule 4: the only door a tenant_id comes through.

No database and no network: every test here builds a resolver from a Settings
object and asks it one question.
"""

import json
import logging
import uuid

import pytest

from app.config import Settings
from app.tenants import (
    ConfigTenantResolver,
    TenantMapError,
    TenantResolver,
    UnknownPhoneNumberError,
)

PHONE_NUMBER_ID = "100000000000001"
OTHER_PHONE_NUMBER_ID = "100000000000002"
TENANT = uuid.UUID("00000000-0000-4000-8000-00000000000a")
OTHER_TENANT = uuid.UUID("00000000-0000-4000-8000-00000000000b")


def settings_with(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+asyncpg://user:pw@db:5432/doctoleb",
        "redis_url": "redis://cache:6379/1",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_a_known_phone_number_id_resolves_to_its_tenant():
    resolver = ConfigTenantResolver({PHONE_NUMBER_ID: TENANT})

    assert resolver.resolve(PHONE_NUMBER_ID) == TENANT


def test_an_unknown_phone_number_id_raises_rather_than_guessing():
    """Hard rule 4 has no default tenant, and this is where that is enforced.

    Returning None, or falling back to "the only tenant we know", would file one
    clinic's patient into another clinic's inbox - quietly, and at exactly the
    moment a second clinic is onboarded.
    """
    resolver = ConfigTenantResolver({PHONE_NUMBER_ID: TENANT})

    with pytest.raises(UnknownPhoneNumberError):
        resolver.resolve(OTHER_PHONE_NUMBER_ID)


def test_an_empty_phone_number_id_raises():
    """A payload with no metadata.phone_number_id must not resolve to anything.

    The empty string is a perfectly good dict key, so without the explicit guard
    a map that ever acquired an "" entry would answer this.
    """
    resolver = ConfigTenantResolver({PHONE_NUMBER_ID: TENANT})

    with pytest.raises(UnknownPhoneNumberError):
        resolver.resolve("")


def test_the_map_is_read_from_json_in_settings():
    resolver = ConfigTenantResolver.from_settings(
        settings_with(
            whatsapp_tenant_map=json.dumps(
                {PHONE_NUMBER_ID: str(TENANT), OTHER_PHONE_NUMBER_ID: str(OTHER_TENANT)}
            )
        )
    )

    assert resolver.resolve(PHONE_NUMBER_ID) == TENANT
    assert resolver.resolve(OTHER_PHONE_NUMBER_ID) == OTHER_TENANT


def test_a_malformed_map_raises_tenant_map_error_at_construction_not_at_import():
    """Assumption A1. The map is a str on Settings precisely so this is the
    failure point: a typed dict field would raise while pydantic-settings read
    the .env file, and the app would not boot at all."""
    settings = settings_with(whatsapp_tenant_map="{not json")

    with pytest.raises(TenantMapError):
        ConfigTenantResolver.from_settings(settings)


def test_a_map_that_is_not_an_object_raises():
    with pytest.raises(TenantMapError):
        ConfigTenantResolver.from_settings(settings_with(whatsapp_tenant_map='["a", "b"]'))


def test_a_map_entry_whose_value_is_not_a_uuid_raises():
    settings = settings_with(whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: "clinic-one"}))

    with pytest.raises(TenantMapError):
        ConfigTenantResolver.from_settings(settings)


def test_the_dev_fallback_builds_a_one_entry_map():
    """Assumption A2. Both keys already existed in .env.example and nothing read
    them; the developer has one number and one clinic, and the live test should
    not need hand-written JSON to run."""
    resolver = ConfigTenantResolver.from_settings(
        settings_with(meta_phone_number_id=PHONE_NUMBER_ID, dev_tenant_id=str(TENANT))
    )

    assert resolver.resolve(PHONE_NUMBER_ID) == TENANT


def test_an_explicit_map_wins_over_the_dev_fallback():
    """Two ways to configure one thing, so the precedence is pinned.

    Silently merging them would make "why is this number going to the wrong
    clinic?" a question with two possible answers.
    """
    resolver = ConfigTenantResolver.from_settings(
        settings_with(
            whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: str(TENANT)}),
            meta_phone_number_id=OTHER_PHONE_NUMBER_ID,
            dev_tenant_id=str(OTHER_TENANT),
        )
    )

    assert resolver.resolve(PHONE_NUMBER_ID) == TENANT
    with pytest.raises(UnknownPhoneNumberError):
        resolver.resolve(OTHER_PHONE_NUMBER_ID)


def test_a_dev_tenant_id_without_a_phone_number_id_resolves_nothing():
    """Half the fallback is not the fallback.

    Guessing which number the lone tenant id belongs to is the same mistake as
    having a default tenant.
    """
    resolver = ConfigTenantResolver.from_settings(settings_with(dev_tenant_id=str(TENANT)))

    with pytest.raises(UnknownPhoneNumberError):
        resolver.resolve(PHONE_NUMBER_ID)


def test_no_map_and_no_fallback_resolves_nothing_without_raising_at_startup():
    """Assumption A1: an unconfigured map is not a boot failure.

    Every event then dead-letters with unknown_phone_number, which is loud in a
    table built for exactly that, and /health stays up so the rest of the system
    is diagnosable.
    """
    resolver = ConfigTenantResolver.from_settings(settings_with())

    with pytest.raises(UnknownPhoneNumberError):
        resolver.resolve(PHONE_NUMBER_ID)


def test_the_resolver_never_logs_a_tenant_map_value(caplog):
    """Hard rule 8/9: the startup line names the source and a count.

    A tenant id is not patient content, but it is the key that scopes every query
    in the system, and a log line is the wrong place to publish one.
    """
    with caplog.at_level(logging.DEBUG):
        ConfigTenantResolver.from_settings(
            settings_with(whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: str(TENANT)}))
        )

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "entries=1" in rendered
    assert str(TENANT) not in rendered
    assert PHONE_NUMBER_ID not in rendered


def test_a_broken_map_does_not_name_the_offending_value(caplog):
    with caplog.at_level(logging.DEBUG), pytest.raises(TenantMapError) as raised:
        ConfigTenantResolver.from_settings(
            settings_with(whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: "not-a-uuid"}))
        )

    assert "not-a-uuid" not in str(raised.value)
    assert raised.value.reason == "bad_tenant_map"


def test_the_config_resolver_satisfies_the_protocol():
    """So a fake in a worker test and the real thing cannot drift apart."""
    assert isinstance(ConfigTenantResolver({}), TenantResolver)
