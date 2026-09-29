"""Application settings.

Hard rule 9: configuration and secrets come only from environment variables.
Nothing in this module carries a literal credential.
"""

from functools import lru_cache
from typing import Any

from pydantic import Field, ValidationInfo, field_validator
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
        # A blank value means "not set" (plan conflict C5). .env.example lists
        # every key with an empty value, and `docker compose` turns `KEY=` into an
        # empty environment variable. Without this, `META_SEND_TIMEOUT_SECONDS=`
        # (VS-004) or `OPENAI_TIMEOUT_SECONDS=` fails float validation and neither
        # the api nor the worker boots from a copy of the example file.
        #
        # Credentials are unaffected: their default IS "", so a blank key still
        # means "every call fails visibly", never "fall back to something". A blank
        # REQUIRED value (DATABASE_URL) is still a loud failure at boot.
        env_ignore_empty=True,
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

    # The send side of the Cloud API. Same empty-by-default reasoning as the two
    # keys above: an empty token means every send fails with a permanent,
    # visible error, which is the correct behaviour for "not configured".
    #
    # meta_phone_number_id is NOT what we send from (plan assumption A7). The
    # reply goes out on the phone_number_id the message ARRIVED on, read from
    # the stored payload, because with more than one clinic this setting is
    # simply the wrong number. It exists only for the single-tenant fallback
    # map in ConfigTenantResolver.from_settings().
    meta_access_token: str = ""
    meta_phone_number_id: str = ""
    # No known-good value lives anywhere in this repo; set it to whatever the
    # Meta dashboard shows. A retired version is a permanent 4xx, not a retry.
    meta_api_version: str = "v21.0"
    # Overridable so a local fake can stand in for Meta. Tests use
    # httpx.MockTransport instead and never touch the network.
    meta_api_base_url: str = "https://graph.facebook.com"
    # Hard rule 11: every external call has a timeout. One attempt per job try,
    # so this is the whole budget for one attempt.
    meta_send_timeout_seconds: float = 10.0

    # phone_number_id -> tenant uuid, as JSON: {"100000000000001": "…uuid…"}.
    # Deliberately a str and not a dict[str, str] (plan assumption A1):
    # pydantic-settings JSON-decodes complex fields, and .env.example ships keys
    # with empty values, so `WHATSAPP_TENANT_MAP=` would raise at import and stop
    # the app booting from its own example file. ConfigTenantResolver parses it,
    # and a broken map becomes a loud dead letter instead of a dead process.
    whatsapp_tenant_map: str = ""
    # The single-tenant fallback: used with meta_phone_number_id when the map
    # above is blank, so local work needs no hand-written JSON.
    dev_tenant_id: str = ""

    # Which inbound message types get a reply. Comma-separated for the same
    # reason the map is a str. Everything is STORED; this only gates replying.
    whatsapp_reply_to_types: str = "text"

    # Hard rule 11: bounded retries with backoff, then a dead letter.
    # Deferrals with these values are 5s, 10s, 20s, 40s, then the 5th failure
    # dead-letters. The job raises arq's Retry itself — arq does not retry a
    # plain exception (plan conflict note C13).
    job_max_tries: int = 5
    job_backoff_base_seconds: float = 5.0
    job_backoff_max_seconds: float = 300.0
    # Must stay above openai_timeout_seconds + meta_send_timeout_seconds. A job
    # arq times out is finished as failed and never retried, and none of our exit
    # paths run: no dead letter, no lease release, and nothing re-enqueues the
    # event. tests/test_config.py pins the relation on the defaults, and the
    # worker warns at startup when a deployment breaks it.
    job_timeout_seconds: float = 60.0
    # Added to job_timeout_seconds to get the claim lease (plan note C3a). The
    # lease MUST outlive the job: if it expires while the job is still inside
    # the Meta call, a second worker claims the same event and sends the same
    # reply. 30s of slack covers a job that arq is in the middle of cancelling.
    job_lease_margin_seconds: float = 30.0

    # OpenAI (VS-005). Empty by default for the same reason as the Meta
    # credentials: the app must boot without them, and "not configured" means
    # every reply is agent_fallback_reply, with a dead letter naming the reason.
    openai_api_key: str = ""
    # No default, on purpose (CLAUDE.md: model names come from env vars, never
    # from code). Blank = permanent failure `openai_model_unset` + the fallback.
    # Named OPENAI_CHAT_MODEL, next to OPENAI_TRANSCRIBE_MODEL and
    # OPENAI_TTS_MODEL, which .env.example has carried since the first commit
    # (plan conflict C1, resolved by execution amendment A1). The reason code
    # names the concept rather than the key, so VS-008 and VS-009 reuse it.
    openai_chat_model: str = ""
    # Hard rule 11: one model call, enforced as a WALL-CLOCK deadline - the SDK's
    # own timeout applies per connection phase. job_timeout_seconds must exceed
    # this plus meta_send_timeout_seconds; tests/test_config.py pins it.
    openai_timeout_seconds: float = Field(default=30.0, gt=0)
    # Sent as max_completion_tokens. For reasoning models it also covers their
    # hidden reasoning tokens - hence the headroom over a short WhatsApp reply
    # (plan assumption A3). Only generated tokens are billed.
    openai_max_output_tokens: int = Field(default=1000, gt=0)

    # Agent Core (VS-005). Earlier messages of the conversation sent with each
    # reply, besides the one being answered (plan assumption A8).
    agent_history_messages: int = Field(default=20, ge=0)
    # Sent instead of an AI reply when one cannot be produced (requirement 4).
    agent_fallback_reply: str = "Sorry, we can't reply right now. The clinic will get back to you."

    @field_validator("meta_api_version", "meta_api_base_url", "agent_fallback_reply", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: Any, info: ValidationInfo) -> Any:
        """Treat an empty string as "not set" for the two settings with real defaults.

        `.env.example` ships `META_API_VERSION=` with no value, so a `.env` copied
        from it sets the variable to the empty string — which pydantic accepts as
        a perfectly good `str` and which would then build a send URL with a
        missing path segment. Every reply would fail with a 404 that looks like a
        bug in the client rather than a missing .env entry.

        AGENT_FALLBACK_REPLY is here for the same reason: a blank fallback can
        only mean "unset", because an empty message is not a reply - and sending
        "" to Meta is a permanent 4xx, so the one path that exists to answer a
        patient when everything else has failed would itself fail.

        Deliberately NOT applied to the credentials: an empty META_ACCESS_TOKEN
        must stay empty, because "not configured" has to mean "every send fails
        visibly", never "fall back to something".
        """
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value

    @property
    def claim_lease_seconds(self) -> float:
        """How long a worker owns a webhook_inbox row once it has claimed it.

        Derived rather than configured, so the invariant "the lease outlives the
        job" cannot be broken by setting one of two independent knobs.
        """
        return self.job_timeout_seconds + self.job_lease_margin_seconds

    @property
    def reply_to_types(self) -> frozenset[str]:
        """The parsed WHATSAPP_REPLY_TO_TYPES filter.

        Lower-cased and stripped, because Meta's `type` values are lower case and
        because a stray space in .env must not silently stop every reply.
        """
        return frozenset(
            part.strip().lower() for part in self.whatsapp_reply_to_types.split(",") if part.strip()
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, building them on first use."""
    return Settings()
