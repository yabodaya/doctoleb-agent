"""Patients, and the channel identities that point at them.

A contact is one person at one clinic. contact_identities exists rather than a
phone column on contacts because VS-008 and later channels attach more than one
identity to the same person.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import Channel, check_constraint


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (sa.Index("ix_contacts_tenant_id", "tenant_id"),)

    tenant_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Patient content (hard rule 8): stored, never logged, never in a repr.
    display_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    identities: Mapped[list["ContactIdentity"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan", lazy="selectin"
    )


class ContactIdentity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contact_identities"
    __table_args__ = (
        # "unique per tenant" from the slice. Two clinics may legitimately have
        # the same patient phone number; one clinic may not have it twice.
        # Named explicitly rather than by convention because the repository
        # reports it by name when a concurrent insert loses the race.
        sa.UniqueConstraint(
            "tenant_id", "channel", "external_id", name="uq_contact_identities_identity"
        ),
        check_constraint("channel", Channel, "channel_valid"),
    )

    tenant_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # The WhatsApp wa_id, i.e. the patient's phone number. Patient content:
    # this is the value a unique-violation message would quote, which is why
    # repositories upsert instead of catching IntegrityError.
    external_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    contact: Mapped[Contact] = relationship(back_populates="identities")
