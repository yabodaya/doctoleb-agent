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


def test_startup_warns_when_the_job_timeout_cannot_cover_both_calls():
    """Plan assumption A14. A loud runtime signal, not a dead process: the api
    must not refuse to boot over a worker knob.

    A job arq times out is finished as failed and never retried, and none of our
    exit paths run - so the event is stranded with no dead letter.
    """
    warnings = startup_warnings(
        _settings(job_timeout_seconds=20, openai_timeout_seconds=30, meta_send_timeout_seconds=10)
    )

    timeout_warning = [w for w in warnings if "JOB_TIMEOUT_SECONDS" in w]
    assert len(timeout_warning) == 1
    for fragment in ("20", "OPENAI_TIMEOUT_SECONDS=30", "META_SEND_TIMEOUT_SECONDS=10"):
        assert fragment in timeout_warning[0], fragment


def test_startup_warnings_name_settings_never_values():
    """Hard rule 9: a startup log is the easiest place in the system to leak a
    key, because it is printed before anyone is watching."""
    warnings = startup_warnings(_settings(openai_api_key="sk-SENTINEL-not-a-real-key"))

    assert all("SENTINEL" not in w for w in warnings)


def test_a_fully_configured_worker_warns_about_nothing():
    """So the warnings mean something when they do appear."""
    assert startup_warnings(_settings()) == []
