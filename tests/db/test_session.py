"""The engine's own hard-rule-8 setting. No database: building an engine does
not connect to one."""

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
