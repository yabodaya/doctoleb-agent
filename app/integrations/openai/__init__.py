"""The OpenAI integration.

This package re-exports the INTERFACE only, never OpenAIChatClient: importing
the interface - which app/agent/ does - must not load the SDK. The worker
imports the client from app.integrations.openai.chat explicitly.
"""

from app.integrations.openai.interface import (
    ChatClient,
    ChatMessage,
    ChatOutcome,
    ChatResult,
    Role,
    ToolCallRequest,
    ToolSpec,
)

__all__ = [
    "ChatClient",
    "ChatMessage",
    "ChatOutcome",
    "ChatResult",
    "Role",
    "ToolCallRequest",
    "ToolSpec",
]
