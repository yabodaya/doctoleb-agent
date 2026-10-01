"""The OpenAI integration.

This package re-exports the INTERFACE only, never OpenAIChatClient or
OpenAITranscribeClient: importing the interface - which app/agent/ does - must
not load the SDK. The worker imports each client from its own module
explicitly.

VS-008 adds the transcription side of that interface, plus the PURE transcript
rules from transcripts.py - which import no SDK, no settings and no logging, so
they are safe to sit beside the Protocols.
"""

from app.integrations.openai.interface import (
    ChatClient,
    ChatMessage,
    ChatOutcome,
    ChatResult,
    Role,
    ToolCallRequest,
    ToolSpec,
    TranscribeClient,
    TranscriptionResult,
)
from app.integrations.openai.transcripts import (
    MIN_TRANSCRIPT_CHARS,
    SILENCE_HALLUCINATIONS,
    normalise_transcript,
    unusable_reason,
)

__all__ = [
    "MIN_TRANSCRIPT_CHARS",
    "SILENCE_HALLUCINATIONS",
    "ChatClient",
    "ChatMessage",
    "ChatOutcome",
    "ChatResult",
    "Role",
    "ToolCallRequest",
    "ToolSpec",
    "TranscribeClient",
    "TranscriptionResult",
    "normalise_transcript",
    "unusable_reason",
]
