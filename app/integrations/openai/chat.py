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
from app.integrations.openai.interface import (
    ChatMessage,
    ChatOutcome,
    ChatResult,
    ToolCallRequest,
    ToolSpec,
)

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


def _to_wire(message: ChatMessage) -> dict[str, Any]:
    """One ChatMessage as OpenAI's Chat Completions wants it.

    `system` and `user` come out byte-identical to VS-005, so a tool-less call
    sends exactly the body it used to. That is asserted by a test, because the
    cheapest way to break a working integration is to "tidy" the request shape.

    The assistant and tool shapes are the ones the SDK round-trips unchanged
    (verified in 3.20.0, plan check U2):
      {"role": "assistant", "content": null, "tool_calls": [
          {"id", "type": "function", "function": {"name", "arguments"}}]}
      {"role": "tool", "tool_call_id": ..., "content": "<string>"}
    """
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": message.content or "",
        }
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return wire


def _tool_specs_to_wire(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            },
        }
        for tool in tools
    ]


def _tool_calls(message: object) -> tuple[ToolCallRequest, ...] | None:
    """The message's tool calls, or None if any of them is malformed.

    `getattr` throughout, deliberately. The SDK's response validation is
    non-strict, and a call whose `type` is not "function" still parses: an
    unknown type builds a function-call object whose `.function` is **None**,
    and `type: "custom"` builds a different class with no `.function` attribute
    at all (both verified in 3.20.0, plan check U2). Attribute access would
    raise inside the classifier, where an exception is the least useful thing.

    Malformed means "we could not answer this with a `tool` message OpenAI would
    accept", and that is a PERMANENT failure rather than something to guess at:

      - a type other than "function": there is no function to call;
      - a missing or non-string id, name or arguments: `tool_call_id` and the
        registry lookup both need real strings;
      - a repeated id: two tool messages with the same id is a request OpenAI
        rejects, and we could not tell which answer belonged to which call.

    An UNKNOWN tool name and arguments that are not JSON are NOT malformed here.
    They are perfectly good strings, and the loop answers them with a tool error
    the model can act on (plan section 5.6).

    `()` means the model asked for no tools, which is the ordinary case.
    """
    raw = getattr(message, "tool_calls", None)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        return None

    calls: list[ToolCallRequest] = []
    seen: set[str] = set()
    for entry in raw:
        if getattr(entry, "type", None) != "function":
            return None
        function = getattr(entry, "function", None)
        call_id = getattr(entry, "id", None)
        name = getattr(function, "name", None)
        arguments = getattr(function, "arguments", None)
        if not isinstance(call_id, str) or not isinstance(name, str):
            return None
        if not isinstance(arguments, str) or not call_id or not name:
            return None
        if call_id in seen:
            return None
        seen.add(call_id)
        calls.append(ToolCallRequest(call_id, name, arguments))
    return tuple(calls)


def read_completion(completion: object) -> ChatResult:
    """The only place a 2xx becomes a usable reply, or a reason it is not one.

    `getattr` throughout rather than attribute access, because a 2xx whose body
    is not JSON is RETURNED by the SDK as a plain str (verified in 3.20.0,
    non-strict response validation), not raised. A 200 that is not a completion
    is most likely a proxy's error page: retryable (plan assumption A12).

    A truncated, filtered or empty reply is PERMANENT and never sent (plan
    assumption A11): half a sentence from a clinic is worse than the fallback,
    and the dead letter names the fix.

    VS-006 adds two rows to the table. A malformed tool call is PERMANENT, and
    a well-formed one makes the turn a SUCCESS with no text (plan conflict C8).
    The order below is load-bearing: `length` is checked BEFORE the tool calls,
    because truncated arguments are not arguments - they are the front half of
    a JSON object, and running a tool on half its arguments is worse than
    failing.
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

    message = getattr(choice, "message", None)
    text = getattr(message, "content", None)
    tool_calls = _tool_calls(message)
    if tool_calls is None:
        return failed(ChatOutcome.PERMANENT, "openai_malformed_tool_call")
    if tool_calls:
        # A tool turn. `finish_reason` may be "tool_calls" or "stop" - both were
        # observed to parse (plan check U2) - and the calls themselves are what
        # decide, not the label. `text` is the model's interim words ("let me
        # check"): echoed back to it, never sent to the patient.
        return ChatResult(
            ChatOutcome.SUCCESS,
            "ok",
            text if isinstance(text, str) and text.strip() else None,
            prompt_tokens,
            completion_tokens,
            tool_calls,
        )

    if finish_reason != "stop":
        # tool_calls or function_call with nothing to call: the request was not
        # the one we think we sent.
        return failed(ChatOutcome.PERMANENT, "openai_unexpected_finish")

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

    async def complete(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()
    ) -> ChatResult:
        """One attempt at one model call. Never a retry.

        Nothing here logs: the job writes one line per generation, because only
        the job knows the webhook_inbox row id every other line carries (plan
        assumption A5).

        `parallel_tool_calls`, `tool_choice` and `strict` are deliberately NOT
        sent. Some models reject them, our Pydantic validation is the authority
        on arguments anyway (hard rule 3), and the server default for parallel
        calls is fine because the loop answers every call it is given.
        """
        if self._sdk is None:
            return ChatResult(ChatOutcome.PERMANENT, "openai_api_key_unset")
        if not self._model:
            return ChatResult(ChatOutcome.PERMANENT, "openai_model_unset")

        body = [_to_wire(message) for message in messages]
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": body,
            "max_completion_tokens": self._max_output_tokens,
            # Not kept for OpenAI's distillation/evals products (plan A2).
            "store": False,
        }
        # Added only when there are tools, so a tool-less call is byte-identical
        # to VS-005's and needs no `Omit` sentinel from the SDK.
        if tools:
            kwargs["tools"] = _tool_specs_to_wire(tools)
        try:
            # The SDK's timeout is per connection phase; this is the whole call
            # (plan assumption A4). In VS-006 this deadline itself sits inside
            # the turn budget, which is what bounds a whole tool loop.
            async with asyncio.timeout(self._deadline):
                completion = await self._sdk.chat.completions.create(**kwargs)
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
