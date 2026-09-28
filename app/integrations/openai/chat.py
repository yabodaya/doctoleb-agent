"""The one module in this repo that imports the OpenAI SDK.

One attempt, one wall-clock deadline, one classifier. Nothing outside this file
ever sees an SDK exception or an HTTP status: the job gets a ChatResult with an
outcome and a reason code, and decides from those alone.

Read `docs/plans/VS-005-plan.md`, section "Classification: one function, one
table", before changing any of the decisions below.
"""

import asyncio
import re
from collections.abc import Sequence
from typing import Any

import httpx2
import openai
from openai import AsyncOpenAI

from app.config import Settings
from app.integrations.openai.interface import ChatMessage, ChatOutcome, ChatResult

# OpenAI error codes look like this (insufficient_quota, model_not_found).
# Anything else is left out of the reason rather than trusted into a log line or
# a dead letter (hard rule 8).
_CODE_SHAPE = re.compile(r"[a-z0-9_]{1,48}")

# The one 429 that retrying cannot fix: the account has no credit (requirement 3).
# Checked against both `code` and `type`, which OpenAI's documented body sets to
# the same value; see "What was verified" in the plan.
_NO_CREDIT = "insufficient_quota"


def classify_openai_error(error: Exception) -> tuple[ChatOutcome, str] | None:
    """The single place an OpenAI failure becomes a retry decision.

    Retryable: timeouts (ours and the SDK's, and a 408), connection errors, 5xx,
    and a 429 rate limit. Permanent: every other 4xx, and a 429 for no credit.

    Never reads error.message, and never str(error): both carry OpenAI's text,
    and for a 401 the masked key fragment OpenAI echoes back. Returns None for
    anything that is not an OpenAI API error, so a bug escapes instead of being
    retried five times and answered with the fallback as if OpenAI were down.
    """
    # Before APIConnectionError: APITimeoutError is a subclass of it. The
    # built-in TimeoutError is our own asyncio.timeout deadline firing.
    if isinstance(error, TimeoutError | openai.APITimeoutError):
        return ChatOutcome.RETRYABLE, "openai_timeout"
    if isinstance(error, openai.APIConnectionError):
        return ChatOutcome.RETRYABLE, "openai_connection"
    if isinstance(error, openai.APIStatusError):
        status = error.status_code
        if status == 429:
            if _NO_CREDIT in (error.code, error.type):
                return ChatOutcome.PERMANENT, "openai_insufficient_quota"
            return ChatOutcome.RETRYABLE, "openai_http_429"
        if status >= 500 or status == 408:
            return ChatOutcome.RETRYABLE, f"openai_http_{status}"
        code = error.code if error.code and _CODE_SHAPE.fullmatch(error.code) else None
        return ChatOutcome.PERMANENT, f"openai_http_{status}" + (f"_{code}" if code else "")
    if isinstance(error, openai.APIError):
        # APIResponseValidationError and anything else the SDK adds later. A
        # response we could not make sense of is most likely transient.
        return ChatOutcome.RETRYABLE, "openai_bad_response"
    return None


def _token_counts(completion: object) -> tuple[int | None, int | None]:
    """prompt_tokens and completion_tokens, when the body carried them.

    Reported even for an unusable completion: the call happened and was billed,
    and a truncation is precisely the case where the numbers explain the
    failure.
    """
    usage = getattr(completion, "usage", None)
    prompt = getattr(usage, "prompt_tokens", None)
    generated = getattr(usage, "completion_tokens", None)
    return (
        prompt if isinstance(prompt, int) else None,
        generated if isinstance(generated, int) else None,
    )


