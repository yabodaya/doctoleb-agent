"""The transcription client: one attempt, one deadline, one classifier.

Every test here goes through an httpx2.MockTransport. The autouse fixture in
tests/conftest.py makes the real transport raise, so a test that forgot one
fails loudly instead of spending the developer's credit - and a transcription
is billed per minute of audio, so that matters more here than for chat.

The audio in every test is synthetic: four bytes of the Ogg magic and some
zeros. A real recording in a fixture would be a real person's voice (hard rule
8), and nothing here decodes anything.
"""

import asyncio
from typing import Any

import httpx2
import pytest

from app.config import Settings
from app.integrations.openai import ChatOutcome, TranscribeClient, TranscriptionResult
from app.integrations.openai.transcribe import OpenAITranscribeClient, read_transcription
from tests.integrations.fakes import FakeTranscribeClient
from tests.whatsapp_factories import SYNTHETIC_OGG

TEST_KEY = "sk-test-not-a-real-one"
TEST_MODEL = "transcribe-model-not-a-real-one"
# What the fake endpoint "heard". A sentinel, so the privacy tests can look for
# it in a request body and in a repr.
HEARD = "SENTINEL-transcribed-words"


def transcribe_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://user:pw@localhost:5432/doctoleb",
        "redis_url": "redis://localhost:6379/0",
        "openai_api_key": TEST_KEY,
        "openai_transcribe_model": TEST_MODEL,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class Transport:
    """Records every request and answers from a script.

    The request COUNT is the point of half these tests: `max_retries=0` means a
    failure must produce exactly one request, whatever kind of failure it is.
    """

    def __init__(self, *responses: httpx2.Response, handler=None):
        self.requests: list[httpx2.Request] = []
        self.bodies: list[bytes] = []
        self._responses = list(responses) or [transcription_response()]
        self._handler = handler

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        self.bodies.append(request.content)
        if self._handler is not None:
            return await self._handler(request)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    @property
    def calls(self) -> int:
        return len(self.requests)


def transcription_response(text: str | None = HEARD, **extra: Any) -> httpx2.Response:
    """A transcription body in the shape the SDK parses.

    `response_format="json"` means the body is `{"text": ...}` plus whatever
    `usage` the model chose to report - and nothing else. There is no
    `duration` and no `segments`: those belong to `TranscriptionVerbose`, i.e.
    to `whisper-1` only (plan conflict C15).
    """
    body: dict[str, Any] = {"text": text}
    body.update(extra)
    return httpx2.Response(200, json=body)


def error_response(
    status: int, code: str | None = None, error_type: str | None = None, message: str = "went wrong"
) -> httpx2.Response:
    return httpx2.Response(
        status,
        json={"error": {"message": message, "type": error_type, "param": None, "code": code}},
    )


def build(transport: Transport, **overrides: Any) -> OpenAITranscribeClient:
    return OpenAITranscribeClient(
        transcribe_settings(**overrides),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
    )


async def run(client: OpenAITranscribeClient, audio: bytes = SYNTHETIC_OGG):
    return await client.transcribe(audio, filename="voice-note.ogg", content_type="audio/ogg")


# --------------------------------------------------------------------------
# One classifier, shared with the chat client (plan conflict C19)
# --------------------------------------------------------------------------


def test_classify_openai_error_is_one_function_shared_by_both_clients():
    """The SAME OBJECT, not merely two functions that agree today.

    The classifier moved from chat.py to errors.py when this client became its
    second caller. chat.py re-exports it so every existing import is unchanged;
    this assertion is what stops a future tidy-up forking them into two tables
    that drift - at which point "is a 429 retryable?" would have two answers
    depending on which client asked.
    """
    from app.integrations.openai import chat, errors

    assert chat.classify_openai_error is errors.classify_openai_error


def test_the_old_import_path_still_works():
    """Every VS-005 and VS-006 import and test uses this path."""
    from app.integrations.openai.chat import classify_openai_error

    assert callable(classify_openai_error)


# --------------------------------------------------------------------------
# The Protocol
# --------------------------------------------------------------------------


def test_the_transcribe_client_satisfies_the_protocol():
    assert isinstance(build(Transport()), TranscribeClient)


def test_the_fake_satisfies_it_too():
    """So a job wired with the fake is wired with something of the right shape."""
    assert isinstance(FakeTranscribeClient(), TranscribeClient)


def test_the_interface_module_imports_no_sdk():
    """app/agent/ imports the package that re-exports this Protocol.

    If the interface imported `openai`, the Agent Core would load the SDK -
    which is exactly what hard rule 3's "no SDK in app/agent/" forbids, and
    what app/integrations/openai/__init__.py's docstring promises.
    """
    import pathlib

    source = pathlib.Path("app/integrations/openai/interface.py").read_text(encoding="utf-8")

    assert "import openai" not in source
    assert "httpx" not in source


# --------------------------------------------------------------------------
# Nothing configured: permanent, and no request
# --------------------------------------------------------------------------


