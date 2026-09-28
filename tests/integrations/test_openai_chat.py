"""The OpenAI client: one attempt, one deadline, one classifier.

Every test here goes through an httpx2.MockTransport. The autouse fixture in
tests/conftest.py makes the real transport raise, so a test that forgot one
fails loudly instead of spending the developer's credit.
"""

import ast
import asyncio
import json
import pathlib
import time
from typing import Any

import httpx2
import pytest

from app.config import Settings
from app.integrations.openai import ChatClient, ChatMessage, ChatOutcome, ChatResult
from app.integrations.openai.chat import OpenAIChatClient
from tests.integrations.fakes import FakeChatClient

# Never a real key. The SDK refuses to build a client with a blank one
# (verified in 3.20.0), so "no key" is handled before the SDK is involved.
TEST_KEY = "sk-test-not-a-real-one"
PATIENT_TEXT = "patient sentinel text"
MESSAGES = (ChatMessage("system", "you are a receptionist"), ChatMessage("user", PATIENT_TEXT))


def chat_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://user:pw@localhost:5432/doctoleb",
        "redis_url": "redis://localhost:6379/0",
        "openai_api_key": TEST_KEY,
        "openai_chat_model": "test-model",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class Transport:
    """Records every request and answers from a script, like `Meta` does.

    The request count is the point of half these tests: max_retries=0 means a
    failure must produce exactly one request, whatever kind of failure it is.
    """

    def __init__(self, *responses: httpx2.Response, handler=None):
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses) or [completion_response()]
        self._handler = handler

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self._handler is not None:
            return await self._handler(request)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    @property
    def calls(self) -> int:
        return len(self.requests)


def completion_response(
    text: str | None = "a generated reply",
    finish_reason: str = "stop",
    prompt_tokens: int = 31,
    completion_tokens: int = 12,
) -> httpx2.Response:
    """A Chat Completions body in the shape the SDK parses."""
    return httpx2.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1730000000,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    )


def error_response(
    status: int, code: str | None = None, error_type: str | None = None, message: str = "went wrong"
) -> httpx2.Response:
    """OpenAI's documented error envelope.

    `message` is filled with a sentinel in the privacy tests: OpenAI echoes
    request content and, for a 401, a masked fragment of the key.
    """
    return httpx2.Response(
        status,
        json={
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": code,
            }
        },
    )


def build(transport: Transport, **overrides: Any) -> OpenAIChatClient:
    return OpenAIChatClient(
        chat_settings(**overrides),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
    )


async def test_a_successful_completion_returns_the_text_and_the_token_counts():
    transport = Transport(completion_response("hello there"))

    result = await build(transport).complete(MESSAGES)

    assert result.outcome is ChatOutcome.SUCCESS
    assert result.reason == "ok"
    assert result.text == "hello there"
    assert result.prompt_tokens == 31
    assert result.completion_tokens == 12


async def test_the_request_carries_the_model_the_messages_the_output_cap_and_store_false():
    """What actually goes on the wire, read off the recorded request.

    store=False is explicit rather than trusted to a default (plan assumption
    A2): patient content should not be kept by OpenAI for its distillation or
    evals products.
    """
    transport = Transport()

    await build(transport, openai_max_output_tokens=77).complete(MESSAGES)

    request = transport.requests[0]
    body = json.loads(request.content)
    assert str(request.url).endswith("/chat/completions")
    assert body["model"] == "test-model"
    assert body["messages"] == [
        {"role": "system", "content": "you are a receptionist"},
        {"role": "user", "content": PATIENT_TEXT},
    ]
    assert body["max_completion_tokens"] == 77
    assert body["store"] is False


