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
