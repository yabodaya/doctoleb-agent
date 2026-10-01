"""The rule that decides whether a transcript is a message at all (W5).

Pure functions, so these are the cheapest tests in the slice and the ones that
carry the most. The corpus is plan appendix A, which was run before a line of
the implementation existed.

The test that matters most is
`test_a_real_sentence_containing_a_silence_phrase_is_usable`. Everything else
here stops a non-message reaching the model; that one stops this helper eating
a real one.
"""

import ast
import pathlib

import pytest

from app.integrations.openai.transcripts import (
    MIN_TRANSCRIPT_CHARS,
    SILENCE_HALLUCINATIONS,
    normalise_transcript,
    unusable_reason,
)


@pytest.mark.parametrize("text", [None, "", " ", "   \n\t  ", " "])
def test_unusable_reason_on_empty_and_whitespace(text):
    assert unusable_reason(text) == "transcript_empty"


@pytest.mark.parametrize("text", [".", "...", "!?", "،", "؟", "-- --"])
def test_punctuation_only_is_empty_and_not_too_short(text):
    """Deliberate, and the reason code is the point.

    `"."` folds to nothing, so the code says what the model would have SEEN -
    nothing - rather than how many characters the API sent. A reader of a
    `voice_notes` row with `transcript_empty` knows there was no message; one
    with `transcript_too_short` would think there had been a syllable.
    """
    assert unusable_reason(text) == "transcript_empty"


@pytest.mark.parametrize("text", ["a", "o", "ل", "1", " x "])
def test_unusable_reason_on_one_character(text):
    assert unusable_reason(text) == "transcript_too_short"


@pytest.mark.parametrize("text", ["ok", "oui", "نعم", "لا", "aa", "hi", "yes", "نعم."])
def test_a_two_character_answer_is_usable(text):
    """The floor has to let a real "yes" and a real "no" through.

    "لا" is two characters and is a complete, unambiguous answer to a question
    the agent asks constantly. A floor of three would throw it away and ask the
    patient to repeat themselves - which is worse than useless, because they
    already said it clearly.
    """
    assert unusable_reason(text) is None


def test_the_floor_is_two_characters():
    assert MIN_TRANSCRIPT_CHARS == 2


@pytest.mark.parametrize("phrase", sorted(SILENCE_HALLUCINATIONS))
@pytest.mark.parametrize("dress", ["{}", "{}!", "  {}  ", "{}.", "{}?"])
def test_every_known_silence_output_is_unusable(phrase, dress):
    """Case and punctuation varied, because the model writes it like prose.

    These are not errors. They are confident, well-punctuated sentences a
    Whisper-family model emits when fed silence or noise, because it was
    trained on subtitled video where silence is paired with end-of-video
    subtitles. Answering one as though the patient had spoken is answering a
    machine.
    """
    assert unusable_reason(dress.format(phrase)) == "transcript_silence"
    assert unusable_reason(dress.format(phrase.upper())) == "transcript_silence"


@pytest.mark.parametrize(
    "text",
    [
        "Thank you, what time do you open tomorrow?",
        "thanks for watching my son yesterday, can we come again?",
        "ok thanks",
        "music lessons give me a headache, can I see the doctor?",
        "bye for now, but first can I move my appointment",
    ],
)
def test_a_real_sentence_containing_a_silence_phrase_is_usable(text):
    """The equality-not-substring rule, and the most important test here.

    A patient who really says "thank you" inside a ten-word sentence must be
    answered. Only a transcript that is NOTHING BUT a known phrase is a
    non-message - so the comparison is against the whole normalised string, and
    a substring test would make this helper eat real messages.
    """
    assert unusable_reason(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "مرحبا، بدي موعد بكرا الصبح",
        "Hello, I would like an appointment tomorrow morning",
        "bonjour, je voudrais un rendez-vous",
        "marhaba baddi maw3ed bukra",
    ],
)
def test_a_real_message_in_any_of_the_expected_languages_is_usable(text):
    """Arabic, English, French and Arabizi all reach the model.

    Arabizi is the hard case for the MODEL (plan section 4.3) but not for this
    function: it is just characters, and nothing here is language-specific
    except the English phrase list, which these do not match.
    """
    assert unusable_reason(text) is None


