from pathlib import Path

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


def _base_settings(**overrides):
    """Settings with the two required DSNs filled in and no .env involved.

    _env_file=None on every call: these tests assert DEFAULTS, and reading the
    developer's real .env would make them pass or fail depending on what that
    file happens to contain.
    """
    values = {
        "database_url": "postgresql+asyncpg://user:pw@db:5432/doctoleb",
        "redis_url": "redis://cache:6379/1",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_the_meta_send_settings_default_to_empty_or_safe_values(monkeypatch):
    """Assumption A3 again, now for the send side.

    An empty token is not a permissive value: it makes every send fail with a
    permanent, visible error, which is the correct meaning of "not configured".
    The two non-credential settings have real defaults, because there is nothing
    secret about an API version or a hostname.
    """
    for key in ("META_ACCESS_TOKEN", "META_PHONE_NUMBER_ID", "META_API_VERSION"):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.meta_access_token == ""
    assert settings.meta_phone_number_id == ""
    assert settings.meta_api_version != ""
    assert "graph.facebook.com" in settings.meta_api_base_url
    assert settings.meta_send_timeout_seconds > 0


def test_a_blank_meta_api_version_falls_back_to_the_default(monkeypatch):
    """`.env.example` ships META_API_VERSION= with no value, so a .env copied
    from it sets the variable to the empty string rather than leaving it unset.

    Empty here can only mean "unset": there is no such thing as a Graph API
    version "". Without this, the send URL would be built with a missing path
    segment and every reply would fail with a 404 that looks like a bug in the
    client rather than a missing .env entry.
    """
    monkeypatch.delenv("META_API_VERSION", raising=False)
    default = _base_settings().meta_api_version

    assert _base_settings(meta_api_version="").meta_api_version == default
    assert _base_settings(meta_api_base_url="").meta_api_base_url != ""


def test_the_tenant_map_is_a_plain_string_and_defaults_to_empty(monkeypatch):
    """Assumption A1, asserted on the TYPE and not only the value.

    A dict[str, str] field would be JSON-decoded by pydantic-settings, and
    `WHATSAPP_TENANT_MAP=` in .env.example would then raise at import and stop
    the app booting from its own example file. Parsing belongs to
    ConfigTenantResolver, where a broken map becomes a loud dead letter instead
    of a dead process.
    """
    monkeypatch.delenv("WHATSAPP_TENANT_MAP", raising=False)
    settings = _base_settings()

    assert Settings.model_fields["whatsapp_tenant_map"].annotation is str
    assert settings.whatsapp_tenant_map == ""
    assert settings.dev_tenant_id == ""


def test_an_empty_tenant_map_does_not_stop_the_app_from_starting():
    assert _base_settings(whatsapp_tenant_map="").whatsapp_tenant_map == ""


def test_a_malformed_tenant_map_does_not_stop_the_app_from_starting():
    """The failure belongs to the resolver, not to boot (assumption A1).

    /health must stay up when the map has a typo in it, and the typo must be
    visible as a dead letter rather than as a container that will not start.
    """
    broken = "{not json"

    assert _base_settings(whatsapp_tenant_map=broken).whatsapp_tenant_map == broken


def test_the_reply_type_filter_defaults_to_text_only(monkeypatch):
    monkeypatch.delenv("WHATSAPP_REPLY_TO_TYPES", raising=False)

    assert _base_settings().whatsapp_reply_to_types == "text"


def test_the_retry_knobs_have_the_documented_defaults(monkeypatch):
    """Hard rule 11. Pinned so a change to the backoff curve is deliberate.

    With these values the deferrals are 5s, 10s, 20s, 40s and the fifth failure
    dead-letters — about 75 seconds of patience, which is what the plan
    documents and what the backoff tests assert.
    """
    for key in ("JOB_MAX_TRIES", "JOB_BACKOFF_BASE_SECONDS", "JOB_BACKOFF_MAX_SECONDS"):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.job_max_tries == 5
    assert settings.job_backoff_base_seconds == 5.0
    assert settings.job_backoff_max_seconds == 300.0


def test_the_job_timeout_exceeds_the_turn_budget_and_the_meta_send_together(monkeypatch):
    """A real constraint, not a tidy coincidence. VS-008's arithmetic (plan 5.1):

        MAX_MODEL_CALLS (a constant, not a setting)   6   model calls per turn
        OPENAI_TIMEOUT_SECONDS                       30   ONE model call
        META_MEDIA_TIMEOUT_SECONDS                   10   ONE media call, x2
        OPENAI_TRANSCRIBE_TIMEOUT_SECONDS            20   ONE transcription
        AGENT_TURN_TIMEOUT_SECONDS                   45   the WHOLE tool loop
        META_SEND_TIMEOUT_SECONDS                    10   the one Meta send
        ---------------------------------------------------------------------
        voice step   = 2 x 10 + 20                   40
        network worst case = 40 + 45 + 10            95
        JOB_TIMEOUT_SECONDS                         140   must be > 95
        JOB_LEASE_MARGIN_SECONDS                     30
        claim lease = 140 + 30                      170   > 140

    VS-008 added the voice step - a media lookup, a download and a
    transcription, all outside the turn budget because the turn has not started
    yet when they run - so JOB_TIMEOUT_SECONDS rose from 90 to 140 (plan
    conflict C5). At 90 the worst case was 95, i.e. ABOVE the job timeout.

    OPENAI_TIMEOUT_SECONDS stays OUT of this relation (VS-006): every model
    call runs INSIDE the turn budget, so the loop, not the per-call deadline,
    is what the job has to cover.

    Why it matters: an arq job that exceeds its timeout is finished as failed
    and never retried, and none of our exit paths run - no dead letter, no lease
    release, nothing re-enqueues the event (verified against arq 0.28). The
    event is simply stranded until its lease expires.

    Every budget here is a wall-clock deadline rather than an httpx timeout,
    because an httpx timeout is per connection phase and one call can
    legitimately take several times its value.
    """
    for key in (
        "JOB_TIMEOUT_SECONDS",
        "META_SEND_TIMEOUT_SECONDS",
        "AGENT_TURN_TIMEOUT_SECONDS",
        "META_MEDIA_TIMEOUT_SECONDS",
        "OPENAI_TRANSCRIBE_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.job_timeout_seconds > (
        settings.voice_note_budget_seconds
        + settings.agent_turn_timeout_seconds
        + settings.meta_send_timeout_seconds
    )


def test_the_turn_budget_and_the_job_timeout_have_their_documented_defaults(monkeypatch):
    """Decision D4 with Q4's numbers, pinned so a change is a visible decision.

    45 seconds is what a patient waits at worst before the fallback, and it did
    NOT change in VS-008: the voice step sits outside the turn budget, so a
    voice turn gets the same model budget as a text one (and V15's
    MIN_SECONDS_FOR_A_BOOKING_CHANGE floor is measured against this same 45).

    JOB_TIMEOUT_SECONDS moved 90 -> 140 in VS-008 (plan conflict C5), because
    the job now also covers the voice step's 40 seconds: 40 + 45 + 10 = 95,
    which was ABOVE the old 90. 140 leaves 45 seconds for the job's five or six
    short transactions (T0, T1, T1a, T1b, T2, and VS-007's T1r on the retry
    path).
    """
    for key in ("AGENT_TURN_TIMEOUT_SECONDS", "JOB_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.agent_turn_timeout_seconds == 45.0
    assert settings.job_timeout_seconds == 140.0


def test_the_claim_lease_outlives_the_job_timeout(monkeypatch):
    """Plan note C3a, pinned as a test.

    The lease is what stops two concurrent runs of one event both sending the
    reply. A lease that expires while the job is still running — and worse, while
    it is inside the Meta call — hands the row to a second worker and produces
    exactly the duplicate the lease exists to prevent.

    The margin is asserted positive as well, so JOB_LEASE_MARGIN_SECONDS=0
    cannot quietly re-open it.
    """
    for key in ("JOB_TIMEOUT_SECONDS", "JOB_LEASE_MARGIN_SECONDS"):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.job_lease_margin_seconds > 0
    assert settings.claim_lease_seconds > settings.job_timeout_seconds
    assert settings.claim_lease_seconds == (
        settings.job_timeout_seconds + settings.job_lease_margin_seconds
    )


def test_every_new_key_is_present_in_env_example():
    """`.env.example` is the only documentation of what to set.

    A setting that exists in code and not in the example file is a setting the
    next person deploying this will not know about. Checked by name, never by
    value — the example file holds no secrets and this test must not encourage
    putting any there.
    """
    # Relative to the repository root, which is where pytest runs from
    # (testpaths = ["tests"] in pyproject.toml).
    example = Path(".env.example").read_text(encoding="utf-8")

    for key in (
        "META_API_BASE_URL",
        "META_SEND_TIMEOUT_SECONDS",
        "WHATSAPP_TENANT_MAP",
        "WHATSAPP_REPLY_TO_TYPES",
        "JOB_MAX_TRIES",
        "JOB_BACKOFF_BASE_SECONDS",
        "JOB_BACKOFF_MAX_SECONDS",
        "JOB_TIMEOUT_SECONDS",
        "JOB_LEASE_MARGIN_SECONDS",
        "OPENAI_API_KEY",
        "OPENAI_CHAT_MODEL",
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_MAX_OUTPUT_TOKENS",
        "AGENT_HISTORY_MESSAGES",
        "AGENT_FALLBACK_REPLY",
        # VS-008. OPENAI_TRANSCRIBE_MODEL has been in the example file since the
        # first commit; this is the first slice in which it is a real setting,
        # so it joins the parity check. The other five are genuinely new keys,
        # which is why Task A1 rebuilds the image: .env.example is COPIED into
        # it, and inside the container this test reads the baked copy.
        "OPENAI_TRANSCRIBE_MODEL",
        "OPENAI_TRANSCRIBE_TIMEOUT_SECONDS",
        "META_MEDIA_TIMEOUT_SECONDS",
        "VOICE_NOTE_MAX_BYTES",
        "VOICE_NOTE_UNCLEAR_REPLY",
        "VOICE_NOTE_FAILED_REPLY",
    ):
        assert f"\n{key}=" in example, key


# Every key .env.example names, so a test can clear all of them from the process
# environment before loading that file (see the boot test below).
_ENV_EXAMPLE_KEYS = tuple(
    line.split("=", 1)[0]
    for line in Path(".env.example").read_text(encoding="utf-8").splitlines()
    if "=" in line and not line.startswith("#")
)


def test_the_openai_settings_default_to_unset_and_do_not_block_startup(monkeypatch):
    """Requirement 7, and VS-003's assumption A3 for the same reason.

    The app must boot with no OpenAI account at all: a developer who has not
    been given a key yet still has to be able to run `docker compose up` and
    `pytest`. "Not configured" then means every reply is AGENT_FALLBACK_REPLY
    with a dead letter naming the reason, never a process that will not start.
    """
    for key in ("OPENAI_API_KEY", "OPENAI_CHAT_MODEL"):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.openai_api_key == ""
    assert settings.openai_chat_model == ""


def test_the_openai_model_has_no_default_in_code():
    """CLAUDE.md: "model names come from env vars, never hardcoded".

    Asserted on the FIELD's default, not on a built instance, so a default
    smuggled into the class cannot be hidden by a monkeypatched environment.
    A model name in source is a name that goes stale silently and bills the
    clinic for a model nobody chose. Requirement 7.
    """
    assert Settings.model_fields["openai_chat_model"].default == ""


def test_the_agent_settings_have_the_documented_defaults(monkeypatch):
    """Plan assumption A3, pinned so a change is a visible decision.

    1000 output tokens looks generous for a short WhatsApp reply and is
    deliberate: max_completion_tokens also covers a reasoning model's hidden
    reasoning tokens, so a tight cap turns into truncated or empty replies -
    which become fallbacks. Only generated tokens are billed.
    """
    for key in (
        "OPENAI_TIMEOUT_SECONDS",
        "OPENAI_MAX_OUTPUT_TOKENS",
        "AGENT_HISTORY_MESSAGES",
        "AGENT_FALLBACK_REPLY",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.openai_timeout_seconds == 30.0
    assert settings.openai_max_output_tokens == 1000
    assert settings.agent_history_messages == 20
    assert settings.agent_fallback_reply == (
        "Sorry, we can't reply right now. The clinic will get back to you."
    )


def test_a_blank_fallback_reply_falls_back_to_the_default(monkeypatch):
    """A blank fallback can only mean "unset": an empty message is not a reply.

    Sending "" to Meta is a permanent 4xx, so the one path that exists to
    answer a patient when everything else has failed would itself fail.
    """
    monkeypatch.delenv("AGENT_FALLBACK_REPLY", raising=False)
    default = _base_settings().agent_fallback_reply

    assert _base_settings(agent_fallback_reply="").agent_fallback_reply == default


def test_the_app_boots_from_a_verbatim_copy_of_env_example(monkeypatch):
    """Plan conflict C5, and the most important test in this task.

    `.env.example` lists every key with an empty value, and `docker compose`'s
    `env_file:` turns `KEY=` into an empty environment variable. pydantic
    rejects "" for a float or an int, so before env_ignore_empty=True a `.env`
    copied from the example file stopped BOTH the api and the worker from
    booting - on VS-004's own numeric keys, before VS-005 added six more.

    Every key the file names is deleted from the process environment first, so
    the developer's own shell cannot mask the file's blank values.
    """
    for key in _ENV_EXAMPLE_KEYS:
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=Path(".env.example"))

    assert settings.meta_send_timeout_seconds == (
        Settings.model_fields["meta_send_timeout_seconds"].default
    )
    assert settings.job_max_tries == Settings.model_fields["job_max_tries"].default
    assert settings.openai_timeout_seconds == (
        Settings.model_fields["openai_timeout_seconds"].default
    )
    assert settings.agent_history_messages == (
        Settings.model_fields["agent_history_messages"].default
    )
    # VS-006's key, added blank to the example file like every other one.
    assert settings.agent_turn_timeout_seconds == (
        Settings.model_fields["agent_turn_timeout_seconds"].default
    )
    # VS-008's five new keys, all blank in the example file. The two replies are
    # the ones that matter most here: a blank VOICE_NOTE_FAILED_REPLY would
    # make the one path that exists to tell a patient we could not hear them
    # fail with a permanent 4xx from Meta.
    assert settings.meta_media_timeout_seconds == (
        Settings.model_fields["meta_media_timeout_seconds"].default
    )
    assert settings.openai_transcribe_timeout_seconds == (
        Settings.model_fields["openai_transcribe_timeout_seconds"].default
    )
    assert settings.voice_note_max_bytes == Settings.model_fields["voice_note_max_bytes"].default
    assert settings.voice_note_unclear_reply == (
        Settings.model_fields["voice_note_unclear_reply"].default
    )
    assert settings.voice_note_failed_reply == (
        Settings.model_fields["voice_note_failed_reply"].default
    )


def test_a_blank_numeric_value_means_unset(monkeypatch):
    """What `docker compose` does with `OPENAI_TIMEOUT_SECONDS=` in an env_file.

    The same trap as the test above, reached through the process environment
    rather than through a dotenv file - two different code paths in
    pydantic-settings, and only one of them is exercised by the example file.
    """
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "")

    assert _base_settings().openai_timeout_seconds == 30.0


def test_a_blank_credential_stays_blank(monkeypatch):
    """Ignoring empties must never turn "not configured" into "configured".

    It cannot, and this test is why that is safe: a credential's default IS "",
    so falling back to the default for a blank value is a no-op. The danger
    would be a credential with a non-empty default, and there is none.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "")

    assert _base_settings().openai_api_key == ""


@pytest.mark.parametrize(
    "override",
    [
        {"openai_timeout_seconds": 0},
        {"agent_turn_timeout_seconds": 0},
        {"openai_max_output_tokens": 0},
        {"agent_history_messages": -1},
        # VS-008's three numeric knobs, for exactly the same reason. A zero
        # media timeout cancels the lookup before it starts, so every voice
        # note would be answered with VOICE_NOTE_FAILED_REPLY; a zero or
        # negative byte cap refuses every download, including a two-second one.
        {"meta_media_timeout_seconds": 0},
        {"openai_transcribe_timeout_seconds": 0},
        {"voice_note_max_bytes": 0},
        {"voice_note_max_bytes": -1},
    ],
)
def test_nonsense_agent_numbers_are_refused_at_boot(override):
    """Plan assumption A13. A MISSING value boots; a NONSENSICAL one does not.

    This is the line pydantic already draws for a non-numeric value. A zero
    timeout means every model call is cancelled before it starts, and a
    negative history is `LIMIT -1`, which is a SQL error inside a job rather
    than a message at boot. Both are typos, and a typo should be loud where it
    is made.
    """
    with pytest.raises(ValidationError):
        _base_settings(**override)


# --------------------------------------------------------------------------
# VS-008: the media and transcription budget, and the two code-owned replies
# --------------------------------------------------------------------------

VOICE_NOTE_KEYS = (
    "OPENAI_TRANSCRIBE_MODEL",
    "OPENAI_TRANSCRIBE_TIMEOUT_SECONDS",
    "META_MEDIA_TIMEOUT_SECONDS",
    "VOICE_NOTE_MAX_BYTES",
    "VOICE_NOTE_UNCLEAR_REPLY",
    "VOICE_NOTE_FAILED_REPLY",
)


def test_the_transcribe_model_has_no_default_and_does_not_block_startup(monkeypatch):
    """CLAUDE.md: "model names come from env vars, never hardcoded".

    The same rule and the same shape as OPENAI_CHAT_MODEL, and asserted on the
    FIELD's default so a default smuggled into the class cannot be hidden by a
    monkeypatched environment. Blank must boot: a developer with no OpenAI
    account still has to be able to run the worker. What blank costs is one
    permanent `openai_transcribe_model_unset` per voice note and the patient
    being told to type instead - never a silent fallback to some model nobody
    chose, and never a process that will not start.
    """
    monkeypatch.delenv("OPENAI_TRANSCRIBE_MODEL", raising=False)

    assert Settings.model_fields["openai_transcribe_model"].default == ""
    assert _base_settings().openai_transcribe_model == ""


def test_the_voice_timeouts_have_the_documented_defaults(monkeypatch):
    """Plan section 5.1, pinned so a change to the voice budget is deliberate.

    10 seconds for ONE media call, applied separately to the lookup and to the
    download, so the pair is at most 20. 20 seconds for ONE transcription,
    which is a guess at a two-minute note on a bad connection and is the
    number Task B4's real latencies are measured against.

    Both are wall-clock deadlines (`asyncio.timeout`), not httpx timeouts: an
    httpx timeout applies per connection phase, so one call can legitimately
    take several times its value.
    """
    for key in ("META_MEDIA_TIMEOUT_SECONDS", "OPENAI_TRANSCRIBE_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.meta_media_timeout_seconds == 10.0
    assert settings.openai_transcribe_timeout_seconds == 20.0
    assert settings.meta_media_timeout_seconds > 0
    assert settings.openai_transcribe_timeout_seconds > 0


def test_the_voice_note_size_cap_has_the_documented_default(monkeypatch):
    """16 MiB, which is Meta's OWN documented maximum for an audio message.

    So this is not a policy we invented: a real voice note cannot exceed it,
    and the audio endpoint's own request limit (25 MB) is higher still, which
    makes Meta's number the binding one.

    Its real job is not the honest case at all. It is the only thing standing
    between a forged payload - or a response that lies about Content-Length -
    and an unbounded allocation inside the worker (plan risk R5), which is why
    it is enforced three times: against the declared file_size before a byte is
    fetched, against the running total while streaming, and by abandoning the
    stream the moment the total is passed.
    """
    monkeypatch.delenv("VOICE_NOTE_MAX_BYTES", raising=False)

    settings = _base_settings()

    assert settings.voice_note_max_bytes == 16 * 1024 * 1024
    assert settings.voice_note_max_bytes > 0


def test_the_voice_replies_are_pinned_in_arabic_and_english(monkeypatch):
    """The two texts a patient gets when the audio side failed (W5, W6).

    Pinned verbatim, because they are the only replies in the system whose
    wording is OURS rather than the model's, and because each has one job:

      * the UNCLEAR reply asks for an ACTION - record it again, or type it -
        because that is the patient's next step, and it claims nothing about
        the clinic;
      * the FAILED reply says to type the message or send a shorter one,
        because "the clinic will get back to you" (AGENT_FALLBACK_REPLY) is
        simply untrue of a voice note nobody will ever hear.

    Arabic AND English in one message, because the patient's language is
    unknown until something has been transcribed - and on these two paths
    nothing was. No French: it would make the message long on a phone screen,
    and it is a one-setting change if the clinic wants it.

    No digits in either, asserted below for the same reason
    test_the_prompt_states_no_digits exists: a number in a fixed string is a
    number nobody can keep correct.
    """
    for key in ("VOICE_NOTE_UNCLEAR_REPLY", "VOICE_NOTE_FAILED_REPLY"):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.voice_note_unclear_reply == (
        "Sorry, I couldn't hear that voice note clearly. Could you record it "
        "again or type your message? / عذراً، لم أتمكن من سماع الرسالة الصوتية "
        "بوضوح. هل يمكنك تسجيلها مرة أخرى أو كتابة رسالتك؟"
    )
    assert settings.voice_note_failed_reply == (
        "Sorry, I couldn't listen to that voice note. Please type your "
        "message, or send a shorter voice note. / عذراً، لم أتمكن من الاستماع "
        "إلى الرسالة الصوتية. يرجى كتابة رسالتك أو إرسال رسالة صوتية أقصر."
    )

    for reply in (settings.voice_note_unclear_reply, settings.voice_note_failed_reply):
        # Arabic: any character in the Arabic block. English: an ASCII letter.
        assert any("؀" <= character <= "ۿ" for character in reply)
        assert any("a" <= character.lower() <= "z" for character in reply)
        assert not any(character.isdigit() for character in reply)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
@pytest.mark.parametrize("field", ["voice_note_unclear_reply", "voice_note_failed_reply"])
def test_a_blank_voice_reply_means_the_default(field, blank, monkeypatch):
    """`_blank_means_unset`, for exactly the reason AGENT_FALLBACK_REPLY is there.

    `.env.example` ships `VOICE_NOTE_UNCLEAR_REPLY=` with no value, so a `.env`
    copied from it sets the variable to the empty string - and sending "" to
    Meta is a permanent 4xx. The one path that exists to tell a patient we
    could not hear them would itself fail, and the patient would get nothing at
    all while the job dead-lettered.

    Whitespace is covered too, because `env_ignore_empty` drops "" but keeps
    "   ", which is the same mistake with a space in it.
    """
    for key in VOICE_NOTE_KEYS:
        monkeypatch.delenv(key, raising=False)
    default = getattr(_base_settings(), field)

    assert getattr(_base_settings(**{field: blank}), field) == default
    assert default != ""


@pytest.mark.parametrize("field", ["voice_note_unclear_reply", "voice_note_failed_reply"])
def test_a_configured_voice_reply_is_kept_verbatim(field):
    """The other half of the blank rule: a real value is never touched.

    Not even stripped. A clinic that writes its own wording - in Arabic, in
    French, with its own punctuation - gets exactly what it wrote, because
    `_blank_means_unset` only replaces a value that is blank after stripping.
    """
    written = "  نص العيادة / the clinic's own words  "

    assert getattr(_base_settings(**{field: written}), field) == written


def test_the_voice_budget_is_derived_from_its_three_parts(monkeypatch):
    """Derived, like claim_lease_seconds, so the invariant cannot be broken by
    setting one of three independent knobs.

    A reader - and `startup_warnings()` - should not have to add three numbers
    up by hand to see whether JOB_TIMEOUT_SECONDS still covers the voice step.
    Two media calls, because the lookup and the download each get the whole
    META_MEDIA_TIMEOUT_SECONDS.
    """
    for key in VOICE_NOTE_KEYS:
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()

    assert settings.voice_note_budget_seconds == (
        2 * settings.meta_media_timeout_seconds + settings.openai_transcribe_timeout_seconds
    )
    assert settings.voice_note_budget_seconds == 40.0
    # And it MOVES when any one of them does, so it can never go stale.
    assert _base_settings(meta_media_timeout_seconds=30).voice_note_budget_seconds == 80.0
    assert _base_settings(openai_transcribe_timeout_seconds=60).voice_note_budget_seconds == 80.0


def test_the_job_timeout_covers_the_voice_step_the_turn_and_the_send(monkeypatch):
    """The whole relation of plan section 5.1, on the defaults: 40 + 45 + 10 = 95 < 140.

    The 45 seconds left over are for the job's own short transactions - T0, T1,
    T1a, T1b, T2, and VS-007's T1r on the retry path. A job arq times out is
    finished as failed with none of our exit paths run: no dead letter, no
    lease release, and the event stranded until its lease expires. That is why
    this is a test and a startup warning and not a comment.
    """
    for key in (
        "JOB_TIMEOUT_SECONDS",
        "META_SEND_TIMEOUT_SECONDS",
        "AGENT_TURN_TIMEOUT_SECONDS",
        *VOICE_NOTE_KEYS,
    ):
        monkeypatch.delenv(key, raising=False)

    settings = _base_settings()
    budget = (
        settings.voice_note_budget_seconds
        + settings.agent_turn_timeout_seconds
        + settings.meta_send_timeout_seconds
    )

    assert budget == 95.0
    assert settings.job_timeout_seconds == 140.0
    assert settings.job_timeout_seconds > budget
    # And the lease still outlives the bigger job: 140 + 30 = 170.
    assert settings.claim_lease_seconds == 170.0


@pytest.mark.parametrize(
    "override",
    [
        {"meta_media_timeout_seconds": 60},
        {"openai_transcribe_timeout_seconds": 100},
        {"agent_turn_timeout_seconds": 100},
        {"meta_send_timeout_seconds": 100},
    ],
)
def test_raising_any_one_budget_knob_can_break_the_job_timeout(override):
    """Why the relation is checked at startup rather than only pinned here.

    Four independent knobs feed it, and raising ANY of them past the job
    timeout strands events - silently, because nothing fails until a real voice
    note arrives and takes too long. These are the four cases
    `startup_warnings()` has to catch, and the worker test asserts it does.
    """
    settings = _base_settings(**override)
    budget = (
        settings.voice_note_budget_seconds
        + settings.agent_turn_timeout_seconds
        + settings.meta_send_timeout_seconds
    )

    assert settings.job_timeout_seconds <= budget
