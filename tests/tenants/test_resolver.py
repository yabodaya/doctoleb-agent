"""Hard rule 4: the only door a tenant_id comes through.

No database and no network: every test here builds a resolver from a Settings
object and asks it one question.
"""

import json
import logging

import pytest

from app.config import Settings
from app.tenants.resolver import (
    ConfigTenantResolver,
    TenantMapError,
    TenantResolver,
    UnknownPhoneNumberError,
)

PHONE_NUMBER_ID = "100000000000001"
OTHER_PHONE_NUMBER_ID = "100000000000002"
# Decision D1: a tenant id is an OPAQUE string. These are deliberately not
# UUIDs, so any code that quietly parses one fails here rather than in front of
# a patient.
TENANT = "clinic-alpha"
OTHER_TENANT = "clinic-beta"


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
                {PHONE_NUMBER_ID: TENANT, OTHER_PHONE_NUMBER_ID: OTHER_TENANT}
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


def test_a_map_entry_may_be_any_clean_string():
    """The inversion of VS-002's "not a uuid raises" (decision D1).

    The Booking Service has not decided the tenant id's format - it is probably
    a clinic username. Refusing anything that is not a UUID would mean this repo
    had decided it instead.
    """
    settings = settings_with(whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: "clinic-one"}))

    assert ConfigTenantResolver.from_settings(settings).resolve(PHONE_NUMBER_ID) == "clinic-one"


def test_an_opaque_tenant_id_is_returned_exactly_as_configured():
    """D1: never parsed, never normalised, never reformatted.

    The value is a key the Booking Service will look up over HTTP. Anything this
    repo does to it in passing is a lookup that quietly misses.
    """
    settings = settings_with(whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: "Clinic_User.01"}))

    resolved = ConfigTenantResolver.from_settings(settings).resolve(PHONE_NUMBER_ID)

    assert resolved == "Clinic_User.01"
    assert type(resolved) is str


def test_a_uuid_shaped_tenant_id_stays_a_string():
    """A UUID-shaped value is not a UUID, it is a string that looks like one.

    Parsing it would round-trip "00000000-0000-4000-8000-00000000000A" into its
    lowercase canonical form, which is a DIFFERENT tenant under D1.
    """
    upper = "00000000-0000-4000-8000-00000000000A"
    settings = settings_with(whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: upper}))

    resolved = ConfigTenantResolver.from_settings(settings).resolve(PHONE_NUMBER_ID)

    assert type(resolved) is str
    assert resolved == upper


@pytest.mark.parametrize(
    "value",
    [
        42,  # a JSON number
        None,  # a JSON null
        "",  # empty
        " clinic",  # leading whitespace
        "clinic ",  # trailing whitespace
        "cli\nnic",  # a newline: unsafe in the X-Tenant-Id header (conflict C4e)
        "​clinic",  # a zero-width space: invisible, and a different tenant
    ],
)
def test_a_tenant_id_that_is_not_a_clean_string_is_refused(value):
    """Q2's hygiene. Opaque does not mean anything goes.

    The value ends up in an HTTP header, so CR/LF is a header-injection hazard;
    and whitespace or a zero-width character produces a tenant nobody can see is
    different from the one they meant.
    """
    settings = settings_with(whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: value}))

    with pytest.raises(TenantMapError):
        ConfigTenantResolver.from_settings(settings)


def test_tenant_ids_are_matched_exactly_not_case_folded():
    """Q2: exact, case-sensitive. Two spellings are two tenants.

    Case folding would be this repo inventing an equivalence rule for someone
    else's identifier, which is hard rule 4's mistake in a different costume.
    """
    resolver = ConfigTenantResolver.from_settings(
        settings_with(
            whatsapp_tenant_map=json.dumps(
                {PHONE_NUMBER_ID: "Clinic-Alpha", OTHER_PHONE_NUMBER_ID: "clinic-alpha"}
            )
        )
    )

    assert resolver.resolve(PHONE_NUMBER_ID) == "Clinic-Alpha"
    assert resolver.resolve(OTHER_PHONE_NUMBER_ID) == "clinic-alpha"
    assert resolver.resolve(PHONE_NUMBER_ID) != resolver.resolve(OTHER_PHONE_NUMBER_ID)


def test_the_dev_fallback_builds_a_one_entry_map():
    """Assumption A2. Both keys already existed in .env.example and nothing read
    them; the developer has one number and one clinic, and the live test should
    not need hand-written JSON to run."""
    resolver = ConfigTenantResolver.from_settings(
        settings_with(meta_phone_number_id=PHONE_NUMBER_ID, dev_tenant_id=TENANT)
    )

    assert resolver.resolve(PHONE_NUMBER_ID) == TENANT


def test_an_explicit_map_wins_over_the_dev_fallback():
    """Two ways to configure one thing, so the precedence is pinned.

    Silently merging them would make "why is this number going to the wrong
    clinic?" a question with two possible answers.
    """
    resolver = ConfigTenantResolver.from_settings(
        settings_with(
            whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: TENANT}),
            meta_phone_number_id=OTHER_PHONE_NUMBER_ID,
            dev_tenant_id=OTHER_TENANT,
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
    resolver = ConfigTenantResolver.from_settings(settings_with(dev_tenant_id=TENANT))

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
            settings_with(whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: TENANT}))
        )

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "entries=1" in rendered
    assert TENANT not in rendered
    assert PHONE_NUMBER_ID not in rendered


def test_a_broken_map_does_not_name_the_offending_value(caplog):
    # " SENTINEL-tenant " is refused by Q2's whitespace rule. The point is that
    # the value never reaches the exception: an exception message is the least
    # controlled string in the system.
    with caplog.at_level(logging.DEBUG), pytest.raises(TenantMapError) as raised:
        ConfigTenantResolver.from_settings(
            settings_with(whatsapp_tenant_map=json.dumps({PHONE_NUMBER_ID: " SENTINEL-tenant "}))
        )

    assert "SENTINEL" not in str(raised.value)
    assert "SENTINEL" not in "\n".join(record.getMessage() for record in caplog.records)
    assert raised.value.reason == "bad_tenant_map"


def test_the_config_resolver_satisfies_the_protocol():
    """So a fake in a worker test and the real thing cannot drift apart."""
    assert isinstance(ConfigTenantResolver({}), TenantResolver)