def test_the_api_reporting_no_audio_is_no_speech():
    """The only no-speech signal the current models give us.

    `whisper-1` reports a per-segment `no_speech_prob`, but that lives on
    `TranscriptionVerbose` and the newer models return JSON only (plan conflict
    C15), so it is not read. A reported duration of zero with words attached
    means the words did not come from the audio.
    """
    assert unusable_reason("appointment tomorrow", seconds=0) == "transcript_no_speech"
    assert unusable_reason("appointment tomorrow", seconds=-1) == "transcript_no_speech"
    assert unusable_reason("appointment tomorrow", seconds=0.5) is None
    assert unusable_reason("appointment tomorrow", seconds=None) is None


def test_the_cheaper_rules_win_over_no_speech():
    """Order matters: an empty transcript is empty whatever the duration says.

    `transcript_empty` is the more useful code, because it describes what the
    model would have seen rather than what the API claimed about the audio.
    """
    assert unusable_reason("", seconds=0) == "transcript_empty"
    assert unusable_reason("thank you", seconds=0) == "transcript_silence"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Thank   you", "thank you"),
        ("THANK YOU!", "thank you"),
        ("  bye.  ", "bye"),
        ("a\tb\nc", "a b c"),
        # A hyphen becomes a space, which is the point: "Thank-you" has to fold
        # to the same thing as "thank you" or the phrase list would miss it.
        ("Thank-you", "thank you"),
    ],
)
def test_normalise_collapses_whitespace_case_and_punctuation(text, expected):
    assert normalise_transcript(text) == expected


def test_arabic_diacritics_are_stripped_and_that_is_harmless():
    """A consequence worth pinning rather than discovering later.

    An Arabic vowel mark (a fatha, a damma) is a Unicode COMBINING MARK, which
    `\\w` does not match - so `[^\\w\\s]` strips it, and because punctuation
    becomes a SPACE, a fully vowelled word comes out as its letters separated.

    That is harmless for both things this function is used for, and neither is
    obvious, so both are asserted:

      1. the phrase list is English, so no Arabic string can match it either
         way - a vowelled Arabic message is usable before and after;
      2. the length floor still passes a real Arabic answer, because the
         letters survive and only the marks are replaced.

    What it must NOT do is turn a real Arabic message into a non-message, and
    that is what the second assertion is.
    """
    assert normalise_transcript("نَعَم") == "ن ع م"
    assert unusable_reason("نَعَم") is None
    assert unusable_reason("مَرْحَبا، بَدّي مَوعِد") is None


def test_normalise_is_nfc():
    """A transcript arrives from a third party, and the same Arabic or accented
    word can be composed or decomposed. Without NFC the phrase list would match
    one spelling and not the other."""
    decomposed = "é"  # e + combining acute
    composed = "é"

    assert normalise_transcript(decomposed) == normalise_transcript(composed)


def test_normalise_leaves_arabic_letters_alone():
    """The punctuation rule is `[^\\w\\s]` with re.UNICODE, so Arabic letters are
    word characters. A rule that stripped non-ASCII would delete the message."""
    assert normalise_transcript("مرحبا، بدي موعد") == "مرحبا بدي موعد"


def test_nothing_in_this_module_logs():
    """Every function in here is handed the patient's own words (hard rule 8).

    An AST check rather than a grep, so a `from logging import getLogger` is
    caught too. The module must be able to read a transcript and answer with a
    CODE, and the simplest guarantee of that is that it has nothing to log
    with.
    """
    source = pathlib.Path("app/integrations/openai/transcripts.py").read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert "logging" not in imported
    assert not any(name.startswith("app.") for name in imported)
    assert "print(" not in source
