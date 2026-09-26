"""Declarative base, shared MetaData and the mixins every table uses."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic names for every constraint and index. Without this, PostgreSQL
# invents names for CHECK constraints, Alembic autogenerate reports a diff on
# every run, and a violated constraint cannot be reported by name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        """Identity only. Never column content.

        Hard rule 8: these objects surface in tracebacks, debugger output and
        assertion failures. A repr that helpfully printed `text` or
        `display_name` would put a patient's words into an error tracker.

        Reads `self.__dict__` and never `getattr`. A rollback expires every
        attribute, even with expire_on_commit=False, and InstanceState._expire
        deletes the values out of the instance __dict__; a getattr would then
        miss in AttributeImpl.get and fire the expired loader, which under
        AsyncSession raises MissingGreenlet. That happens while an exception is
        being formatted, so it replaces the error you were trying to read.
        """
        parts: list[str] = []
        for name in ("id", "tenant_id"):
            value: Any = self.__dict__.get(name)
            if value is not None:
                parts.append(f"{name}={value}")
        return f"<{type(self).__name__} {' '.join(parts)}>"


class UUIDPrimaryKeyMixin:
    # `default=` alone is an INSERT-time default: SQLAlchemy stores it as a
    # CallableColumnDefault and only evaluates it when the row is flushed, so
    # `row.id` would be None until then. It is kept as the fallback for Core
    # inserts that bypass this constructor.
    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)

    def __init__(self, **kwargs: Any) -> None:
        """Assign the id now, not at flush.

        Repositories and tests build a parent and a child in one breath — a
        contact and its identity, a conversation and its messages — and pass the
        parent's id to the child before anything is flushed. With only the
        column default that id is None and the child gets a NULL foreign key.

        MRO note: a model declared as `Contact(UUIDPrimaryKeyMixin,
        TimestampMixin, Base)` runs this first, and `super().__init__` reaches
        the declarative constructor that assigns the remaining kwargs.
        """
        kwargs.setdefault("id", uuid.uuid4())
        super().__init__(**kwargs)


class TimestampMixin:
    created_at: Mapped[Any] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    updated_at: Mapped[Any] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
