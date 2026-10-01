"""Repository errors that are safe to log.

Review Focus 1: the unique key on contact_identities contains a patient's phone
number, and asyncpg puts conflicting values into the exception message. One
logger.exception() or one Sentry capture would ship that to a third party
(hard rule 8). Nothing here ever carries the original message.
"""

from typing import Any

from sqlalchemy.exc import IntegrityError


class RepositoryError(Exception):
    """Base for errors this layer raises."""


class DuplicateRecordError(RepositoryError):
    """A unique constraint rejected the write.

    Carries the constraint name and nothing else. The name says which rule was
    broken, which is all a caller or a log line needs; the conflicting value is
    patient content.
    """

    def __init__(self, constraint: str) -> None:
        self.constraint = constraint
        super().__init__(f"duplicate violates {constraint}")


def _driver_error(error: IntegrityError) -> Any:
    """Find the exception that actually carries `constraint_name`.

    With the asyncpg dialect this is NOT `error.orig`. SQLAlchemy 2.1's asyncpg
    adapter wraps the driver exception in an emulated DBAPI error
    (`AsyncAdapt_asyncpg_dbapi.UniqueViolationError`, an `EmulatedDBAPIException`
    from sqlalchemy/exc.py) and raises it `from` the asyncpg one. So:

        IntegrityError.orig      -> the adapter exception (no constraint_name)
        .driver_exception / .orig-> the asyncpg exception (has constraint_name)

    Verified on the pinned SQLAlchemy 2.1.1 / asyncpg 0.31: reading
    `error.orig.constraint_name` returns None for every violation, which would
    silently degrade every duplicate to "unknown constraint".

    Each candidate is tried in turn so this also stays correct for sync drivers
    (psycopg), where `error.orig` IS the driver exception, and for SQLAlchemy
    2.0.x, which predates `driver_exception`.
    """
    seen: list[Any] = []
    candidate: Any = error.orig
    while candidate is not None and candidate not in seen:
        if getattr(candidate, "constraint_name", None):
            return candidate
        seen.append(candidate)
        candidate = (
            getattr(candidate, "driver_exception", None)
            or getattr(candidate, "orig", None)
            or candidate.__cause__
        )
    return None


def as_duplicate(error: IntegrityError) -> DuplicateRecordError:
    """Translate an IntegrityError, discarding its message.

    The original exception is deliberately NOT chained with `raise ... from
    error`: a chained traceback would print the message we are trying not to
    keep.
    """
    driver_error = _driver_error(error)
    constraint = getattr(driver_error, "constraint_name", None) or "unknown constraint"
    return DuplicateRecordError(constraint)


class RunNotRecordedError(RepositoryError):
    """Recording an agent run and its tool executions failed (VS-006).

    Carries the exception CLASS name and nothing else, and is raised `from None`
    so no chained traceback survives. The engine runs with hide_parameters=True,
    but a chained traceback would still print the statement, and a statement is
    the wrong place to look for a leak from a table designed to hold no content.

    It is a distinct type because the caller's reaction is specific: bookkeeping
    that fails is logged and rolled back inside its own SAVEPOINT, and the
    patient's reply still goes out.
    """

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(f"agent run not recorded: {error_class}")


class VoiceNoteNotRecordedError(RepositoryError):
    """Recording a voice note's bookkeeping failed (VS-008).

    The same shape and the same reasoning as `RunNotRecordedError`: the exception
    CLASS name and nothing else, raised `from None` so no chained traceback
    survives to print the statement.

    The caller logs it and carries on, because bookkeeping that fails must never
    cost a patient their reply. Note what is NOT in this savepoint: the
    transcript write. That one is allowed to fail the whole transaction, because
    a turn built from a transcript nobody stored is the single thing the "write
    it the moment it exists" rule exists to prevent - the retry would pay for a
    second transcription and could hear something different.
    """

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(f"voice note not recorded: {error_class}")


class BookingStateNotRecordedError(RepositoryError):
    """Recording a booking action failed (VS-007).

    The same shape and the same reasoning as `RunNotRecordedError`: the exception
    CLASS name and nothing else, raised `from None` so no chained traceback
    survives to print the statement.

    A distinct type because the caller's reaction is distinct. A booking outcome
    that could not be written is worse than a cost record that could not be
    written: the Booking Service has changed something our table does not know
    about. So the job logs it, writes a dead letter, and still sends the reply -
    the change happened, and the patient's receipt is built from the service's own
    answer, not from this row.
    """

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(f"booking action not recorded: {error_class}")
