"""Reading and writing one voice note's bookkeeping.

Four things this layer is careful about, all of them learned in earlier slices:

  * **Plain data out, never an entity.** `status_for` selects COLUMNS and
    returns a frozen dataclass. With `expire_on_commit=False` an entity loaded
    in one session never sees another transaction's commit, so an ORM object
    crossing back into the job is a stale read waiting to happen (plan risk R2).
  * **Upsert, never read-then-write.** `start` is one
    `INSERT ... ON CONFLICT (message_id) DO UPDATE`, so two attempts on one
    message produce an insert and an update rather than a duplicate or an
    `IntegrityError` to catch (hard rule 2).
  * **Tenant-scoped by construction.** The tenant comes from the constructor,
    never from an argument, so no call site can forget it (hard rule 4).
  * **Bookkeeping failures are logged, not fatal.** Everything here runs inside
    `begin_nested()` - a SAVEPOINT - because PostgreSQL aborts the WHOLE
    transaction on any failed statement, and this runs in the same transaction
    that writes the transcript and builds the turn.
"""

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from app.db.enums import VoiceNoteStatus
from app.db.models import VoiceNote
from app.db.repositories.base import TenantScopedRepository
from app.db.repositories.errors import VoiceNoteNotRecordedError


@dataclass(frozen=True)
class VoiceNoteRow:
    """What T1 needs to know about a voice note before doing anything.

    Three fields, and none of them can hold content. `status` is what decides
    whether the voice step runs at all: `DONE`, `UNCLEAR` and `FAILED` are
    terminal - the patient was answered - and `PENDING` is the one state a
    retry re-attempts.
    """

    status: str
    attempts: int
    media_id: str | None

    @property
    def terminal(self) -> bool:
        """True when this message's voice step is finished for good.

        A retry that finds a terminal row skips the whole voice step: no media
        lookup, no download, and above all no second transcription to pay for.
        """
        return self.status in (
            VoiceNoteStatus.DONE.value,
            VoiceNoteStatus.UNCLEAR.value,
            VoiceNoteStatus.FAILED.value,
        )

    @property
    def transcribed(self) -> bool:
        """True when `messages.text` holds the transcript."""
        return self.status == VoiceNoteStatus.DONE.value


class VoiceNoteRepository(TenantScopedRepository):
    """Writes `voice_notes`, and makes the one read T1 needs."""

    async def status_for(self, message_id: uuid.UUID) -> VoiceNoteRow | None:
        """How far this message's voice note already got, or None.

        A COLUMN select, never an entity, so it cannot be served from the
        session's identity map: this is the read that decides whether to spend
        money on a transcription, and a stale answer would mean either paying
        twice or answering the patient twice.
        """
        row = (
            await self._session.execute(
                sa.select(VoiceNote.status, VoiceNote.attempts, VoiceNote.media_id).where(
                    VoiceNote.message_id == message_id,
                    VoiceNote.tenant_id == self._tenant_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return VoiceNoteRow(status=row.status, attempts=row.attempts, media_id=row.media_id)

    async def start(
        self,
        message_id: uuid.UUID,
        *,
        inbox_event_id: uuid.UUID,
        media_id: str | None,
        mime_type: str | None,
        voice: bool | None,
    ) -> None:
        """Mark an attempt as started, and count it.

        Called before anything else is written, so a row exists even for a
        failure - which is what gives the operator something to look at when
        voice notes stop working.

        `ON CONFLICT (message_id) DO UPDATE` with `attempts = attempts + 1`: the
        increment happens in SQL, so two writers cannot read the same count and
        both write the same number. The identifying columns are re-set on
        conflict too, because a retry re-reads them from the stored payload and
        they should be whatever the latest attempt saw.
        """
        statement = (
            pg_insert(VoiceNote)
            .values(
                id=uuid.uuid4(),
                tenant_id=self._tenant_id,
                message_id=message_id,
                inbox_event_id=inbox_event_id,
                media_id=media_id[:255] if media_id else None,
                mime_type=mime_type[:64] if mime_type else None,
                voice=voice,
                status=VoiceNoteStatus.PENDING.value,
                attempts=1,
            )
            .on_conflict_do_update(
                constraint="uq_voice_notes_message_id",
                set_={
                    "status": VoiceNoteStatus.PENDING.value,
                    "attempts": VoiceNote.attempts + 1,
                    "media_id": sa.literal(media_id[:255] if media_id else None),
                    "mime_type": sa.literal(mime_type[:64] if mime_type else None),
                    "voice": sa.literal(voice, sa.Boolean),
                    "updated_at": sa.func.now(),
                },
            )
        )
        try:
            async with self._session.begin_nested():
                await self._session.execute(statement)
        except SQLAlchemyError as error:
            raise VoiceNoteNotRecordedError(type(error).__name__) from None

    async def finish(
        self,
        message_id: uuid.UUID,
        *,
        status: VoiceNoteStatus,
        error_code: str | None = None,
        byte_size: int | None = None,
        duration_seconds: float | None = None,
        model: str | None = None,
    ) -> None:
        """Move a PENDING row to its terminal state.

        One `UPDATE ... WHERE message_id = ... AND tenant_id = ...`, never a
        load-then-mutate: there is nothing to read first, and reading would put
        an entity in the identity map for the next transaction to be misled by.

        `error_code` is truncated here rather than at the call site, for the
        same reason `agent_runs.reason` is: a code that outgrew its column
        would turn a recorded failure into a second one.
        """
        try:
            async with self._session.begin_nested():
                await self._session.execute(
                    sa.update(VoiceNote)
                    .where(
                        VoiceNote.message_id == message_id,
                        VoiceNote.tenant_id == self._tenant_id,
                    )
                    .values(
                        status=status.value,
                        error_code=error_code[:64] if error_code else None,
                        byte_size=byte_size,
                        duration_seconds=duration_seconds,
                        model=model[:128] if model else None,
                        updated_at=sa.func.now(),
                    )
                )
        except SQLAlchemyError as error:
            raise VoiceNoteNotRecordedError(type(error).__name__) from None
