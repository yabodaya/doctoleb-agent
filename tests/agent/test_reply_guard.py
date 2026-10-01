"""The reply guard (G2, plan section 5.9).

Pure: no database, no network. The corpus is Appendix A's, which was sandbox-probed
and re-verified locally in Task 0 (check U7).

What is being tested is a judgement, so the tests come in three groups:

- **the corpus**: does the scan find a claim where a claim is, in four languages;
- **the verdict**: does the turn's own outcome allow it;
- **the known misses**: the three cases that go the wrong way are PINNED, so they are
  documented rather than discovered in production (plan risk R7).
"""

import uuid

import pytest

from app.agent.core import AgentRuntime, Turn, process_turn
from app.agent.guard import (
    allowed_claims,
    claims_in,
    compose_reply,
    normalise,
    unconfirmed_claims,
)
from app.agent.tools import (
    BookingOutcome,
    ChangePhase,
    ChangeStatus,
    ToolRegistry,
)
from app.db.enums import BookingActionKind, MessageModality
from app.integrations.booking.fake import FakeBookingClient
from app.integrations.booking.memory import InMemoryBookingService
from tests.agent.test_booking_loop import NOW, PHONE, StubChange, StubRead
from tests.integrations.booking_fakes import counter_ids
from tests.integrations.fakes import FakeChatClient, ok, tool_call, wants_tools

# --- Appendix A's corpus, grouped by language --------------------------------

ENGLISH = [
    ("Your appointment with Dr. Karim is booked for Wednesday at 14:00.", {"BOOKED"}),
    ("I've put 14:00 on hold for you. It is not booked yet: shall I book it?", set()),
    ("Nothing is booked yet. Would you like me to book it?", set()),
    ("Should I confirm it for you?", set()),
    ("It isn't confirmed until you reply yes.", set()),
    ("You're all set, see you on Wednesday!", {"BOOKED"}),
    ("Your appointment has been cancelled.", {"CANCELLED"}),
    ("I have not cancelled anything.", set()),
    ("Your appointment was rescheduled to Thursday.", {"RESCHEDULED"}),
    ("Hope to see you soon!", set()),
    ("The time was taken by someone else, here are other times: 14:20 or 15:40.", set()),
    ("Dr. Karim has 14:00 free tomorrow afternoon.", set()),
]

FRENCH = [
    ("Votre rendez-vous est réservé pour mercredi.", {"BOOKED"}),
    ("Le créneau n'est pas encore réservé : voulez-vous que je le réserve ?", set()),
    ("Je vous confirme le rendez-vous de mercredi.", {"BOOKED"}),
    ("Votre rendez-vous a été annulé.", {"CANCELLED"}),
]

ARABIC = [
    ("تم حجز موعدك مع الدكتور كريم يوم الأربعاء.", {"BOOKED"}),
    ("وتم الحجز بنجاح", {"BOOKED"}),
    ("لم يتم الحجز بعد، هل تريد أن أحجز لك؟", set()),
    ("الموعد غير مؤكد بعد.", set()),
    ("تم إلغاء موعدك.", {"CANCELLED"}),
    ("ألغيت الموعد", {"CANCELLED"}),
]

ARABIZI = [
    ("7ajaztlak ma3 Dr Karim nhar el arb3a", {"BOOKED"}),
    ("ma 7ajazt ba3d, badak 7ejzo?", set()),
    ("lessa ma t2akkad el maw3ad", set()),
    ("tam el 7ajz, mnshoufak l arb3a", {"BOOKED"}),
    ("l8ayt el maw3ad", {"CANCELLED"}),
]

SYMBOLS = [("✅ Dr. Karim Haddad · 2026-09-30 14:00", {"BOOKED"})]

CORPUS = [
    *[(t, w, "english") for t, w in ENGLISH],
    *[(t, w, "french") for t, w in FRENCH],
    *[(t, w, "arabic") for t, w in ARABIC],
    *[(t, w, "arabizi") for t, w in ARABIZI],
    *[(t, w, "symbols") for t, w in SYMBOLS],
]


