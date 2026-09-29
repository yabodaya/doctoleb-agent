"""The chat model, behind an interface (VS-005 requirement 1).

Like JobQueue and TenantResolver: the worker and the agent depend on this
Protocol; the OpenAI SDK sits behind it in one module (chat.py); every test
uses tests/integrations/fakes.py:FakeChatClient. That is how no test can reach
OpenAI, and why switching provider or API is a change to one class.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]


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
    """

    role: Role
    content: str = field(repr=False)

    def __repr__(self) -> str:
        return f"ChatMessage(role={self.role!r}, chars={len(self.content)})"


@dataclass(frozen=True)
class ChatResult:
    """The outcome of ONE attempt, classified.

    `reason` is a short code - `ok`, `openai_http_429`, `openai_model_unset` -
    built from a status and an allow-listed error code and nothing else. It is
    written to logs and to dead_letter_jobs.error, so it must be as safe to
    keep as an identifier. OpenAI's own error text never reaches it.

    `text` is the generated reply, and is excluded from the repr for the same
    reason as ChatMessage.content. SUCCESS guarantees it is non-empty.
    """

    outcome: ChatOutcome
    reason: str
    text: str | None = field(default=None, repr=False)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is ChatOutcome.SUCCESS


@runtime_checkable
class ChatClient(Protocol):
    """What the worker and the agent are allowed to know about a chat model."""

    async def complete(self, messages: Sequence[ChatMessage]) -> ChatResult:
        """One attempt. Never a retry (requirement 3, hard rule 11).

        Never raises for a provider failure: it returns a classified result,
        because "what kind of failure was this" has one home. SUCCESS
        guarantees non-empty text. A bug in OUR code still raises.
        """
        ...
