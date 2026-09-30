"""A JobQueue that records instead of enqueueing."""

import uuid

from app.queue import EnqueueError, JobQueue


class FakeJobQueue(JobQueue):
    """Records the row ids it was handed, or fails on demand.

    Deliberately typed as a JobQueue and asserted against the runtime-checkable
    Protocol, so this and ArqJobQueue cannot drift.
    """

    def __init__(self, fail: bool = False) -> None:
        self.enqueued: list[uuid.UUID] = []
        # Everything ever handed over, kept even after `drain` pops `enqueued`. It is
        # what lets a test assert what reached REDIS - our row id and nothing else -
        # after the jobs have already run (VS-004's plan note C2).
        self.enqueued_ever: list[uuid.UUID] = []
        self.fail = fail

    async def enqueue_inbox_event(self, row_id: uuid.UUID) -> None:
        if self.fail:
            raise EnqueueError("ConnectionError")
        self.enqueued.append(row_id)
        self.enqueued_ever.append(row_id)
