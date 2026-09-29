"""Recording what a turn cost, without recording what it said."""

import uuid
from dataclasses import dataclass, field

from sqlalchemy.exc import SQLAlchemyError

from app.db.models import AgentRun, ToolExecution
from app.db.repositories.base import TenantScopedRepository
from app.db.repositories.errors import RunNotRecordedError


@dataclass(frozen=True)
class ToolExecutionRow:
    """One `tool_executions` row, as plain data.

    It lives here, in `app/db/`, rather than in `app/agent/`, so that this layer
    never imports the Agent Core. The job maps the agent's `ToolCallRecord` onto
    this on its way in - two small dataclasses instead of one shared import that
    would tie the database schema to the loop's internals.

    Nothing on it can hold content: a registered tool name or "unknown",
    declared argument NAMES, a status, an error code, and two integers.
    """

    sequence: int
    model_call: int
    tool_name: str
    argument_names: tuple[str, ...]
    status: str
    error_code: str | None
    duration_ms: int

    def __repr__(self) -> str:  # pragma: no cover - trivial, but see hard rule 8
        return (
            f"ToolExecutionRow(sequence={self.sequence}, tool_name={self.tool_name!r}, "
            f"status={self.status!r}, error_code={self.error_code!r})"
        )


@dataclass(frozen=True)
class AgentRunRow:
    """One `agent_runs` row, as plain data. Codes, counts and ids only."""

    inbox_event_id: uuid.UUID
    conversation_id: uuid.UUID
    inbound_message_id: uuid.UUID
    job_try: int
    prompt_version: str
    outcome: str
    reason: str
    model_calls: int
    duration_ms: int
    reply_message_id: uuid.UUID | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_executions: tuple[ToolExecutionRow, ...] = field(default=())


class AgentRunRepository(TenantScopedRepository):
    """Writes `agent_runs` and `tool_executions`. It never reads them back.

    Tenant-scoped like every table that knows a tenant: the tenant comes from
    the constructor, never from an argument, so no call site can forget it and
    no method signature is shaped like something a tool could expose
    (hard rule 4).
    """

    async def add(self, run: AgentRunRow) -> uuid.UUID:
        """Insert the run and its tool rows, and return the run's id.

        Everything happens inside `session.begin_nested()` - a SAVEPOINT - for a
        reason that is easy to get wrong: PostgreSQL aborts the WHOLE
        transaction on any failed statement. This call happens in T1b, after the
        reply has been reserved, so without the savepoint a failed bookkeeping
        insert would poison the transaction and cost the patient their reply.
        With it, only these rows roll back.

        Any `SQLAlchemyError` becomes `RunNotRecordedError(<class name>)`,
        raised `from None`: the caller logs a code and carries on sending
        (hard rule 8, and hard rule 11's "a failure is recorded, not fatal").
        """
        run_id = uuid.uuid4()
        try:
            async with self._session.begin_nested():
                self._session.add(
                    AgentRun(
                        id=run_id,
                        tenant_id=self._tenant_id,
                        inbox_event_id=run.inbox_event_id,
                        conversation_id=run.conversation_id,
                        inbound_message_id=run.inbound_message_id,
                        reply_message_id=run.reply_message_id,
                        job_try=run.job_try,
                        model=run.model[:100] if run.model else None,
                        prompt_version=run.prompt_version[:32],
                        outcome=run.outcome,
                        # Truncated here rather than at the call site: a reason
                        # is a code, and a code that outgrows its column would
                        # otherwise turn a recorded failure into a second one.
                        reason=run.reason[:100],
                        model_calls=run.model_calls,
                        prompt_tokens=run.prompt_tokens,
                        completion_tokens=run.completion_tokens,
                        duration_ms=run.duration_ms,
                    )
                )
                for tool in run.tool_executions:
                    self._session.add(
                        ToolExecution(
                            agent_run_id=run_id,
                            tenant_id=self._tenant_id,
                            sequence=tool.sequence,
                            model_call=tool.model_call,
                            tool_name=tool.tool_name[:64],
                            argument_names=list(tool.argument_names),
                            status=tool.status,
                            error_code=tool.error_code[:64] if tool.error_code else None,
                            duration_ms=tool.duration_ms,
                        )
                    )
                await self._session.flush()
        except SQLAlchemyError as error:
            raise RunNotRecordedError(type(error).__name__) from None
        return run_id
