"""Rows of `messages` turned into chat turns.

Pure mapping: no database, no SDK. The SQL half - which rows, in what order -
is MessageRepository.history_before's job and is tested in tests/db/.
"""

from app.agent.history import (
    NON_TEXT_PLACEHOLDER,
    VOICE_NOTE_PLACEHOLDER,
    HistoryEntry,
    content_for,
    to_chat_messages,
)
from app.db.enums import MessageDirection, MessageModality


def inbound(text=None, modality=MessageModality.TEXT) -> HistoryEntry:
    return HistoryEntry(MessageDirection.INBOUND, modality, text)


def outbound(text=None, modality=MessageModality.TEXT) -> HistoryEntry:
    return HistoryEntry(MessageDirection.OUTBOUND, modality, text)


def test_inbound_becomes_user_and_outbound_becomes_assistant():
    """The roles are what let the model tell the clinic's words from the
    patient's - which is what "patient text is data" rests on."""
    messages = to_chat_messages([inbound("hello"), outbound("hi there")])

    assert [(m.role, m.content) for m in messages] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]


def test_the_order_is_kept_oldest_first():
    entries = [inbound("one"), outbound("two"), inbound("three")]

    assert [m.content for m in to_chat_messages(entries)] == ["one", "two", "three"]


def test_a_voice_note_without_a_transcript_becomes_the_voice_placeholder():
    messages = to_chat_messages([inbound(None, MessageModality.VOICE_NOTE)])

    assert messages[0].content == VOICE_NOTE_PLACEHOLDER


def test_a_voice_note_with_a_transcript_is_sent_as_its_transcript():
    """VS-008 writes the transcript into messages.text; nothing here changes then."""
    messages = to_chat_messages([inbound("the transcript", MessageModality.VOICE_NOTE)])

    assert messages[0].content == "the transcript"


def test_a_non_text_message_becomes_the_placeholder_never_its_payload():
    """Plan conflict C4: one generic placeholder, because `messages` stores no
    Meta type. HistoryEntry has no field that COULD carry a payload, so a media
    id or a caption cannot reach OpenAI by accident (hard rule 8).
    """
    messages = to_chat_messages([inbound(None, MessageModality.OTHER)])

    assert messages[0].content == NON_TEXT_PLACEHOLDER
    assert set(HistoryEntry.__dataclass_fields__) == {"direction", "modality", "text"}


def test_an_outbound_message_without_text_is_skipped():
    """It cannot exist today - every outbound row is reserved WITH its text -
    and skipping is the safe reading: a placeholder here would tell the model
    the clinic had sent a photo."""
    messages = to_chat_messages([inbound("hello"), outbound(None), inbound("still there?")])

    assert [m.role for m in messages] == ["user", "user"]


def test_blank_text_is_treated_as_no_text():
    assert content_for(MessageModality.TEXT, "   ") == NON_TEXT_PLACEHOLDER
    assert content_for(MessageModality.VOICE_NOTE, "") == VOICE_NOTE_PLACEHOLDER
    assert to_chat_messages([outbound("   ")]) == []
