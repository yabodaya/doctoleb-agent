"""One voice note's bookkeeping, as ids, codes and counts.

**Why this table exists.** Transcription is the only step in this repo that costs
money per attempt and cannot be made idempotent by a key. If a job dies after
transcribing and before replying, arq re-runs it - and without a durable marker
the second run would pay again and, worse, could get a *different* transcript,
so the patient's message would change between attempts. A `DONE` row here is
what lets a retry read what the first attempt heard instead of buying a second
opinion.

It also carries the three things an operator needs when voice notes stop
working - the media id, the byte count and an error code - and the configured
model name, so the real cost per note can be worked out from a table rather
than from a bill.

**There is no `transcript` column and there never will be.** The transcript is
the patient's message, it lives in `messages.text` like a typed one, and it is
subject to the same retention and the same access. Putting a copy here would
create a second place patient content lives, with no retention story of its
own. Three tests pin that - the exact column set, the absence of any unbounded
`TEXT` column except `tenant_id`, and the absence of a transcript column by
name - so adding one means consciously editing a test (hard rule 8).

**What else may never be here** (plan W13): the media URL, which is a signed
link and therefore a credential; Meta's `sha256` of the audio, which is a
fingerprint of a patient's voice and buys us nothing once the audio is gone;
the patient reference; and anything from the reply.

**The unique constraint on `message_id` is the design.** One row per message,
decided by the database. It is what makes a retried job - and a duplicated
webhook - produce one transcription instead of two (hard rule 2), rather than
an application-level "select then insert" that has a window in it.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import VoiceNoteStatus, check_constraint


class VoiceNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "voice_notes"
    __table_args__ = (
        check_constraint("status", VoiceNoteStatus, "status_valid"),
        # One row per message, decided by the database (hard rule 2).
        sa.UniqueConstraint("message_id", name="uq_voice_notes_message_id"),
        sa.Index("ix_voice_notes_tenant_id_created_at", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Operational state dies with its message, so this IS a foreign key with
    # ON DELETE CASCADE - unlike agent_runs, whose cost records deliberately
    # outlive message retention. There is nothing here worth keeping once the
    # message it describes is gone.
    message_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    # The webhook_inbox row - the `event_id=` on every log line, so a log line
    # and this row are joinable by hand. No FK: retention may prune the inbox,
    # the same reasoning as agent_runs.inbox_event_id.
    inbox_event_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    # Meta's media id. An identifier, not content, and the only handle that
    # could ever fetch the audio again - for seven days, from Meta, never from
    # us. NULL when the payload carried none.
    media_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # The BASE mime type, parameters stripped (`audio/ogg`, not
    # `audio/ogg; codecs=opus`): a parameter is a detail of Meta's encoder and
    # the only thing we decide with is the base type.
    mime_type: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # Whatever the audio endpoint reported, when it reports anything (plan
    # conflict C15, check U8). Read by nobody; kept so the real cost per note
    # can be worked out from the table instead of from a bill.
    duration_seconds: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    # True for a recorded voice note, False for an attached audio file. Recorded
    # and never acted on: a patient who attaches a recording meant us to hear it,
    # and MessageModality has one value for both.
    voice: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # A CODE from the failure table in plan section 5.5. Never a message: an
    # error message is the least controlled string in the system, and the ones
    # that reach here come from Meta and from OpenAI.
    error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    # The CONFIGURED model name (VS-006's Q10), never the one the provider says
    # it served. NULL means the row never reached the audio endpoint at all.
    model: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
