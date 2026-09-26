"""Where jobs go when retrying stops being useful (hard rule 11)."""

import uuid
from typing import Any

from app.db.models import DeadLetterJob
from app.db.repositories.base import Repository


class DeadLetterJobRepository(Repository):
    """Not tenant-scoped: a job can die before tenant resolution succeeds, and
    that is exactly the failure most worth recording."""

    async def add(
        self,
        job_name: str,
        payload: dict[str, Any],
        error: str,
        attempts: int,
        tenant_id: uuid.UUID | None = None,
        source_event_id: str | None = None,
    ) -> DeadLetterJob:
        job = DeadLetterJob(
            tenant_id=tenant_id,
            job_name=job_name,
            source_event_id=source_event_id,
            payload=payload,
            # A reason code or exception class name. Never a formatted
            # exception carrying request or payload content (hard rule 8).
            error=error[:1000],
            attempts=attempts,
        )
        self._session.add(job)
        await self._session.flush()
        return job