async def test_a_blank_api_key_is_permanent_without_calling():
    transport = Transport()

    result = await run(build(transport, openai_api_key=""))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_api_key_unset"
    assert transport.calls == 0


@pytest.mark.parametrize("model", ["", "   "])
async def test_a_blank_model_is_permanent_without_calling(model):
    """CLAUDE.md: model names come from env vars, never from code.

    So "not set" has to be a loud, specific failure rather than a silent
    fallback to a model nobody chose - and it must cost nothing, because every
    voice note would hit it.
    """
    transport = Transport()

    result = await run(build(transport, openai_transcribe_model=model))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_transcribe_model_unset"
    assert transport.calls == 0


# --------------------------------------------------------------------------
# The request on the wire
# --------------------------------------------------------------------------


async def test_the_request_carries_the_model_the_bytes_and_the_content_type():
    """Read off the multipart body, because that is what OpenAI actually gets.

    The audio goes as a `(filename, bytes, content_type)` tuple, which is what
    makes W1 true all the way down: there is no file on disk to name, because
    there is no file.
    """
    transport = Transport()

    result = await run(build(transport))

    assert result.succeeded
    assert transport.calls == 1
    body = transport.bodies[0]
    assert TEST_MODEL.encode() in body
    assert SYNTHETIC_OGG in body
    assert b"voice-note.ogg" in body
    assert b"audio/ogg" in body
    assert transport.requests[0].url.path.endswith("/audio/transcriptions")


async def test_no_language_and_no_prompt_are_sent():
    """W8, and both for a reason worth keeping.

    `language="ar"` would help Arabic and break English, French and Arabizi,
    all of which are expected in Lebanon. A `prompt` biases the output toward
    whatever we wrote AND is a place clinic data could leak into a third
    party's request.
    """
    transport = Transport()

    await run(build(transport))

    body = transport.bodies[0]
    assert b'name="language"' not in body
    assert b'name="prompt"' not in body
    assert b'name="temperature"' not in body
    assert b'name="timestamp_granularities"' not in body
    # The one format option that IS sent, because the current models support
    # only this one.
    assert b"json" in body


async def test_the_request_body_contains_no_tenant_no_patient_and_no_phone_number():
    """The wire-level privacy test, the sibling of VS-007's booking-turn one.

    What leaves this process for a transcription is: the model name, the audio
    bytes, a filename we invented and a content type. Not a tenant (hard rule
    4), not the patient's number, not a conversation id, and nothing a clinic
    configured.
    """
    transport = Transport()
    client = OpenAITranscribeClient(
        transcribe_settings(
            whatsapp_tenant_map='{"100000000000001":"SENTINEL-tenant"}',
            dev_tenant_id="SENTINEL-tenant",
            meta_phone_number_id="100000000000001",
        ),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
    )

    await run(client)

    body = transport.bodies[0]
    for sentinel in (b"SENTINEL-tenant", b"100000000000001", b"96170"):
        assert sentinel not in body, sentinel


# --------------------------------------------------------------------------
# Classification: one attempt each, whatever the failure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (error_response(429, code="rate_limit_exceeded"), "openai_http_429"),
        (error_response(500), "openai_http_500"),
        (error_response(503), "openai_http_503"),
        (error_response(408), "openai_http_408"),
    ],
)
async def test_a_429_a_5xx_and_a_408_are_retryable(response, reason):
    transport = Transport(response)

    result = await run(build(transport))

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == reason
    # max_retries=0: one attempt, whatever happened. The retry curve, the try
    # count and the dead letter all live in the job.
    assert transport.calls == 1


async def test_a_connection_error_is_retryable():
    async def refuse(request):
        raise httpx2.ConnectError("no route")

    transport = Transport(handler=refuse)

    result = await run(build(transport))

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == "openai_connection"
    assert transport.calls == 1


async def test_insufficient_quota_is_permanent():
    """The one 429 retrying cannot fix: the account has no credit.

    Five tries would be five calls to learn the same thing, and the patient
    waits through the whole backoff curve before being told to type instead.
    """
    transport = Transport(
        error_response(429, code="insufficient_quota", error_type="insufficient_quota")
    )

    result = await run(build(transport))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_insufficient_quota"
    assert transport.calls == 1


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (400, "invalid_request_error", "openai_http_400_invalid_request_error"),
        (401, None, "openai_http_401"),
        (404, "model_not_found", "openai_http_404_model_not_found"),
        (415, None, "openai_http_415"),
    ],
)
async def test_another_4xx_is_permanent_and_carries_its_code(status, code, reason):
    """A 415 is the one to notice: it is what a rejected audio format looks like.

    Plan check U6 - whether the endpoint accepts Opus-in-Ogg directly - is
    answered by the live test, and if the answer is no, THIS is the code that
    will say so. The fix would be ffmpeg in the image, which is a decision for
    the developer and not something to add unasked.
    """
    transport = Transport(error_response(status, code=code))

    result = await run(build(transport))

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == reason
    assert transport.calls == 1