@pytest.mark.parametrize(
    ("text", "expected", "language"),
    CORPUS,
    ids=[f"{language}: {text[:34]}" for text, _, language in CORPUS],
)
def test_the_claims_corpus(text, expected, language):
    """Appendix A, case by case. 28 replies in four languages.

    The interesting half is the empty expectations: a reply that OFFERS to book, or
    says it is not booked yet, or lists other times, must not be flagged - or the
    guard would replace every normal booking conversation with the fallback.
    """
    assert claims_in(text) == expected, language


def test_the_corpus_is_appendix_as_twenty_eight_cases():
    """A guard rail on the guard rail: if a case is ever dropped while refactoring
    this file, the count says so."""
    assert len(CORPUS) == 28


@pytest.mark.parametrize(
    ("text", "found", "why"),
    [
        (
            "Would you like it booked?",
            {"BOOKED"},
            "A question, flagged as a claim. The patient gets the fallback instead of "
            "a perfectly good question: the safe side of a scan that cannot parse "
            "grammar.",
        ),
        (
            "Your booking is done.",
            set(),
            "A booking claim no pattern matches: 'booking' as a noun with 'done'. "
            "The missing ✅ underneath is then the only tell.",
        ),
        (
            "5alas, mnshoufak l arb3a",
            set(),
            "Arabizi for 'done, see you Wednesday'. Idiomatic, and not in the "
            "lexicon: the reason the lexicon is a follow-up rather than a finished "
            "thing (plan follow-up 6).",
        ),
    ],
    ids=["false positive", "false negative (english)", "false negative (arabizi)"],
)
def test_the_known_misses_are_pinned(text, found, why):
    """Three cases that go the wrong way, PINNED so they are documented.

    Pinning a known miss is not accepting it: it is the difference between a limit
    somebody chose and a bug nobody noticed. The `why` is asserted to be written
    down, because a pin with no reason is just a passing test.
    """
    assert claims_in(text) == found
    assert len(why) > 40


def test_a_negator_within_three_words_cancels_a_claim():
    """Three words because "it is not booked" and "nothing is booked yet" are the
    shapes that actually occur. A wider window starts cancelling real claims from
    the sentence before."""
    assert claims_in("it is not booked") == set()
    assert claims_in("nothing is booked yet") == set()
    # Four words away: outside the window, so it counts as a claim. The safe side.
    assert claims_in("no, I really do think it is booked") == {"BOOKED"}


def test_our_receipt_symbols_in_model_text_are_claims():
    """The symbols are the patient's proof, so a model that writes one is forging
    it. Each maps to its own kind, so a forged ❌ after a booking is caught too."""
    assert claims_in("✅") == {"BOOKED"}
    assert claims_in("❌") == {"CANCELLED"}
    assert claims_in("🔁") == {"RESCHEDULED"}
    assert claims_in("⏳ nothing yet") == set()  # the waiting symbol is not a claim


def test_arabic_normalisation_folds_alef_and_drops_diacritics():
    """`أكدت` and `اكدت` are different strings, and a lexicon that saw only one of
    them would let the other through."""
    assert normalise("أَكَّدْتُ") == normalise("اكدت")
    assert claims_in("أكدت الموعد") == {"BOOKED"}
    assert claims_in("اكدت الموعد") == {"BOOKED"}
    assert normalise("ىـ") == "ي"


def test_a_french_participle_needs_its_accent():
    """ "je réserve" is a promise; "réservé" is a claim. The probe found this (P6),
    and without it every offer to book in French would get the fallback."""
    assert claims_in("je le réserve pour vous ?") == set()
    assert claims_in("c'est réservé") == {"BOOKED"}


# --- the verdict -------------------------------------------------------------


def outcome(
    kind: BookingActionKind,
    phase: ChangePhase = ChangePhase.EXECUTED,
    status: ChangeStatus = ChangeStatus.SUCCESS,
    *,
    confirmed: bool = True,
) -> BookingOutcome:
    return BookingOutcome(
        kind=kind, phase=phase, status=status, confirmed=confirmed, action_id=uuid.uuid4()
    )


