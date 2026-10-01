"""The chat model, behind an interface (VS-005 requirement 1).

Like JobQueue and TenantResolver: the worker and the agent depend on this
Protocol; the OpenAI SDK sits behind it in one module (chat.py); every test
uses tests/integrations/fakes.py:FakeChatClient. That is how no test can reach
OpenAI, and why switching provider or API is a change to one class.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

# "tool" joins in VS-006: a tool result is a message in the conversation, with
# the id of the call it answers.
Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call: OUR name, OUR description, OUR JSON Schema.

    Built by the registry from a Pydantic args model. Nothing here comes from
    the model, and nothing here is secret - the whole thing is sent on every
    model call, so a tenant id in it would be a tenant id handed to the model
    (hard rule 4). A test greps every spec for one.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    """One tool call the model ASKED for. Our code decides whether to run it.

    Everything on it is untrusted input, exactly like a patient's message:
    `name` is model-written until the registry matches it against a registered
    tool, and `arguments` is model-written JSON **text** which can quote the
    patient's own words.

    So neither is ever logged, stored or put in a repr (hard rule 8). The repr
    shows a length, which is enough to tell two calls apart in a traceback and
    carries nothing.
    """

    id: str
    name: str = field(repr=False)
    arguments: str = field(repr=False)

    def __repr__(self) -> str:
        return f"ToolCallRequest(id={self.id!r}, chars={len(self.arguments)})"


class ChatOutcome(StrEnum):
    """What kind of result one model call produced.

    Three values, because there are exactly three things the job can do: send
    the reply, try again later, or stop trying and send the fallback.
    """

    SUCCESS = "SUCCESS"
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"


@dataclass(frozen=True)
class ChatMessage:
    """One turn of the conversation as the model sees it.

    `repr` shows the role and the LENGTH, never the content (hard rule 8):
    pytest prints reprs on a failed assertion, which is exactly how a patient's
    words reach a CI log.

    `content` became optional in VS-006 because an assistant turn that only
    asks for tools has none. The remaining fields have defaults, so every
    VS-005 construction - positional or keyword - still works unchanged.
    """

    role: Role
    content: str | None = field(default=None, repr=False)
    # Set on an assistant turn that asked for tools, so the request we send back
    # reproduces what the model said.
    tool_calls: tuple[ToolCallRequest, ...] = ()
    # Set on a `tool` turn: which call this message answers. OpenAI requires
    # exactly one tool message per tool_call_id.
    tool_call_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"ChatMessage(role={self.role!r}, chars={len(self.content or '')}, "
            f"tool_calls={len(self.tool_calls)})"
        )


@dataclass(frozen=True)
class ChatResult:
    """The outcome of ONE attempt, classified.

    `reason` is a short code - `ok`, `openai_http_429`, `openai_model_unset` -
    built from a status and an allow-listed error code and nothing else. It is
    written to logs and to dead_letter_jobs.error, so it must be as safe to
    keep as an identifier. OpenAI's own error text never reaches it.

    `text` is the generated reply, and is excluded from the repr for the same
    reason as ChatMessage.content.

    **SUCCESS means non-empty text OR at least one well-formed tool call**
    (VS-006, plan conflict C8). It no longer guarantees text: a turn where the
    model only asked for tools is a success, and its `text` is None. A caller
    that wants a reply must check `tool_calls` first - the loop does, and that
    is the only caller.

    `tool_calls` is LAST so every VS-005 positional construction is unchanged.
    """

    outcome: ChatOutcome
    reason: str
    text: str | None = field(default=None, repr=False)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.outcome is ChatOutcome.SUCCESS


@dataclass(frozen=True)
class TranscriptionResult:
    """The outcome of ONE transcription attempt, classified (VS-008).

    `outcome` reuses `ChatOutcome` rather than introducing a fourth parallel
    three-value enum: the job's three choices are the same three - use it, try
    again later, stop trying.

    `reason` is a short code built from a status and an allow-listed error code,
    as `ChatResult.reason` is, because it is written to logs and to
    `dead_letter_jobs.error`.

    `text` is the patient's own words and is excluded from the repr for exactly
    the reason `ChatMessage.content` is: pytest prints reprs on a failed
    assertion, which is how a patient's words reach a CI log (hard rule 8).

    `seconds` is whatever the API reported, when it reports anything at all -
    the newer audio models return JSON with `usage` and nothing else, and some
    of them report tokens rather than seconds (plan check U8). Nothing decides
    with it; it is recorded so the real cost per voice note can be worked out
    from a table instead of from a bill.
    """

    outcome: ChatOutcome
    reason: str
    text: str | None = field(default=None, repr=False)
    seconds: float | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is ChatOutcome.SUCCESS


@runtime_checkable
class TranscribeClient(Protocol):
    """What the worker is allowed to know about an audio model (VS-008).

    Deliberately NOT something `app/agent/` ever sees. The model must not be
    able to decide whether to transcribe, or to see a media id: a transcript is
    the INPUT to a turn, not something the turn can ask for (hard rule 3). So
    this Protocol is injected into the JOB, and the transcript reaches the Agent
    Core as a plain string on `Turn.input_text`.
    """

    async def transcribe(
        self, audio: bytes, *, filename: str, content_type: str
    ) -> TranscriptionResult:
        """One attempt. Never a retry (hard rule 11).

        Never raises for a provider failure: it returns a classified result. A
        bug in OUR code still raises.

        `audio` is bytes in memory, never a path: the audio is not stored
        anywhere (W1), so there is no file to name.
        """
        ...


@runtime_checkable
class ChatClient(Protocol):
    """What the worker and the agent are allowed to know about a chat model."""

    async def complete(
        self, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()
    ) -> ChatResult:
        """One attempt. Never a retry (requirement 3, hard rule 11).

        Never raises for a provider failure: it returns a classified result,
        because "what kind of failure was this" has one home. A bug in OUR code
        still raises.

        `tools` is what the model MAY ask for; it never runs anything. The
        answer comes back as `ChatResult.tool_calls`, which our code validates
        and executes (hard rule 3).
        """
        ...
