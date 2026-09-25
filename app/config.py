"""Application settings.

Hard rule 9: configuration and secrets come only from environment variables.
Nothing in this module carries a literal credential.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" is required, not cosmetic. pydantic-settings defaults to
    # extra="forbid" and raises on any dotenv key the model does not declare.
    # .env.example already lists META_*, OPENAI_* and BOOKING_* keys for later
    # slices, so without this the app cannot boot from its own example file.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # No defaults: a missing DSN must stop the process rather than silently
    # point at something wrong, and a default would put a credential in source.
    database_url: str
    redis_url: str


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, building them on first use."""
    return Settings()
