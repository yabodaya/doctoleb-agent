"""A ChatClient that never leaves the process.

The counterpart of `Meta` in tests/worker/conftest.py: every test that runs a
job runs it against this, because nothing in this suite may reach OpenAI
(VS-005 requirement 1) and several tests need to know exactly how many model
calls happened.
"""

import inspect
import json
from collections.abc import Awaitable, Callable, Sequence

from app.integrations.openai import (
    ChatMessage,
    ChatOutcome,
    ChatResult,
    ToolCallRequest,
    ToolSpec,
    TranscriptionResult,
)

# The text the fake model "writes". Asserted ABSENT from every log line, job
# result and dead letter, and PRESENT in the reply row and the Meta request -
# which is the whole of "the generated text is what gets sent".
AI_REPLY = "synthetic ai reply"

# What the fake audio model "hears" (VS-008). An obvious synthetic sentence, as
# every string in these fixtures is: hard rule 8 forbids a real recording OR a
# real person's words in a fixture, and a plausible-looking transcript in a test
# file is the second of those. Asserted PRESENT in messages.text and ABSENT
# everywhere else.
TRANSCRIPT = "synthetic voice note transcript"


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


# A scripted step: a fixed answer, or a function of the conversation so far.
Step = ChatResult | Callable[[Sequence[ChatMessage]], ChatResult | Awaitable[ChatResult]]


class FakeChatClient:
    """A ChatClient that records every call and answers from a script.

    The last scripted result repeats, so a test that wants "always 503" passes
    one result and a test that wants "503 then ok" passes two. `hook` runs
    inside the call, which is how the worker tests put a staff takeover
    *during* generation - the one window hard rule 7's second read exists for.

    `tool_specs` is a list PARALLEL to `calls`, holding the tools it was given
    on each call. Kept separate so every VS-005 assertion on `calls` is
    unchanged.

    **A step may be a CALLABLE** (VS-007): it is given the messages so far and
    returns the `ChatResult`. That is what lets a script behave like a real model
    instead of a recording - a booking flow has to pass back the `slot_id` the
    SEARCH RESULT just produced, and that id is an opaque token the test cannot
    know in advance. A callable step may be async, for a script that needs to
    await something of its own.
    """

    def __init__(self, *results: Step, hook=None):
        self.calls: list[list[ChatMessage]] = []
        self.tool_specs: list[list[ToolSpec]] = []
        self._results: list[Step] = list(results) or [ok()]
        self._hook = hook

    async def complete(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()
    ) -> ChatResult:
        self.calls.append(list(messages))
        self.tool_specs.append(list(tools))
        if self._hook is not None:
            await self._hook(messages)
        step = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(step, ChatResult):
            return step
        answer = step(messages)
        if inspect.isawaitable(answer):
            answer = await answer
        return answer


# --------------------------------------------------------------------------
# VS-008: the audio model
# --------------------------------------------------------------------------


def heard(text: str = TRANSCRIPT, seconds: float | None = 3.5) -> TranscriptionResult:
    return TranscriptionResult(ChatOutcome.SUCCESS, "ok", text, seconds)


def unclear(text: str = "") -> TranscriptionResult:
    """A SUCCESS whose text is nothing a model could act on.

    Note the outcome: an empty transcript is not a FAILURE of the audio model.
    The call worked and was billed; there was simply nothing in the audio. The
    job's `unusable_reason` is what turns this into the UNCLEAR reply (W5).
    """
    return TranscriptionResult(ChatOutcome.SUCCESS, "ok", text, 1.0)


def transcribe_retryable(reason: str = "openai_http_503") -> TranscriptionResult:
    return TranscriptionResult(ChatOutcome.RETRYABLE, reason)


def transcribe_permanent(reason: str = "openai_insufficient_quota") -> TranscriptionResult:
    return TranscriptionResult(ChatOutcome.PERMANENT, reason)


# A scripted transcription step: a fixed answer, or a function of the audio.
TranscribeStep = (
    TranscriptionResult | Callable[[bytes], TranscriptionResult | Awaitable[TranscriptionResult]]
)


class FakeTranscribeClient:
    """A TranscribeClient that never leaves the process.

    `FakeChatClient`'s shape: scripted results, the last one repeating, and
    callable steps allowed so a script can answer differently for different
    audio.

    `calls` records **the byte count and the content type, and nothing else**.
    Deliberately not the audio and not the transcript it was asked to return: a
    fake that stored either would put patient content in a fixture, which is
    the thing hard rule 8 forbids. A test that wants to know what was sent
    asserts on the count.
    """

    def __init__(self, *results: TranscribeStep, hook=None):
        self.calls: list[tuple[int, str]] = []
        self._results: list[TranscribeStep] = list(results) or [heard()]
        self._hook = hook

    async def transcribe(
        self, audio: bytes, *, filename: str, content_type: str
    ) -> TranscriptionResult:
        self.calls.append((len(audio), content_type))
        if self._hook is not None:
            await self._hook(audio)
        step = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(step, TranscriptionResult):
            return step
        answer = step(audio)
        if inspect.isawaitable(answer):
            answer = await answer
        return answer


def last_tool_result(messages: Sequence[ChatMessage]) -> dict:
    """The JSON of the most recent `tool` message, parsed.

    The one helper a callable step needs: it is how a script reads what a tool
    just returned - a `slot_id` from a search, an `appointment_id` from a list -
    without the test having to predict an opaque id.
    """
    for message in reversed(messages):
        if message.role == "tool" and message.content:
            return json.loads(message.content)
    raise AssertionError("no tool message in the conversation yet")
