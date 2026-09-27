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
        self.fail = fail

    async def enqueue_inbox_event(self, row_id: uuid.UUID) -> None:
        if self.fail:
            raise EnqueueError("ConnectionError")
        self.enqueued.append(row_id)
