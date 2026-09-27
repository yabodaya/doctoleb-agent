import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def test_settings_reads_values_from_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://user:pw@db:5432/doctoleb\n"
        "REDIS_URL=redis://cache:6379/1\n"
    )

    settings = Settings(_env_file=env_file)

    assert settings.database_url == "postgresql+asyncpg://user:pw@db:5432/doctoleb"
    assert settings.redis_url == "redis://cache:6379/1"


def test_settings_ignores_env_file_keys_this_slice_does_not_model(tmp_path, monkeypatch):
    """Review Focus 1.

    .env.example already lists keys later slices need. pydantic-settings
    defaults to extra="forbid" and would reject the whole file because of them.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://user:pw@db:5432/doctoleb\n"
        "REDIS_URL=redis://cache:6379/1\n"
        "META_ACCESS_TOKEN=placeholder\n"
        "OPENAI_CHAT_MODEL=placeholder\n"
        "BOOKING_CLIENT=fake\n"
    )

    settings = Settings(_env_file=env_file)

    assert settings.redis_url == "redis://cache:6379/1"


def test_settings_fails_loudly_when_a_required_value_is_missing(monkeypatch):
    """Review Focus 5. A broken config must stop the process, not start a half-app."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_get_settings_returns_the_same_object_every_call():
    assert get_settings() is get_settings()


def test_meta_secrets_default_to_empty_and_do_not_block_startup(monkeypatch):
    """Assumption A3. The app must boot without a Meta app.

    Making these required would mean pytest and `docker compose up` fail for a
    developer who has no Meta developer app yet — which is exactly the developer
    this slice is written for. The rejection happens per request instead, where
    it is total: see tests/api/test_whatsapp_webhook.py and _verify.py.
    """
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.delenv("META_VERIFY_TOKEN", raising=False)

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:pw@db:5432/doctoleb",
        redis_url="redis://cache:6379/1",
    )

    assert settings.meta_app_secret == ""
    assert settings.meta_verify_token == ""


def test_meta_secrets_are_read_from_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.delenv("META_VERIFY_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://user:pw@db:5432/doctoleb\n"
        "REDIS_URL=redis://cache:6379/1\n"
        "META_APP_SECRET=not-a-real-secret\n"
        "META_VERIFY_TOKEN=not-a-real-token\n"
    )

    settings = Settings(_env_file=env_file)

    assert settings.meta_app_secret == "not-a-real-secret"
    assert settings.meta_verify_token == "not-a-real-token"


def test_docs_are_disabled_by_default_and_are_not_tied_to_app_env(monkeypatch):
    """Assumption A7.

    DOCS_ENABLED is deliberately its own switch. Gating the OpenAPI surface on
    APP_ENV would be open at exactly the wrong moment: the tunnel that publishes
    this API to the internet runs while APP_ENV=development.
    """
    monkeypatch.delenv("DOCS_ENABLED", raising=False)
    base = {
        "database_url": "postgresql+asyncpg://user:pw@db:5432/doctoleb",
        "redis_url": "redis://cache:6379/1",
    }

    assert Settings(_env_file=None, app_env="development", **base).docs_enabled is False
    assert Settings(_env_file=None, app_env="production", **base).docs_enabled is False
    # And it is a real env-driven boolean, not a constant.
    monkeypatch.setenv("DOCS_ENABLED", "true")
    assert Settings(_env_file=None, **base).docs_enabled is True