def test_the_sdk_retry_budget_is_zero():
    """One retry layer, the job's (requirement 3, plan conflict S1).

    The SDK's default is 2, so three attempts per call. Inside five job tries
    that is fifteen requests, fifteen bills, and a dead letter that says five.
    This one line is what keeps that from happening.
    """
    client = build(Transport())

    assert client._sdk.max_retries == 0


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(error_response(500), id="500"),
        pytest.param(error_response(429, code="rate_limit_exceeded"), id="429"),
    ],
)
async def test_one_attempt_per_call_for_status_failures(response):
    """The most important test in the module (requirement 3).

    A 500 and a 429 are exactly the two statuses the SDK retries by default.
    One request each, or the retry layers multiply.
    """
    transport = Transport(response)

    result = await build(transport).complete(MESSAGES)

    assert result.outcome is not ChatOutcome.SUCCESS
    assert transport.calls == 1


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(httpx2.ReadTimeout("slow"), id="read-timeout"),
        pytest.param(httpx2.ConnectError("refused"), id="connect-error"),
    ],
)
async def test_one_attempt_per_call_for_transport_failures(error):
    """Same rule for a transport failure, which is where an SDK retry would
    normally kick in hardest."""

    async def handler(request):
        raise error

    transport = Transport(handler=handler)

    result = await build(transport).complete(MESSAGES)

    assert result.outcome is ChatOutcome.RETRYABLE
    assert transport.calls == 1


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        pytest.param(error_response(500), "openai_http_500", id="500"),
        pytest.param(
            httpx2.Response(502, text="<html>bad gateway</html>"),
            "openai_http_502",
            id="502-not-json",
        ),
        pytest.param(error_response(503), "openai_http_503", id="503"),
        pytest.param(error_response(408), "openai_http_408", id="408"),
        pytest.param(
            error_response(429, code="rate_limit_exceeded", error_type="requests"),
            "openai_http_429",
            id="429-rate-limit",
        ),
    ],
)
async def test_retryable_status_failures(response, reason):
    """5xx is OpenAI's problem; 429 is OpenAI asking us to slow down.

    408 is both "a timeout" and "a 4xx" (plan conflict C6) and is read as a
    timeout, which is also what the SDK's own retry set does.
    """
    result = await build(Transport(response)).complete(MESSAGES)

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == reason
    assert result.text is None


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        pytest.param(httpx2.ReadTimeout("slow"), "openai_timeout", id="read-timeout"),
        pytest.param(httpx2.ConnectError("refused"), "openai_connection", id="connect-error"),
    ],
)
async def test_retryable_transport_failures(error, reason):
    async def handler(request):
        raise error

    result = await build(Transport(handler=handler)).complete(MESSAGES)

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == reason


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        pytest.param(
            error_response(400, code="context_length_exceeded"),
            "openai_http_400_context_length_exceeded",
            id="400",
        ),
        pytest.param(
            error_response(401, code="invalid_api_key"), "openai_http_401_invalid_api_key", id="401"
        ),
        pytest.param(error_response(403), "openai_http_403", id="403"),
        pytest.param(
            error_response(404, code="model_not_found"), "openai_http_404_model_not_found", id="404"
        ),
        pytest.param(error_response(409), "openai_http_409", id="409"),
        pytest.param(error_response(422), "openai_http_422", id="422"),
    ],
)
async def test_permanent_status_failures(response, reason):
    """Every other 4xx. None of them changes because we ask again.

    409 stays permanent although the SDK would retry it (its source calls it a
    lock timeout) - plan conflict C6.
    """
    result = await build(Transport(response)).complete(MESSAGES)

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == reason


async def test_no_credit_is_permanent_although_it_is_a_429():
    """Requirement 3's one explicit exception.

    Retrying cannot buy credit. Five tries of it would only delay the fallback
    by the whole 75-second backoff curve, and the patient would wait that long
    for a message we could have sent immediately.
    """
    transport = Transport(
        error_response(429, code="insufficient_quota", error_type="insufficient_quota")
    )

    result = await build(transport).complete(MESSAGES)

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_insufficient_quota"


async def test_no_credit_is_recognised_from_the_type_alone():
    """The classifier accepts either field.

    UNVERIFIED against the live service (plan, "What was verified"): OpenAI's
    documentation says both `code` and `type` are `insufficient_quota`, and
    this was never observed on a real no-credit account. Accepting either is
    the cheap insurance.
    """
    transport = Transport(error_response(429, code=None, error_type="insufficient_quota"))

    result = await build(transport).complete(MESSAGES)

    assert result.reason == "openai_insufficient_quota"


async def test_the_wall_clock_deadline_is_enforced():
    """Plan assumption A4: the SDK's float timeout is per connection PHASE.

    httpx2.Timeout(30.0) means connect=30, read=30, write=30, pool=30, so one
    call can legitimately take several times OPENAI_TIMEOUT_SECONDS - and the
    job-timeout invariant depends on it not doing that. A MockTransport does
    not enforce the SDK's timeout at all (verified), so the only thing that can
    end this call is our own deadline.
    """

    async def handler(request):
        await asyncio.sleep(1.0)
        return completion_response()

    transport = Transport(handler=handler)
    started = time.monotonic()

    result = await build(transport, openai_timeout_seconds=0.05).complete(MESSAGES)

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == "openai_timeout"
    assert time.monotonic() - started < 0.9


async def test_an_unset_key_is_permanent_and_sends_nothing():
    """Requirement 7: the worker must boot with no OpenAI account.

    AsyncOpenAI(api_key="") raises "Missing credentials" at CONSTRUCTION
    (verified in 3.20.0), so the client is simply not built without a key -
    which is why building this one did not raise.
    """
    transport = Transport()

    result = await build(transport, openai_api_key="").complete(MESSAGES)

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_api_key_unset"
    assert transport.calls == 0


