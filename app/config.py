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

    # Whether this process publishes /docs, /redoc and /openapi.json.
    # Off by default, and deliberately NOT derived from app_env: VS-003 puts this
    # API behind a public tunnel so Meta can reach it, and that tunnel runs while
    # APP_ENV=development — so an app_env-based gate would be open at precisely
    # the moment the API is reachable from the internet.
    # Turn it on locally when you want Swagger UI, with no tunnel running.
    docs_enabled: bool = False

    # No defaults: a missing DSN must stop the process rather than silently
    # point at something wrong, and a default would put a credential in source.
    database_url: str
    redis_url: str

    # Meta WhatsApp Cloud API. Deliberately NOT required, and deliberately
    # empty by default:
    #   * the app must boot without a Meta app, or no other slice can be worked
    #     on and `pytest` fails for a developer who has not been granted one yet;
    #   * an empty value is not a permissive value. app_secret="" rejects every
    #     POST (an empty HMAC key is a valid key, so "unset" must mean "reject"),
    #     and verify_token="" rejects every handshake.
    # app/channels/whatsapp/signature.py and app/api/whatsapp.py enforce that.
    meta_app_secret: str = ""
    meta_verify_token: str = ""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, building them on first use."""
    return Settings()
