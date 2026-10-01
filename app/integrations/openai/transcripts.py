"""Is this transcript a message, or is it nothing? (VS-008, W5)

Pure: no SDK, no settings, no session, and **no logging at all** - a test
asserts this module imports `logging` nowhere, because every function in here
is handed the patient's own words and must answer with a CODE.

Why this exists rather than "send whatever came back to the model".

Automatic speech recognition does not return nothing when there is nothing to
hear. Whisper-family models were trained on subtitled video, where long silences
are paired with end-of-video subtitle text, so fed silence or noise they emit
memorised phrases - "Thank you for watching!", "Please subscribe", "Subtitles by
the Amara.org community" - as confident, well-punctuated sentences. One
published analysis puts "thank you" in roughly a quarter of hallucinations.

A clinic receptionist that answers a silent voice note as though the patient had
spoken is answering a machine, not a person. And no prompt can fix it: the model
cannot tell a hallucinated "thank you" from a real one. So the check is code,
it runs BEFORE the model is called, and an unusable transcript gets a fixed
code-owned reply asking the patient to repeat or type.
"""

import re
import unicodedata

# Two characters, because "ok", "لا" and "نعم" are all real answers to a real
# question, and a stray "." is not. Below this there is nothing a model could
# usefully act on.
MIN_TRANSCRIPT_CHARS = 2

# Known silence outputs of the Whisper family (plan P8). Normalised, compared
# for EQUALITY against the WHOLE transcript, never as a substring: a patient
# who really says "thank you" in a ten-word sentence must be answered, and only
# a transcript that is NOTHING BUT one of these is a non-message.
#
# Plan check U5 is whether the CHOSEN model hallucinates these or others. A
# phrase observed in the live test is added here, with a test case - it is not
# the prompt, so nothing is versioned by it.
SILENCE_HALLUCINATIONS: frozenset[str] = frozenset(
    {
        "thank you",
        "thanks",
        "thank you for watching",
        "thanks for watching",
        "please subscribe",
        "subscribe",
        "subtitles by the amara org community",
        "subtitles by the amaraorg community",
        "you",
        "bye",
        "okay",
        "music",
        "applause",
        "foreign",
    }
)

# Anything that is not a word character or whitespace, in ANY script. Arabic
# LETTERS are word characters to `re.UNICODE`, so this strips punctuation and
# leaves the language alone.
#
# It does also strip Arabic vowel MARKS, which are combining marks and not word
# characters - so a fully vowelled word folds to its letters separated by
# spaces. Harmless for both things this module does, and pinned by a test:
# SILENCE_HALLUCINATIONS is an English list that no Arabic string matches
# either way, and the length floor still passes a real Arabic answer because
# the letters survive.
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalise_transcript(text: str) -> str:
    """NFC, case-folded, punctuation stripped, whitespace collapsed.

    Its own tiny implementation rather than `app/agent/guard.py`'s `normalise`,
    for two reasons: `app/integrations/` must not import `app/agent/`, and the
    two are answering different questions - that one folds Arabic orthography
    for a claim lexicon, this one compares against an English phrase list.

    NFC first, because a transcript arrives from a third party and the same
    Arabic word can be composed or decomposed. `casefold` rather than `lower`,
    because it is the right answer for more scripts.
    """
    folded = unicodedata.normalize("NFC", text).casefold()
    folded = _PUNCTUATION.sub(" ", folded)
    return " ".join(folded.split())


def unusable_reason(text: str | None, *, seconds: float | None = None) -> str | None:
    """Why this transcript must not reach the model, or None if it may.

        transcript_empty      nothing, only whitespace, or only punctuation;
        transcript_too_short  fewer than MIN_TRANSCRIPT_CHARS characters after
                              normalisation - "ok", "لا" and "نعم" all pass, a
                              stray "." does not;
        transcript_silence    the whole transcript is a known silence output
                              (plan P8, check U5);
        transcript_no_speech  the API itself reported there was nothing there.

    It reads the patient's words and returns a CODE. Nothing it is given is
    logged, stored or put in an exception message (hard rule 8).

    On `"."`: it folds to `transcript_empty` rather than `transcript_too_short`,
    deliberately and pinned by a test. The reason code says what the model would
    have SEEN - nothing - not how many characters the API sent.
    """
    if text is None or not text.strip():
        return "transcript_empty"
    folded = normalise_transcript(text)
    if not folded:
        return "transcript_empty"
    if len(folded) < MIN_TRANSCRIPT_CHARS:
        return "transcript_too_short"
    if folded in SILENCE_HALLUCINATIONS:
        return "transcript_silence"
    if seconds is not None and seconds <= 0:
        # The only no-speech signal the current models give us. `whisper-1`
        # reports a per-segment `no_speech_prob`, but that is on
        # `TranscriptionVerbose` and the newer models return JSON only (plan
        # conflict C15), so it is not read. A reported duration of zero with
        # words attached means the words did not come from the audio.
        return "transcript_no_speech"
    return None
