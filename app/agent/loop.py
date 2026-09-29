"""The tool loop: the model asks, our code decides, and both are bounded.

**Tool calling: the model REQUESTS, our code EXECUTES.** Each model call is
given the tool schemas. The model can answer with text, or with one or more
tool calls. It never runs anything. We read each call, validate it, run it with
values the model cannot see or change (the tenant), append the result as a
`tool` message, and ask again - until it answers with text, or until a limit
stops it.

**Why two limits.** A model can keep asking for tools forever, so the loop needs
both a COUNT limit and a WALL-CLOCK one. Each model call re-sends the whole
prompt and is billed, so the count bounds the bill. The deadline bounds how long
the patient waits, and it is what keeps the job inside arq's timeout - the one
deadline with none of our exit paths behind it.

Nothing here logs (VS-005 A5): only the job knows the `webhook_inbox` row id
that every other line carries, so the job writes the one line per generation.
"""

from dataclasses import dataclass, field

from app.agent.tools import ToolCallRecord, ToolContext, ToolCrashed, ToolExecutionStatus
from app.agent.tools.registry import ToolRegistry
from app.integrations.openai import ChatClient, ChatMessage, ChatOutcome, ChatResult

# Decision D4. A constant, not a setting: the number was fixed by the decision,
# and a deployment that could raise it could raise the bill without a code
# change. Three is the normal tool turn (list_doctors, search, answer); the
# fourth is the margin for one self-correction after an invalid-arguments error.
MAX_MODEL_CALLS = 4

# A belt-and-braces cap on total tool calls per turn, for the case where the
# model asks for many PARALLEL calls each round. 4 model calls x 3 tools would
# be 12; beyond that the turn is not going anywhere useful.
MAX_TOOL_CALLS_PER_TURN = 12


@dataclass
class LoopState:
    """What the turn has accumulated so far.

    Mutable and local to one `process_turn` call, never shared: arq runs several
    jobs at once in one process.
    """

    records: list[ToolCallRecord] = field(default_factory=list)
    model_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    # The tool that was running when the turn deadline fired, if any. It has no
    # record yet - `execute` returns one only when it finishes - so the deadline
    # handler writes one for it rather than losing the call entirely.
    in_flight: tuple[int, int, str] | None = None

    @property
    def tool_count(self) -> int:
        return len(self.records)

    def next_sequence(self) -> int:
        return len(self.records)

    def add_tokens(self, result: ChatResult) -> None:
        """Summed across every model call that reported them.

        `None` stays `None` until something is reported, so "the API told us
        nothing" and "zero tokens" stay distinguishable in `agent_runs`.
        """
        if result.prompt_tokens is not None:
            self.prompt_tokens = (self.prompt_tokens or 0) + result.prompt_tokens
        if result.completion_tokens is not None:
            self.completion_tokens = (self.completion_tokens or 0) + result.completion_tokens

    def close_in_flight(self, error_code: str) -> None:
        """Record the tool that the deadline interrupted.

        Without this the call is invisible: it was asked for, it started, it may
        have reached the Booking Service, and `tool_executions` would show a gap
        in `sequence` with nothing explaining it.
        """
        if self.in_flight is None:
            return
        sequence, model_call, tool_name = self.in_flight
        self.records.append(
            ToolCallRecord(
                sequence, model_call, tool_name, (), ToolExecutionStatus.ERROR, error_code, 0
            )
        )
        self.in_flight = None

    def record_crash(self, crash: ToolCrashed) -> None:
        self.in_flight = None
        self.records.append(
            ToolCallRecord(
                self.next_sequence(),
                self.model_calls,
                crash.tool_name,
                (),
                ToolExecutionStatus.ERROR,
                "tool_crashed",
                0,
            )
        )


async def run_loop(
    messages: list[ChatMessage],
    chat: ChatClient,
    registry: ToolRegistry,
    ctx: ToolContext,
    state: LoopState,
) -> tuple[ChatOutcome, str, str | None]:
    """The loop proper. Returns (outcome, reason, reply_text).

    Returning a tuple rather than an `AgentResult` keeps `app/agent/core.py` the
    only place that knows what an `AgentResult` looks like, so the timeout and
    crash handlers there build one the same way this does.
    """
    specs = registry.specs()
    for model_call in range(1, MAX_MODEL_CALLS + 1):
        # Set BEFORE the await, so a call cut off by the deadline still counts:
        # it was started and it was billed.
        state.model_calls = model_call
        result = await chat.complete(messages, specs)
        state.add_tokens(result)

        if result.outcome is not ChatOutcome.SUCCESS:
            # A RETRYABLE failure mid-loop ends the turn RETRYABLE, keeping the
            # records gathered so far. The job's one retry layer re-runs the
            # whole turn, which is safe because every tool is read-only.
            return result.outcome, result.reason, None

        if not result.tool_calls:
            # A text-only response is the reply. Interim text that arrived WITH
            # tool calls never gets here, which is the point: only a final
            # text-only answer is sent to the patient.
            return ChatOutcome.SUCCESS, "ok", result.text

        if model_call == MAX_MODEL_CALLS:
            # It asked for tools on the last allowed call. Running them would
            # produce results nothing could read, so they are recorded and
            # skipped. PERMANENT (Q3): a retry would likely loop the same way
            # and bill again.
            for call in result.tool_calls:
                _, record = registry.skipped(
                    call,
                    sequence=state.next_sequence(),
                    model_call=model_call,
                    reason="max_model_calls",
                )
                state.records.append(record)
            return ChatOutcome.PERMANENT, "agent_max_model_calls", None

        messages.append(ChatMessage("assistant", result.text, tool_calls=result.tool_calls))
        for call in result.tool_calls:
            # Parallel calls run SEQUENTIALLY and in order: deterministic for
            # `sequence` and for tests, and the tools are read-only and fast.
            # Concurrency is a follow-up if latency ever needs it.
            if state.tool_count >= MAX_TOOL_CALLS_PER_TURN:
                content, record = registry.skipped(
                    call,
                    sequence=state.next_sequence(),
                    model_call=model_call,
                    reason="too_many_tool_calls",
                )
            else:
                sequence = state.next_sequence()
                resolved = call.name if registry.get(call.name) is not None else "unknown"
                state.in_flight = (sequence, model_call, resolved)
                content, record = await registry.execute(
                    call, ctx, sequence=sequence, model_call=model_call
                )
                state.in_flight = None
            state.records.append(record)
            # EVERY call gets a tool message, skipped ones included: OpenAI
            # requires exactly one per tool_call_id, and a missing one is a 400
            # on the next request rather than a worse answer.
            messages.append(ChatMessage("tool", content, tool_call_id=call.id))

    raise AssertionError("unreachable: the last iteration always returns")  # pragma: no cover


__all__ = [
    "MAX_MODEL_CALLS",
    "MAX_TOOL_CALLS_PER_TURN",
    "LoopState",
    "run_loop",
]
