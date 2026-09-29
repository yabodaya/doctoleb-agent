import os

from arq.connections import RedisSettings

from app.config import Settings
from app.worker.main import WorkerSettings, ping, startup_warnings


async def test_ping_job_returns_pong():
    assert await ping({}) == "pong"


def test_worker_registers_at_least_one_function():
    """arq refuses to start a worker with no functions and no cron jobs."""
    assert len(WorkerSettings.functions) >= 1


def test_worker_redis_settings_come_from_the_environment():
    """Hard rule 9: the worker reads REDIS_URL, it does not hardcode a host.

    Compared against whatever REDIS_URL actually holds, not a literal, so this
    passes on the host (redis://localhost:6379/0, from conftest) and inside the
    container (redis://redis:6379/0, from .env) without being two tests.
    """
    expected = RedisSettings.from_dsn(os.environ["REDIS_URL"])

    assert WorkerSettings.redis_settings.host == expected.host
    assert WorkerSettings.redis_settings.port == expected.port
    assert WorkerSettings.redis_settings.database == expected.database


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+asyncpg://user:pw@db:5432/doctoleb",
        "redis_url": "redis://cache:6379/1",
        "openai_api_key": "sk-test-not-a-real-one",
        "openai_chat_model": "test-model",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_startup_warns_when_the_openai_key_is_unset():
    """Without it, "every reply is the fallback" looks like a bug rather than a
    missing .env entry."""
    warnings = startup_warnings(_settings(openai_api_key=""))

    assert any("OPENAI_API_KEY is not set" in w for w in warnings)
    assert any("AGENT_FALLBACK_REPLY" in w for w in warnings)


def test_startup_warns_when_the_openai_model_is_unset():
    warnings = startup_warnings(_settings(openai_chat_model=""))

    assert any("OPENAI_CHAT_MODEL is not set" in w for w in warnings)


def test_startup_warns_when_the_job_timeout_cannot_cover_the_turn_and_the_send():
    """Plan assumption A14, with VS-006's arithmetic (plan section 5.2).

    The relation changed: the job must now cover the WHOLE tool loop plus the
    send, not one model call plus the send. Every model call runs inside
    AGENT_TURN_TIMEOUT_SECONDS, so OPENAI_TIMEOUT_SECONDS is no longer part of
    the sum - four of them at 30s each would be 120s on their own.

    A warning and not a boot failure: the api must not refuse to start over a
    worker knob. A job arq times out is finished as failed and never retried,
    and none of our exit paths run - so the event is stranded with no dead
    letter.
    """
    warnings = startup_warnings(
        _settings(
            job_timeout_seconds=50, agent_turn_timeout_seconds=45, meta_send_timeout_seconds=10
        )
    )

    timeout_warning = [w for w in warnings if "JOB_TIMEOUT_SECONDS" in w]
    assert len(timeout_warning) == 1
    for fragment in ("50", "AGENT_TURN_TIMEOUT_SECONDS=45", "META_SEND_TIMEOUT_SECONDS=10"):
        assert fragment in timeout_warning[0], fragment
    # The per-call deadline is deliberately NOT named: it is not in the sum any
    # more, and naming it would send an operator to the wrong knob.
    assert "OPENAI_TIMEOUT_SECONDS" not in timeout_warning[0]


def test_startup_warnings_name_settings_never_values():
    """Hard rule 9: a startup log is the easiest place in the system to leak a
    key, because it is printed before anyone is watching."""
    warnings = startup_warnings(_settings(openai_api_key="sk-SENTINEL-not-a-real-key"))

    assert all("SENTINEL" not in w for w in warnings)


def test_startup_always_warns_that_the_booking_client_is_fake():
    """Q8, and risk R8: fake availability reaching a real patient.

    Unconditional, because in VS-006 there is no other booking client to
    configure. VS-011 adds the BOOKING_CLIENT switch and the refusal to start in
    production; until then this line is the whole of the mitigation.
    """
    warnings = startup_warnings(_settings())

    fake = [w for w in warnings if "FAKE" in w]
    assert len(fake) == 1
    assert "demo data" in fake[0]
    assert "never put this worker in front of real patients" in fake[0]


def test_a_fully_configured_worker_warns_only_about_the_fake_booking_client():
    """So the OTHER warnings still mean something when they appear.

    VS-005's version asserted an empty list; the fake-booking line is now
    unconditional, so the assertion is "nothing else", which is the same
    guarantee.
    """
    assert [w for w in startup_warnings(_settings()) if "FAKE" not in w] == []
