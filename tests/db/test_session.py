"""The engine's own hard-rule-8 setting. No database: building an engine does
not connect to one."""

import pytest

from app.db.session import dispose_engine, get_engine


async def test_the_app_engine_hides_bound_parameters():
    # Hard rule 8, systematically. Every SQLAlchemy DBAPI error message appends
    # the statement's bound parameters as `[parameters: ...]`, and in this repo
    # those parameters are message text, raw Meta payloads and phone numbers.
    # as_duplicate() only sanitises the two paths that go through it; this
    # setting covers every statement the app will ever emit, including ones a
    # later slice has not written yet.
    #
    # Asserted on sync_engine because AsyncEngine does not proxy the attribute.
    await dispose_engine()  # ignore whatever an earlier test built
    try:
        assert get_engine().sync_engine.hide_parameters is True
    finally:
        await dispose_engine()


def test_the_production_sessionmaker_uses_the_shared_session_options():
    """SESSION_OPTIONS is the single definition, and production really uses it.

    Asserted on the built factory rather than on the constant, so removing an
    option from get_sessionmaker() while leaving the constant intact fails here.
    """
    from app.db.session import SESSION_OPTIONS, get_sessionmaker

    factory = get_sessionmaker()

    assert SESSION_OPTIONS["expire_on_commit"] is False
    assert SESSION_OPTIONS["autoflush"] is False
    for option, expected in SESSION_OPTIONS.items():
        assert factory.kw[option] is expected, option


@pytest.mark.db
def test_the_worker_test_sessionmaker_matches_production(db_engine):
    """The harness must not be able to diverge from production silently.

    VS-004's job commits the claim and then reads row.payload, which works only
    because sessions do not expire on commit. A test factory that set
    expire_on_commit=False by hand would keep passing if production ever stopped
    doing it - and the job would fail live with MissingGreenlet while the suite
    stayed green. This pins them together.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.db.session import SESSION_OPTIONS, get_sessionmaker

    harness = async_sessionmaker(bind=db_engine, **SESSION_OPTIONS)
    production = get_sessionmaker()

    for option in SESSION_OPTIONS:
        assert harness.kw[option] is production.kw[option], option