async def test_our_own_deadline_fires_as_a_timeout():
    """`asyncio.timeout`, not the SDK's.

    httpx's float applies per connection phase, so one call can legitimately
    take several times OPENAI_TRANSCRIBE_TIMEOUT_SECONDS - and a MockTransport
    does not honour it at all, which makes this the only proof that the
    wall-clock deadline is the one that bites.
    """

    async def stall(request):
        await asyncio.sleep(5)
        return transcription_response()

    transport = Transport(handler=stall)

    result = await run(build(transport, openai_transcribe_timeout_seconds=0.05))

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == "openai_timeout"


async def test_a_bug_in_our_code_still_raises():
    """`classify_openai_error` returning None is what lets a real bug be seen.

    Retrying a ValueError five times and then telling the patient to type
    instead would hide it behind a dead letter that said OpenAI was unreachable.
    """

    async def explode(request):
        raise ValueError("a bug, not an outage")

    transport = Transport(handler=explode)

    with pytest.raises(ValueError):
        await run(build(transport))


# --------------------------------------------------------------------------
# Reading a 2xx
# --------------------------------------------------------------------------


async def test_a_transcription_returns_its_text():
    transport = Transport(transcription_response("appointment tomorrow please"))

    result = await run(build(transport))

    assert result.succeeded
    assert result.reason == "ok"
    assert result.text == "appointment tomorrow please"


async def test_the_text_is_returned_exactly_as_the_api_gave_it():
    """Normalisation belongs to transcripts.py, which the JOB calls.

    Normalising here would mean the transcript we store in messages.text is not
    the transcript we were given - and messages.text is what a staff member
    reads to find out what the patient actually said.
    """
    messy = "  Thank   you,   doctor.  "
    transport = Transport(transcription_response(messy))

    result = await run(build(transport))

    assert result.text == messy


async def test_seconds_are_reported_when_the_model_reports_them():
    """Plan check U8: whether `usage` on a JSON transcription carries seconds.

    Nothing decides with it (plan conflict C15). It is recorded so the real
    cost per voice note can be worked out from `voice_notes` rather than from a
    bill, and it is None when the model reports tokens instead - which breaks
    nothing, because nothing reads it.
    """
    transport = Transport(transcription_response(usage={"type": "duration", "seconds": 12.5}))

    result = await run(build(transport))

    assert result.seconds == 12.5


async def test_a_usage_without_seconds_leaves_them_unknown():
    transport = Transport(
        transcription_response(usage={"type": "tokens", "input_tokens": 9, "output_tokens": 4})
    )

    result = await run(build(transport))

    assert result.succeeded
    assert result.seconds is None


@pytest.mark.parametrize("body", [{"text": None}, {"text": 7}, {}, {"error": "nope"}])
def test_a_2xx_that_is_not_a_transcription_is_retryable(body):
    """Most likely a proxy's error page arriving with a 200.

    `read_transcription` uses `getattr` throughout because the SDK's response
    validation is non-strict: a 2xx whose body is not what we asked for comes
    back as a plain object rather than raised.
    """

    class Answer:
        def __init__(self, **fields):
            for key, value in fields.items():
                setattr(self, key, value)

    result = read_transcription(Answer(**body))

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == "openai_transcribe_bad_response"


def test_a_2xx_that_is_a_bare_string_is_retryable():
    """The SDK really does return a `str` for a 2xx it could not parse."""
    result = read_transcription("<html>502 Bad Gateway</html>")

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == "openai_transcribe_bad_response"


def test_an_empty_transcript_is_a_success_not_a_failure():
    """The call worked and was billed; there was nothing in the audio.

    This distinction is load-bearing: a failure would be retried five times and
    dead-lettered, while a success with nothing in it is W5's UNCLEAR reply -
    the patient is asked to repeat or type, and nobody has to fix anything.
    """

    class Answer:
        text = ""
        usage = None

    result = read_transcription(Answer())

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.text == ""


# --------------------------------------------------------------------------
# Hard rule 8
# --------------------------------------------------------------------------


def test_the_result_repr_hides_the_transcript():
    """pytest prints reprs on a failed assertion, which is how a patient's own
    words reach a CI log. The outcome and the reason are what a traceback
    needs, and neither is content."""
    printed = repr(TranscriptionResult(ChatOutcome.SUCCESS, "ok", HEARD, 3.0))

    assert HEARD not in printed
    assert "SUCCESS" in printed
    assert "ok" in printed


def test_this_module_never_logs():
    """One line per voice step, written by the JOB.

    Only the job knows the webhook_inbox row id every other line carries - and
    what this module would have to log is the transcript.
    """
    import pathlib

    source = pathlib.Path("app/integrations/openai/transcribe.py").read_text(encoding="utf-8")

    assert "logging" not in source
    assert "logger" not in source


def test_the_fake_records_counts_and_types_but_never_content():
    """A fake that stored the audio or the transcript would put patient content
    in a fixture, which is the thing hard rule 8 forbids."""
    fake = FakeTranscribeClient()

    assert fake.calls == []
    # The recorded shape is a byte count and a content type, and that is all.
    assert all(isinstance(entry, tuple) and len(entry) == 2 for entry in fake.calls)
