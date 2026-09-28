"""The enqueue boundary.

CLAUDE.md: the queue is behind an interface so it can move to SQS later. That is
the stated reason; the useful one is narrower — an interface with one method and
one argument is a place a payload cannot be smuggled through.
"""

import uuid
from typing import Protocol, runtime_checkable


class EnqueueError(Exception):
    """The job could not be handed to the queue.

    A single type so the webhook never has to know what a redis exception looks
    like, and never has to catch something broad enough to swallow a bug.
    """


@runtime_checkable
class JobQueue(Protocol):
    """Enqueue one inbox event for the worker.

    One method, one argument, and that argument is OUR row id - never Meta's
    event id (plan note C2). A wamid is base64 and decodes to include the
    patient's phone number, and a status event id carries the wamid of the
    message we sent TO the patient; neither belongs in Redis, in a job argument,
    or in the five log lines a retrying job writes.

    The narrowness is the point: SQS, arq and a three-line test double can all
    satisfy this, and hard rule 8 is satisfied by the signature itself.
    """

    async def enqueue_inbox_event(self, row_id: uuid.UUID) -> None: ...