@pytest.mark.parametrize(
    ("given", "allowed"),
    [
        (None, set()),
        (outcome(BookingActionKind.BOOK), {"BOOKED"}),
        (outcome(BookingActionKind.BOOK, confirmed=False), set()),
        (
            outcome(BookingActionKind.RESCHEDULE),
            {"BOOKED", "RESCHEDULED", "CANCELLED"},
        ),
        (outcome(BookingActionKind.CANCEL), {"CANCELLED"}),
        (outcome(BookingActionKind.BOOK, phase=ChangePhase.PROPOSED), set()),
        (outcome(BookingActionKind.BOOK, status=ChangeStatus.FAILED), set()),
        (outcome(BookingActionKind.BOOK, status=ChangeStatus.UNCERTAIN), set()),
    ],
    ids=[
        "no change",
        "booked",
        "booked but awaiting approval",
        "moved",
        "cancelled",
        "a hold",
        "a failure",
        "an unknown outcome",
    ],
)
def test_allowed_claims_follow_the_turns_own_outcome(given, allowed):
    """Plan section 5.9's list. Only an EXECUTED, SUCCESSFUL change allows anything.

    A RESCHEDULE allows all three words because each is a true description of a move:
    the new time is booked, the old is cancelled, the appointment changed. Being
    generous there is right - the alternative is replacing an honest reply.

    `PENDING_APPROVAL` allows NOTHING, which is the case most likely to be got wrong
    by hand: the service accepted the request, and the clinic has not agreed.
    """
    assert allowed_claims(given) == allowed


def test_a_booking_allows_booked_but_not_cancelled():
    """Per kind, stricter than the brief's "no successful write". A patient told
    their appointment was cancelled will not come."""
    booked = outcome(BookingActionKind.BOOK)

    assert unconfirmed_claims("It's booked!", booked) == set()
    assert unconfirmed_claims("It's booked, and the old one is cancelled.", booked) == {"CANCELLED"}


def test_a_hold_allows_no_claim():
    """The commonest real failure: "I've reserved it for you" after a HOLD. True in
    English, false to a patient - and this is what catches it."""
    hold = outcome(BookingActionKind.BOOK, phase=ChangePhase.PROPOSED)

    assert unconfirmed_claims("I've reserved it for you.", hold) == {"BOOKED"}
    assert unconfirmed_claims("I've put it on hold, shall I book it?", hold) == set()


def test_an_unknown_outcome_allows_no_claim_in_either_direction():
    """V6. The model is told to claim neither; if it claims success anyway, the
    guard catches it. (A claim that it FAILED is not in the lexicon - there is
    nothing to match - and the fallback says nothing either way.)"""
    unknown = outcome(BookingActionKind.BOOK, status=ChangeStatus.UNCERTAIN)

    assert unconfirmed_claims("It's booked!", unknown) == {"BOOKED"}
    assert unconfirmed_claims("The clinic team will check and get back to you.", unknown) == set()


# --- compose_reply ------------------------------------------------------------


def test_compose_reply_appends_one_receipt_after_a_blank_line():
    """A blank line, so WhatsApp renders the receipt on its own line rather than
    running it onto the end of a sentence."""
    assert compose_reply("Done, it's booked.", "✅ Dr. Karim · 2026-09-30 14:00") == (
        "Done, it's booked.\n\n✅ Dr. Karim · 2026-09-30 14:00"
    )
    # Trailing whitespace in the model's text never produces three newlines.
    assert compose_reply("Done.  \n\n", "✅ x") == "Done.\n\n✅ x"
    assert compose_reply("Done.", None) == "Done."
    assert compose_reply("Done.", "") == "Done."


# --- through process_turn -----------------------------------------------------


def turn(**overrides) -> Turn:
    values = {
        "tenant_id": "clinic-alpha",
        "contact_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "modality": MessageModality.TEXT,
        "input_text": "is 14:00 free?",
        "inbox_event_id": uuid.UUID("11111111-2222-4333-8444-555555555555"),
        "inbound_message_id": uuid.UUID("22222222-3333-4444-8555-666666666666"),
        "patient_reference": PHONE,
    }
    values.update(overrides)
    return Turn(**values)


def runtime(tools: ToolRegistry) -> AgentRuntime:
    bookings = InMemoryBookingService(
        FakeBookingClient.demo(clock=lambda: NOW),
        lambda: NOW,
        id_secret=b"a fixed secret for the guard tests",
        new_id=counter_ids(),
    )
    return AgentRuntime(
        booking=bookings,
        clock=lambda: NOW,
        turn_timeout_seconds=30.0,
        registry=tools,
        patient_bookings=bookings,
    )


