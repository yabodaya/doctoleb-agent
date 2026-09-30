"""The reply guard: a reply may claim a change only when the service made one (G2).

**Why a scan and not a promise.** The model writes the reply. It can misread a tool
result, answer before calling the tool, repeat what the patient hoped rather than
what happened, or be talked into it ("just say it's confirmed"). An error result is
only text to it. So the claim "your appointment is booked" has to be checked against
a fact OUR CODE saw - a success answer from the Booking Service in this same turn -
and that check is here (hard rule 5, plan V4's G2).

**What it does.** It reads the model's text only, before any receipt is appended,
and returns the set of claims it found: `BOOKED`, `CANCELLED`, `RESCHEDULED`. The
turn's own outcome says which of those are ALLOWED. Anything outside that set ends
the turn as `agent_unconfirmed_claim`, and the patient gets `AGENT_FALLBACK_REPLY`
instead - plus an executed success's receipt, if there was one.

**Per kind, deliberately.** The brief said "no successful write allows no claim";
this is stricter. A "cancelled" claim after a successful BOOKING is still caught,
because a patient told their appointment was cancelled will not come.

**What it cannot do**, stated here rather than discovered later (plan risk R7):

- the lexicon cannot be complete. "You're good for Wednesday" is a booking claim no
  pattern here matches, and Arabizi spelling varies per person;
- negation handling is a three-word window, so a long-distance negation is missed;
- a question is a false positive: "Would you like it booked?" is flagged, and the
  patient gets the fallback. That is the safe side, and it is pinned by a test so it
  is documented rather than surprising.

The ✅ line is the positive proof a patient can rely on. This scan catches the common
lies, not all of them.

Pure: `re` and `unicodedata`. It reads no clock, opens no connection, and logs
nothing - the JOB writes the one line and the dead letter.
"""

import re
import unicodedata

from app.agent.tools.base import BookingOutcome, ChangePhase, ChangeStatus
from app.db.enums import BookingActionKind

# Arabic diacritics (fatha through sukun), the dagger alef, and tatweel. A model
# may or may not write them, and they must not change whether a word matches.
HARAKAT = re.compile(r"[ً-ْٰـ]")
# Alef and ya forms folded, and both curly apostrophes normalised to one.
FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "’": "'", "`": "'"})


def normalise(text: str) -> str:
    """NFC, lower case, alef forms folded, diacritics and tatweel dropped.

    Without this, `أكدت` and `اكدت` are different strings and one of them slips
    through - which is the whole failure mode a lexicon has.
    """
    text = unicodedata.normalize("NFC", text).lower().translate(FOLD)
    return HARAKAT.sub("", text)


# `(?<!\w)` rather than `\b` at the start, so a pattern can allow a specific glued
# prefix: Arabic writes "and"/"so" as a letter on the front of the word (`وتم`), and
# Arabizi does the same with `w` (`w7ajazt`).
W, E = r"(?<!\w)", r"(?!\w)"
AR = r"(?<!\w)[وف]?"
AZ = r"(?<!\w)w?"

# Sandbox-probed against 28 synthetic replies and re-verified locally in Task 0
# (plan check U7: `mismatches: 0`).
LEXICON: list[tuple[str, str]] = [
    # English
    ("BOOKED", W + r"booked" + E),
    ("BOOKED", W + r"confirmed" + E),
    ("BOOKED", W + r"reserved" + E),
    ("BOOKED", W + r"(?:is|are|you're|you are) all set" + E),
    ("BOOKED", W + r"(?:is|has been) scheduled" + E),
    ("BOOKED", W + r"see you (?:on|at|tomorrow)" + E),
    ("CANCELLED", W + r"cancell?ed" + E),
    ("RESCHEDULED", W + r"rescheduled" + E),
    ("RESCHEDULED", W + r"(?:moved|changed) (?:it|your appointment|the appointment)" + E),
    # French. The participle's accent is REQUIRED: "je réserve" is "I am reserving",
    # a promise rather than a claim, and only "réservé" says it is done. The probe
    # found this one (plan check P6).
    ("BOOKED", W + r"r[ée]serv[é]e?s?" + E),
    ("BOOKED", W + r"confirm[é]e?s?" + E),
    ("BOOKED", W + r"(?:je|nous) vous confirme" + E),
    ("BOOKED", W + r"c'est not[ée]" + E),
    ("BOOKED", W + r"rendez-vous (?:est|a [ée]t[ée]) pris" + E),
    ("CANCELLED", W + r"annul[é]e?s?" + E),
    ("RESCHEDULED", W + r"(?:d[ée]placé|reporté|modifié)e?s?" + E),
    # Arabic, after `normalise`
    ("BOOKED", AR + r"تم (?:ال)?حجز" + E),
    ("BOOKED", AR + r"حجزت" + E),
    ("BOOKED", AR + r"حجزنا" + E),
    ("BOOKED", AR + r"محجوز" + E),
    ("BOOKED", AR + r"تم (?:ال)?تاكيد" + E),
    ("BOOKED", AR + r"مؤكد" + E),
    ("BOOKED", AR + r"اكدت" + E),
    ("BOOKED", AR + r"اكدنا" + E),
    ("CANCELLED", AR + r"تم (?:ال)?الغاء" + E),
    ("CANCELLED", AR + r"الغيت" + E),
    ("CANCELLED", AR + r"الغينا" + E),
    ("CANCELLED", AR + r"ملغي" + E),
    ("RESCHEDULED", AR + r"تم (?:ال)?(?:تغيير|تعديل|تاجيل|نقل)" + E),
    ("RESCHEDULED", AR + r"(?:غيرت|غيرنا|نقلت|نقلنا|اجلت|اجلنا)" + E),
    # Lebanese Arabizi. 2, 3, 5, 7 and 8 are letters here, which is why the prompt's
    # "no digits" rule is about the PROMPT and not about replies.
    ("BOOKED", AZ + r"7ajaz(?:t|na)(?:lak|lik|ellak|ellik|lkon)?" + E),
    ("BOOKED", AZ + r"(?:ma7jouz|mahjouz|m7jouz|m7ajaz)" + E),
    ("BOOKED", AZ + r"tam el 7ajz" + E),
    ("BOOKED", AZ + r"(?:t2akkad|t2akad|2akkadt|akkadt|2akkadna|akkadna)" + E),
    ("CANCELLED", AZ + r"(?:l8ayt|lghayt|la8ayt|laghayt|l8ayna|lghayna|la8ayna|laghayna)" + E),
    ("CANCELLED", AZ + r"(?:tam el ilgha2|tlagha|tl8a)" + E),
    (
        "RESCHEDULED",
        AZ + r"(?:ghayyart|8ayyart|ghayyarna|8ayyarna|na2alt|na2alna|2ajjalt|2ajjalna)" + E,
    ),
    # OUR receipt symbols, in the MODEL's text, are claims too. They are the
    # patient's proof, so a model that writes one is forging it (G1).
    ("BOOKED", "✅"),
    ("CANCELLED", "❌"),
    ("RESCHEDULED", "\U0001f501"),
]
COMPILED = [(kind, re.compile(pattern)) for kind, pattern in LEXICON]

