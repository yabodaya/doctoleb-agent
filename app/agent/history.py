"""Conversation history, as the model sees it.

Pure mapping over plain data. WHICH rows come here - the last N of this
conversation, before the message being answered, failed outbound skipped - is
decided in SQL by MessageRepository.history_before, so AGENT_HISTORY_MESSAGES
counts only what the model will actually be shown (requirement 5).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.db.enums import MessageDirection, MessageModality
from app.integrations.openai import ChatMessage

# What the model sees instead of content it cannot read. Fixed strings, never
# derived from the payload (requirement 5, hard rule 8): a media id or a caption
# is still patient content, and not ours to forward. One generic placeholder for
# every non-text type, because messages stores no Meta type (plan conflict C4).
VOICE_NOTE_PLACEHOLDER = "[patient sent a voice note]"
NON_TEXT_PLACEHOLDER = "[patient sent a photo, file, location or other non-text message]"


@dataclass(frozen=True)
class HistoryEntry:
    """One earlier message, reduced to the three things the model needs.

    Built inside the job's first transaction, while the session is open, so no
    ORM object ever crosses into the agent. `text` is kept out of the repr for
    the usual reason (hard rule 8): pytest prints reprs on a failed assertion.
    """

    direction: MessageDirection
    modality: MessageModality
    text: str | None = field(default=None, repr=False)


def content_for(modality: MessageModality, text: str | None) -> str:
    """A message's text, or the placeholder for what it was.

    A voice note WITH a transcript (VS-008 writes it into messages.text) is its
    transcript; without one, the voice placeholder. Used for the history and for
    the message being answered alike, so the two can never describe the same
    kind of message differently.

    Blank text counts as no text: a message whose body is whitespace tells the
    model nothing, and an empty `user` turn is worse than saying what arrived.
    """
    if text and text.strip():
        return text
    if modality is MessageModality.VOICE_NOTE:
        return VOICE_NOTE_PLACEHOLDER
    return NON_TEXT_PLACEHOLDER


def to_chat_messages(entries: Sequence[HistoryEntry]) -> list[ChatMessage]:
    """INBOUND -> user, OUTBOUND -> assistant, oldest first.

    An outbound entry with no text is SKIPPED rather than given a placeholder:
    a placeholder would tell the model the clinic had sent a photo. It cannot
    happen today - every outbound row is reserved with its text - and skipping
    is the safe reading if it ever does.
    """
    messages: list[ChatMessage] = []
    for entry in entries:
        if entry.direction is MessageDirection.OUTBOUND:
            if not (entry.text and entry.text.strip()):
                continue
            messages.append(ChatMessage("assistant", entry.text))
        else:
            messages.append(ChatMessage("user", content_for(entry.modality, entry.text)))
    return messages