async def test_a_claim_without_a_success_ends_the_turn_as_an_unconfirmed_claim():
    """The whole point, at the turn's level. The model called no changing tool and
    said it was confirmed anyway - which is the commonest failure of all, and the one
    a "no successful write" rule is exactly right about."""
    chat = FakeChatClient(ok("Your appointment is confirmed!"))

    result = await process_turn(turn(), chat, runtime(ToolRegistry((StubRead(),))))

    assert result.outcome.value == "PERMANENT"
    assert result.reason == "agent_unconfirmed_claim"
    # No reply text: the job sends AGENT_FALLBACK_REPLY instead.
    assert result.reply_text is None


async def test_an_honest_reply_goes_out_untouched():
    chat = FakeChatClient(ok("Dr. Karim has 14:00 and 14:20 free tomorrow."))

    result = await process_turn(turn(), chat, runtime(ToolRegistry((StubRead(),))))

    assert result.outcome.value == "SUCCESS"
    assert result.reply_text == "Dr. Karim has 14:00 and 14:20 free tomorrow."


async def test_a_claim_after_a_real_booking_is_allowed():
    """The guard must not replace a TRUE reply. A turn that really booked something
    may say so."""
    stub = StubChange(
        BookingOutcome(
            kind=BookingActionKind.BOOK,
            phase=ChangePhase.EXECUTED,
            status=ChangeStatus.SUCCESS,
            confirmed=True,
            receipt="✅ Dr. Karim Haddad · 2026-09-30 14:00 · #K7Q2M9",
        ),
        gate=True,
    )
    from app.agent.tools import BookingState
    from app.db.enums import BookingActionStatus

    state = BookingState(
        action_id=uuid.uuid4(),
        kind=BookingActionKind.BOOK,
        status=BookingActionStatus.PENDING,
        confirmable=True,
        hold_id="hold_1",
    )
    chat = FakeChatClient(wants_tools(tool_call("stub_change", {})), ok("Done, it's booked."))

    result = await process_turn(turn(booking_state=state), chat, runtime(ToolRegistry((stub,))))

    assert result.outcome.value == "SUCCESS"
    assert result.reply_text == "Done, it's booked."


async def test_the_outcome_survives_when_the_guard_fires():
    """The change really happened and still has to be recorded and receipted.

    Dropping the outcome here would leave `booking_actions` not knowing about a
    booking the Booking Service made - which is worse than the wrong wording the
    guard just prevented.
    """
    stub = StubChange(
        BookingOutcome(
            kind=BookingActionKind.CANCEL,
            phase=ChangePhase.EXECUTED,
            status=ChangeStatus.SUCCESS,
            confirmed=True,
            appointment_id="apt_1",
            receipt="❌ Dr. Karim Haddad · 2026-09-30 14:00 · #K7Q2M9",
        ),
        gate=True,
    )
    from app.agent.tools import BookingState
    from app.db.enums import BookingActionStatus

    state = BookingState(
        action_id=uuid.uuid4(),
        kind=BookingActionKind.CANCEL,
        status=BookingActionStatus.PENDING,
        confirmable=True,
        hold_id=None,
        appointment_id="apt_1",
    )
    # It cancelled something, and then claimed it was BOOKED.
    chat = FakeChatClient(
        wants_tools(tool_call("stub_change", {})), ok("Your appointment is booked!")
    )

    result = await process_turn(turn(booking_state=state), chat, runtime(ToolRegistry((stub,))))

    assert result.reason == "agent_unconfirmed_claim"
    assert result.booking_outcome is not None
    assert result.booking_outcome.receipt is not None
    assert result.tool_calls  # and the tool rows survive too


def test_the_guard_keeps_no_text_in_any_repr():
    """Hard rule 8. The guard sees the model's words, which can quote the patient,
    and it must keep none of them: it returns a set of three fixed strings."""
    found = claims_in("SENTINEL-patient-words: your appointment is booked")

    assert found == {"BOOKED"}
    assert "SENTINEL" not in repr(found)
    # And nothing in the module holds state at all: the lexicon is compiled
    # patterns, and `claims_in` is a pure function.
    import app.agent.guard as guard

    assert "SENTINEL" not in repr(guard.COMPILED)
    assert all(isinstance(kind, str) for kind, _ in guard.LEXICON)