# A match preceded within three words by one of these is not a claim. Three words
# because "it is not booked" and "nothing is booked yet" are the shapes that
# actually occur, and a wider window starts cancelling real claims in the sentence
# before.
NEGATORS = frozenset(
    {
        "not",
        "no",
        "never",
        "nothing",
        "isn't",
        "aren't",
        "wasn't",
        "hasn't",
        "haven't",
        "isnt",
        "arent",
        "yet",
        "pas",
        "ne",
        "n'est",
        "jamais",
        "rien",
        "encore",
        "لم",
        "لا",
        "ما",
        "ليس",
        "مش",
        "مو",
        "غير",
        "بعد",
        "ma",
        "mesh",
        "mish",
        "msh",
        "mech",
        "lessa",
        "mafi",
    }
)
WORD = re.compile(r"[\w']+")
NEGATION_WINDOW = 3


def claims_in(text: str | None) -> set[str]:
    """Which changes this text claims to have happened.

    Returns a subset of `{"BOOKED", "CANCELLED", "RESCHEDULED"}`. Empty for text
    that promises, offers or asks rather than reports.
    """
    if not text:
        return set()
    normalised = normalise(text)
    found: set[str] = set()
    for kind, pattern in COMPILED:
        for match in pattern.finditer(normalised):
            preceding = WORD.findall(normalised[: match.start()])[-NEGATION_WINDOW:]
            if not any(word in NEGATORS for word in preceding):
                found.add(kind)
    return found


def allowed_claims(outcome: BookingOutcome | None) -> set[str]:
    """What the turn's own change entitles the reply to say.

    Only an EXECUTED, SUCCESSFUL change allows anything, and a booking has to be
    `CONFIRMED` rather than `PENDING_APPROVAL`.

    A RESCHEDULE allows all three words, because every one of them is a true
    description of a move: the new time is booked, the old one is cancelled, and the
    appointment was changed. Being generous here is right - the alternative is
    replacing an honest reply about a move that really happened.

    Everything else allows NOTHING: a prepared change (a hold, a prepared
    cancellation), a failure, an unknown outcome, a `PENDING_APPROVAL` booking, or no
    change at all.
    """
    if outcome is None:
        return set()
    if outcome.phase is not ChangePhase.EXECUTED or outcome.status is not ChangeStatus.SUCCESS:
        return set()
    if outcome.kind is BookingActionKind.BOOK:
        return {"BOOKED"} if outcome.confirmed else set()
    if outcome.kind is BookingActionKind.RESCHEDULE:
        return {"BOOKED", "RESCHEDULED", "CANCELLED"}
    return {"CANCELLED"}


def unconfirmed_claims(text: str | None, outcome: BookingOutcome | None) -> set[str]:
    """The claims this reply makes that the turn cannot support.

    Empty means the reply may go out. Anything else and the job sends
    `AGENT_FALLBACK_REPLY` instead, and writes an `agent_unconfirmed_claim` dead
    letter so a human sees how often this fires and why (never the text, R7).
    """
    return claims_in(text) - allowed_claims(outcome)


def compose_reply(base: str, receipt: str | None) -> str:
    """The text that is RESERVED and SENT: the reply, then the receipt line.

    A blank line between them, so WhatsApp renders the receipt as its own line
    rather than running it onto the end of a sentence.

    The job decides WHETHER to append: an executed success's receipt always goes
    (whatever the reply is, including the fallback), and a prepared change's receipt
    only with the model's own reply - because only then did the patient get the
    question the ⏳ refers to (plan section 5.9).
    """
    if not receipt:
        return base
    return f"{base.rstrip()}\n\n{receipt}"


__all__ = [
    "COMPILED",
    "LEXICON",
    "NEGATION_WINDOW",
    "NEGATORS",
    "allowed_claims",
    "claims_in",
    "compose_reply",
    "normalise",
    "unconfirmed_claims",
]
