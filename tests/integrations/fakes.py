"""A ChatClient that never leaves the process.

The counterpart of `Meta` in tests/worker/conftest.py: every test that runs a
job runs it against this, because nothing in this suite may reach OpenAI
(VS-005 requirement 1) and several tests need to know exactly how many model
calls happened.
"""

import json
from collections.abc import Sequence

from app.integrations.openai import (
    ChatMessage,
    ChatOutcome,
    ChatResult,
    ToolCallRequest,
    ToolSpec,
)

# The text the fake model "writes". Asserted ABSENT from every log line, job
# result and dead letter, and PRESENT in the reply row and the Meta request -
# which is the whole of "the generated text is what gets sent".
AI_REPLY = "synthetic ai reply"


def ok(text: str = AI_REPLY, prompt_tokens: int = 11, completion_tokens: int = 7) -> ChatResult:
    return ChatResult(ChatOutcome.SUCCESS, "ok", text, prompt_tokens, completion_tokens)


def retryable(reason: str = "openai_http_503") -> ChatResult:
    """A failure worth trying again: OpenAI was down, slow or rate-limiting."""
    return ChatResult(ChatOutcome.RETRYABLE, reason)


def permanent(reason: str = "openai_insufficient_quota") -> ChatResult:
    """A failure retrying cannot fix: no credit, no model, a bad key."""
    return ChatResult(ChatOutcome.PERMANENT, reason)


def tool_call(name: str, arguments: dict | str, call_id: str | None = None) -> ToolCallRequest:
    """One tool call the fake model asks for.

    `arguments` takes a dict for the ordinary case and a raw `str` for the
    interesting ones: the model sends JSON TEXT, so "{not json" and "[1,2]" are
    things it can really produce and things the registry has to answer.
    """
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ToolCallRequest(call_id or f"call_{name}", name, raw)


def wants_tools(
    *calls: ToolCallRequest,
    text: str | None = None,
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
) -> ChatResult:
    """A SUCCESS that asks for tools instead of answering.

    `text` is the model's interim words ("let me check"): echoed back to it,
    never sent to the patient.
    """
    return ChatResult(
        ChatOutcome.SUCCESS, "ok", text, prompt_tokens, completion_tokens, tuple(calls)
    )


class FakeChatClient:
    """A ChatClient that records every call and answers from a script.

    The last scripted result repeats, so a test that wants "always 503" passes
    one result and a test that wants "503 then ok" passes two. `hook` runs
    inside the call, which is how the worker tests put a staff takeover
    *during* generation - the one window hard rule 7's second read exists for.

    `tool_specs` is a list PARALLEL to `calls`, holding the tools it was given
    on each call. Kept separate so every VS-005 assertion on `calls` is
    unchanged.
    """

    def __init__(self, *results: ChatResult, hook=None):
        self.calls: list[list[ChatMessage]] = []
        self.tool_specs: list[list[ToolSpec]] = []
        self._results = list(results) or [ok()]
        self._hook = hook

    async def complete(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()
    ) -> ChatResult:
        self.calls.append(list(messages))
        self.tool_specs.append(list(tools))
        if self._hook is not None:
            await self._hook(messages)
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]
