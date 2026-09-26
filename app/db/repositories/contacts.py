"""Patients, found or created by the identity that messaged us."""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Contact, ContactIdentity
from app.db.repositories.base import TenantScopedRepository


class ContactRepository(TenantScopedRepository):
    async def get(self, contact_id: uuid.UUID) -> Contact | None:
        """Tenant-scoped by construction: a correct id from the wrong tenant
        returns None rather than another clinic's patient."""
        return await self._session.scalar(
            sa.select(Contact).where(Contact.id == contact_id, Contact.tenant_id == self.tenant_id)
        )

    async def get_by_identity(self, channel: str, external_id: str) -> Contact | None:
        return await self._session.scalar(
            sa.select(Contact)
            .join(ContactIdentity, ContactIdentity.contact_id == Contact.id)
            .where(
                ContactIdentity.tenant_id == self.tenant_id,
                ContactIdentity.channel == str(channel),
                ContactIdentity.external_id == external_id,
            )
        )

    async def get_or_create_by_identity(
        self, channel: str, external_id: str, display_name: str | None = None
    ) -> Contact:
        """Find the patient behind this channel identity, creating both if new.

        The identity insert uses ON CONFLICT DO NOTHING and then re-reads. That
        is not only about concurrency: catching the IntegrityError instead would
        put an exception quoting the patient's phone number on the stack, one
        logger.exception() away from an error tracker (hard rule 8).
        """
        existing = await self.get_by_identity(channel, external_id)
        if existing is not None:
            return existing

        contact = Contact(tenant_id=self.tenant_id, display_name=display_name)
        self._session.add(contact)
        await self._session.flush()

        statement = (
            pg_insert(ContactIdentity)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                contact_id=contact.id,
                channel=str(channel),
                external_id=external_id,
            )
            .on_conflict_do_nothing(constraint="uq_contact_identities_identity")
            .returning(ContactIdentity.id)
        )
        inserted = await self._session.execute(statement)
        if inserted.scalar_one_or_none() is not None:
            return contact

        # Another writer won the race. Drop the contact we just made and take
        # theirs, so one patient never ends up as two rows.
        await self._session.delete(contact)
        await self._session.flush()
        winner = await self.get_by_identity(channel, external_id)
        assert winner is not None  # the conflict proves the row exists
        return winner
