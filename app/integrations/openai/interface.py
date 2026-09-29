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
