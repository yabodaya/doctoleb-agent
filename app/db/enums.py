"""Vocabularies stored in the database.

These are VARCHAR + CHECK, not native PostgreSQL ENUM types. MessageModality
gains VOICE_NOTE in VS-008 and MessageStatus grows as VS-004 handles Meta status
callbacks; ALTER TYPE ... ADD VALUE has no reverse operation, which would make
`alembic downgrade` a lie. Swapping a CHECK constraint is fully reversible.

The .value strings are the literal contents of database rows. Renaming one is a
data migration, not a refactor.
"""

from enum import StrEnum

import sqlalchemy as sa


class Channel(StrEnum):
    WHATSAPP = "whatsapp"


class ConversationState(StrEnum):
    """docs/architecture.md: AI_ACTIVE -> HUMAN_REQUESTED -> HUMAN_ACTIVE -> CLOSED."""

    AI_ACTIVE = "AI_ACTIVE"
    HUMAN_REQUESTED = "HUMAN_REQUESTED"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    CLOSED = "CLOSED"


class MessageDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageModality(StrEnum):
    TEXT = "TEXT"
    VOICE_NOTE = "VOICE_NOTE"
    # Anything else Meta can send: image, document, location, sticker, contact,
    # interactive reply. VS-004 stores every inbound type, and inventing a value
    # per type would be a CHECK migration per Meta feature. The raw type stays
    # readable in webhook_inbox.payload.
    OTHER = "OTHER"


class MessageStatus(StrEnum):
    RECEIVED = "RECEIVED"  # inbound, stored
    QUEUED = "QUEUED"  # outbound, not yet handed to Meta
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


# How far along a message is. A status callback only ever moves a message to a
# HIGHER rank (VS-004 requirement 5), enforced in the UPDATE's WHERE clause so
# there is no read-then-write race between two callbacks.
#
# FAILED sits above SENT and below DELIVERED on purpose: a send Meta later
# reports as failed must overwrite SENT, while a message that was delivered did
# not fail. RECEIVED and QUEUED share rank 0 - neither is ever advanced by a
# status callback. RECEIVED is inbound-only, and QUEUED becomes SENT by the send
# itself, not by a callback.
STATUS_RANK: dict[MessageStatus, int] = {
    MessageStatus.RECEIVED: 0,
    MessageStatus.QUEUED: 0,
    MessageStatus.SENT: 1,
    MessageStatus.FAILED: 2,
    MessageStatus.DELIVERED: 3,
    MessageStatus.READ: 4,
}


class InboxStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


def check_constraint(column: str, enum_cls: type[StrEnum], name: str) -> sa.CheckConstraint:
    """Build a named CHECK restricting `column` to `enum_cls`'s values.

    Named explicitly: an anonymous CHECK gets a random PostgreSQL name, which
    makes autogenerate noisy and makes it impossible to report a violated
    constraint by name instead of by conflicting value.

    Alembic's autogenerate never compares CHECK constraints, so changing an enum
    here does NOT produce a migration on its own. Widening one is a hand-written
    migration plus a new value in the runtime test in tests/db/test_constraints.py.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name)


class AgentRunOutcome(StrEnum):
    """How one generated turn ended (VS-006).

    Deliberately the same three values as `ChatOutcome` in
    `app/integrations/openai/interface.py`, and a test keeps them equal. They
    are a separate enum rather than an import because `app/db/` must not depend
    on the integrations layer, and because these strings are row contents: if
    ChatOutcome ever grows a value, that is a CHECK migration, and the test is
    what makes it a decision instead of a surprise.
    """

    SUCCESS = "SUCCESS"
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"


class ToolExecutionStatus(StrEnum):
    """What happened to one tool call the model asked for (VS-006).

    Every call the model made gets a row, executed or not, which is why SKIPPED
    exists: calls arriving on the last allowed model response, or past the
    per-turn cap, are recorded and not run.

    VS-007 adds two, and the CHECK constraint widens with them (migration
    b919820bf52e). `app/agent/tools/base.py` mirrors this enum and a test keeps
    the two equal, so they must always be added together.
    """

    OK = "OK"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"  # Pydantic refused them, or they were not JSON
    UNKNOWN_TOOL = "UNKNOWN_TOOL"  # the model named a tool that is not registered
    ERROR = "ERROR"  # the tool ran and failed (booking error, crash, deadline)
    SKIPPED = "SKIPPED"  # recorded but never executed
    # VS-007, V6. A booking-changing call whose outcome is UNKNOWN: it timed out,
    # lost its connection, or was cut off by the turn deadline. Deliberately not
    # ERROR: an error means "it did not happen", and that is the one thing we
    # cannot say here (hard rule 5).
    UNCERTAIN = "UNCERTAIN"
    # VS-007, V3/V12/V15. OUR code declined to run it: the confirmation gate, the
    # one-change-per-message rule, or too little turn budget left. Not the model's
    # mistake and not the service's - so neither SKIPPED nor ERROR would be true.
    REFUSED = "REFUSED"


class VoiceNoteStatus(StrEnum):
    """How far one voice note got (VS-008).

    PENDING   an attempt started and did not finish: the job died, or the media
              or transcription step failed RETRYABLY. The ONE state a retry
              re-attempts.
    DONE      transcribed; `messages.text` holds the transcript. A retry reads
              this and skips the whole voice step, so nobody pays twice - which
              matters more here than anywhere else in the repo, because
              transcription is the one step that costs money per attempt and
              cannot be made idempotent by a key.
    UNCLEAR   transcribed to nothing usable (empty, too short, a known silence
              output, or flagged no-speech). The patient was asked to repeat or
              type, and the model was never called.
    FAILED    permanently: no media id, an expired id, a rejected URL, an
              oversized or unsupported file, no model configured, a 4xx from
              the audio endpoint. The patient was told to type instead.

    UNCLEAR, DONE and FAILED are all terminal for the message: the patient has
    been answered, so no retry reaches them.
    """

    PENDING = "PENDING"
    DONE = "DONE"
    UNCLEAR = "UNCLEAR"
    FAILED = "FAILED"


class BookingActionKind(StrEnum):
    """Which kind of change a `booking_actions` row is about (VS-007, V2).

    Imported from here by `app/agent/tools/base.py`, exactly as `MessageModality`
    already is: a vocabulary is not database access, and the forbidden-import test
    allows `app.db.enums` for that reason. Duplicating it a second time (as
    `ToolExecutionStatus` had to be, because it is a STATUS the agent produces)
    would be two places to forget.
    """

    BOOK = "BOOK"
    RESCHEDULE = "RESCHEDULE"
    CANCEL = "CANCEL"


class BookingActionStatus(StrEnum):
    """How far a prepared change got (VS-007, V2).

    PENDING     prepared - a hold, or a prepared cancellation - and waiting for
                the patient. The ONLY status that can be confirmed, and only
                under V3's gate.
    DONE        executed; the Booking Service said success.
    FAILED      executing it failed definitively: HOLD_EXPIRED, SLOT_TAKEN,
                NOT_FOUND or UNAVAILABLE.
    UNCERTAIN   the service's answer is unknown. For an executed change a
                `booking_uncertain` dead letter exists, so a human checks.
    SUPERSEDED  replaced by a newer prepared change, or voided - a takeover, the
                reply guard firing, or a fallback reply.
    EXPIRED     a hold whose expiry passed before the patient confirmed, on the
                INJECTED clock.

    A partial unique index allows at most one PENDING row per conversation, which
    is what makes "the prepared change" a single thing rather than a set.
    """

    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