def read_completion(completion: object) -> ChatResult:
    """The only place a 2xx becomes a usable reply, or a reason it is not one.

    `getattr` throughout rather than attribute access, because a 2xx whose body
    is not JSON is RETURNED by the SDK as a plain str (verified in 3.20.0,
    non-strict response validation), not raised. A 200 that is not a completion
    is most likely a proxy's error page: retryable (plan assumption A12).

    A truncated, filtered or empty reply is PERMANENT and never sent (plan
    assumption A11): half a sentence from a clinic is worse than the fallback,
    and the dead letter names the fix.
    """
    prompt_tokens, completion_tokens = _token_counts(completion)

    def failed(outcome: ChatOutcome, reason: str) -> ChatResult:
        return ChatResult(outcome, reason, None, prompt_tokens, completion_tokens)

    choices = getattr(completion, "choices", None)
    if not isinstance(choices, list) or not choices:
        return failed(ChatOutcome.RETRYABLE, "openai_bad_response")

    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        return failed(ChatOutcome.PERMANENT, "openai_reply_truncated")
    if finish_reason == "content_filter":
        return failed(ChatOutcome.PERMANENT, "openai_content_filter")
    if finish_reason != "stop":
        # tool_calls or function_call: this slice gives the model no tools, so
        # either means the request was not the one we think we sent.
        return failed(ChatOutcome.PERMANENT, "openai_unexpected_finish")

    text = getattr(getattr(choice, "message", None), "content", None)
    if not isinstance(text, str) or not text.strip():
        return failed(ChatOutcome.PERMANENT, "openai_empty_reply")

    return ChatResult(ChatOutcome.SUCCESS, "ok", text, prompt_tokens, completion_tokens)


class OpenAIChatClient:
    """ChatClient over the OpenAI SDK. One per worker process (plan assumption A9).

    The httpx2 client is injectable for the same reason MetaClient's httpx one
    is: every test passes one wired to an httpx2.MockTransport, so nothing in
    this repo's test suite can reach OpenAI.
    """

    def __init__(self, settings: Settings, http_client: httpx2.AsyncClient | None = None) -> None:
        self._model = settings.openai_chat_model.strip()
        self._max_output_tokens = settings.openai_max_output_tokens
        self._deadline = settings.openai_timeout_seconds
        # Not built without a key: the SDK raises "Missing credentials" for an
        # empty one (verified in 3.20.0), and the worker must boot without a key
        # (requirement 7). complete() reports it as openai_api_key_unset instead.
        #
        # max_retries=0: one retry layer, the job's (requirement 3, plan S1).
        self._sdk = (
            AsyncOpenAI(
                api_key=settings.openai_api_key,
                max_retries=0,
                timeout=self._deadline,
                http_client=http_client,
            )
            if settings.openai_api_key
            else None
        )

    async def complete(self, messages: Sequence[ChatMessage]) -> ChatResult:
        """One attempt to write one reply. Never a retry.

        Nothing here logs: the job writes one line per generation, because only
        the job knows the webhook_inbox row id every other line carries (plan
        assumption A5).
        """
        if self._sdk is None:
            return ChatResult(ChatOutcome.PERMANENT, "openai_api_key_unset")
        if not self._model:
            return ChatResult(ChatOutcome.PERMANENT, "openai_model_unset")

        body: list[dict[str, Any]] = [
            {"role": message.role, "content": message.content} for message in messages
        ]
        try:
            # The SDK's timeout is per connection phase; this is the whole call
            # (plan assumption A4). The job's timeout budget depends on it.
            async with asyncio.timeout(self._deadline):
                completion = await self._sdk.chat.completions.create(
                    model=self._model,
                    messages=body,  # type: ignore[arg-type]
                    max_completion_tokens=self._max_output_tokens,
                    # Not kept for OpenAI's distillation/evals products (plan A2).
                    store=False,
                )
        except Exception as error:  # noqa: BLE001 - re-raised unless it is ours
            classified = classify_openai_error(error)
            if classified is None:
                raise
            return ChatResult(*classified)
        return read_completion(completion)

    async def aclose(self) -> None:
        """Close the SDK's own connection pool, which is not the worker's httpx one."""
        if self._sdk is not None:
            await self._sdk.close()