async def test_an_unset_model_is_permanent_and_sends_nothing():
    """CLAUDE.md: model names come from env vars. Blank is a configuration
    mistake, not something to guess a default for."""
    transport = Transport()

    result = await build(transport, openai_chat_model="").complete(MESSAGES)

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == "openai_model_unset"
    assert transport.calls == 0


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        pytest.param(
            completion_response("half a sen", finish_reason="length"),
            "openai_reply_truncated",
            id="length",
        ),
        pytest.param(
            completion_response("blocked", finish_reason="content_filter"),
            "openai_content_filter",
            id="content-filter",
        ),
        pytest.param(
            completion_response("", finish_reason="tool_calls"),
            "openai_unexpected_finish",
            id="tool-calls",
        ),
        pytest.param(completion_response("   "), "openai_empty_reply", id="whitespace"),
        pytest.param(completion_response(None), "openai_empty_reply", id="null-content"),
    ],
)
async def test_unusable_completions_are_permanent(response, reason):
    """Plan assumption A11.

    A truncated reply is never sent: half a sentence from a clinic is worse
    than the fallback, and the dead letter names the fix. Retrying a truncation
    rarely helps and never reliably, so all of these are permanent - which also
    makes a too-small OPENAI_MAX_OUTPUT_TOKENS loud rather than intermittent.
    """
    result = await build(Transport(response)).complete(MESSAGES)

    assert result.outcome is ChatOutcome.PERMANENT
    assert result.reason == reason
    assert result.text is None
    # The call happened and was billed, so the counts are still reported.
    assert result.prompt_tokens == 31


async def test_a_malformed_success_body_is_retryable():
    """Plan assumption A12.

    Verified: a 2xx whose body is not JSON is RETURNED by create() as a plain
    str, not raised - the SDK's default response validation is non-strict. A
    200 that is not a completion is most likely a proxy's error page, so it is
    retryable and bounded by JOB_MAX_TRIES.
    """
    transport = Transport(httpx2.Response(200, text="not json"))

    result = await build(transport).complete(MESSAGES)

    assert result.outcome is ChatOutcome.RETRYABLE
    assert result.reason == "openai_bad_response"
    assert result.text is None


async def test_no_reason_carries_openai_text_or_the_key():
    """Hard rule 8, at the one place OpenAI's own words enter the process.

    Verified: str(an SDK error) contains the whole error body, which for a 401
    includes the masked key fragment OpenAI echoes back. The classifier never
    reads error.message and never str()s the error, and this test holds it to
    that through reason, repr and str.
    """
    sentinel = "SENSITIVE-ECHO-sk-abcd1234"
    transport = Transport(error_response(401, code="invalid_api_key", message=sentinel))

    result = await build(transport).complete(MESSAGES)

    assert sentinel not in result.reason
    assert sentinel not in repr(result)
    assert sentinel not in str(result)


async def test_an_error_code_that_is_not_code_shaped_is_left_out():
    """A reason code goes into log lines and dead_letter_jobs.error.

    OpenAI's codes are [a-z0-9_]; anything else is a string we did not write
    going into a table people read casually. A phone number in an error code
    is not a hypothetical shape for it to have.
    """
    transport = Transport(error_response(400, code="Bad Code +96170123456"))

    result = await build(transport).complete(MESSAGES)

    assert result.reason == "openai_http_400"


async def test_an_exception_that_is_not_an_openai_error_escapes():
    """VS-004's rule for the Meta client, applied here.

    A bug in OUR code must not be retried five times and answered with the
    fallback as if OpenAI had been down. classify_openai_error returns None for
    anything that is not an SDK error, and complete() re-raises.
    """

    async def handler(request):
        raise ValueError("a bug in our own code")

    with pytest.raises(ValueError):
        await build(Transport(handler=handler)).complete(MESSAGES)


def test_the_client_and_the_fake_satisfy_the_protocol():
    """runtime_checkable, so the fake and the real client cannot drift apart."""
    assert isinstance(build(Transport()), ChatClient)
    assert isinstance(FakeChatClient(), ChatClient)


async def test_a_real_transport_is_blocked_in_the_test_suite():
    """The network block itself, asserted rather than assumed.

    Without this test the autouse fixture could stop working and nothing would
    notice until a test run appeared on the developer's OpenAI bill.
    """
    async with httpx2.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="reach the network"):
            await client.get("https://api.openai.com/v1/models")


def test_only_the_openai_integration_imports_the_sdk():
    """Hard rule 3 made structural, and "classify in one place" made enforceable.

    The LLM gets no raw HTTP access: the SDK lives behind ChatClient in exactly
    one module. An `import openai` anywhere else is a second place a request
    could be made, a second place an exception could be swallowed, and a second
    place a response body could reach a log.
    """
    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == "openai" or name.startswith("openai.") for name in names):
                offenders.append(path.as_posix())

    assert sorted(set(offenders)) == ["app/integrations/openai/chat.py"]


def test_no_repr_shows_message_content():
    """pytest prints reprs on a failed assertion, which is exactly how patient
    text reaches a CI log - the same reason VS-002 rewrote Base.__repr__."""
    sentinel = "REPR-SENTINEL-patient-words"

    assert sentinel not in repr(ChatMessage("user", sentinel))
    assert sentinel not in repr(ChatResult(ChatOutcome.SUCCESS, "ok", sentinel, 1, 1))
