"""arq worker entrypoint: `arq app.worker.main.WorkerSettings`.

Run as its own process, separate from the api, so slow work never happens inside
a webhook request (hard rule 1). This is where the two genuinely slow things in
the repo live — the OpenAI call and the Meta call — and they live on the far
side of the queue.
"""

import logging
from typing import Any

import httpx
from arq.connections import RedisSettings
from arq.worker import func

from app.agent import Clock, utc_now
from app.agent.tools import MIN_SECONDS_FOR_A_BOOKING_CHANGE
from app.channels.whatsapp.client import MetaClient
from app.config import Settings, get_settings
from app.db.session import dispose_engine, get_sessionmaker
from app.integrations.booking.memory import InMemoryBookingService
from app.integrations.openai.chat import OpenAIChatClient
from app.logging_config import configure_logging
from app.tenants.resolver import ConfigTenantResolver
from app.worker.jobs import process_inbox_event

logger = logging.getLogger(__name__)

_settings = get_settings()


async def ping(ctx: dict[str, Any]) -> str:
    """A no-op job, used to verify the worker is consuming from Redis.

    Kept from VS-001: it is still the cheapest proof that the worker is alive,
    and it costs one function.
    """
    return "pong"


def startup_warnings(settings: Settings) -> list[str]:
    """What an operator needs to hear once per worker start. Names and numbers,
    never secrets (hard rule 9).

    Without the first two, "every reply is the fallback" looks like a bug rather
    than a missing .env entry. Without the third, a slow reply is cut off by
    arq's job timeout and stranded with no dead letter (plan assumption A14).

    Warnings rather than a boot failure, deliberately: VS-004's assumption A1
    chose a loud runtime signal over a dead process for configuration mistakes,
    and the api must not refuse to boot over a worker knob.
    """
    warnings: list[str] = []
    if not settings.openai_api_key:
        warnings.append("OPENAI_API_KEY is not set: every reply will be AGENT_FALLBACK_REPLY")
    if not settings.openai_chat_model.strip():
        warnings.append("OPENAI_CHAT_MODEL is not set: every reply will be AGENT_FALLBACK_REPLY")
    # Decision D4: the job must cover the WHOLE tool loop plus the one send.
    # OPENAI_TIMEOUT_SECONDS is deliberately not in this sum - it bounds one
    # model call, and every model call happens inside the turn budget. Naming it
    # here would point an operator at the wrong knob.
    budget = settings.agent_turn_timeout_seconds + settings.meta_send_timeout_seconds
    if settings.job_timeout_seconds <= budget:
        warnings.append(
            f"JOB_TIMEOUT_SECONDS={settings.job_timeout_seconds:g} does not exceed "
            f"AGENT_TURN_TIMEOUT_SECONDS={settings.agent_turn_timeout_seconds:g} + "
            f"META_SEND_TIMEOUT_SECONDS={settings.meta_send_timeout_seconds:g}: "
            "a slow reply can be cut off mid-send"
        )
    # Q8, and risk R8: the real danger in this slice is fake availability
    # reaching a real patient. VS-011 adds the BOOKING_CLIENT switch and the
    # refusal to start in production; until then this line, on every worker
    # start, is the whole of the mitigation - with clearly fake names and a
    # fictional address behind it.
    warnings.append(
        # The three substrings test_startup_always_warns_that_the_booking_client_is_fake
        # asserts are kept: "booking service", "FAKE" and "demo". VS-007 adds what is
        # now also true and was not before - it holds BOOKINGS, they are lost on a
        # restart, and two workers would each have their own set (risk R8).
        "booking service is the in-memory FAKE (VS-007): availability, holds and "
        "bookings are demo data kept in this worker's memory and lost on every "
        "restart - run ONE worker, and never put this worker in front of real patients"
    )
    # V15. Below the floor no booking change can ever START, so every confirmation
    # would be refused with turn_time_low and the patient would be asked to send it
    # again, forever. A warning rather than a boot failure, like the one above: the
    # api must not refuse to boot over a worker knob (VS-005 A14).
    if settings.agent_turn_timeout_seconds <= MIN_SECONDS_FOR_A_BOOKING_CHANGE:
        warnings.append(
            f"AGENT_TURN_TIMEOUT_SECONDS={settings.agent_turn_timeout_seconds:g} is not "
            f"above MIN_SECONDS_FOR_A_BOOKING_CHANGE={MIN_SECONDS_FOR_A_BOOKING_CHANGE:g}: "
            "no booking change can ever start"
        )
    return warnings


def booking_backends(clock: Clock) -> InMemoryBookingService:
    """The ONE Booking Service stand-in a worker process uses, for both roles.

    A function so that `startup()` can call it INSIDE arq's running loop. The service
    holds an `asyncio.Lock`, and a lock binds to the loop it is first contended in
    (plan check U5): built at import time it would belong to no loop, and the first
    two concurrent jobs would get `RuntimeError: ... bound to a different event loop`.

    One instance for `booking` AND `patient_bookings`, deliberately: it implements both
    Protocols, and two instances would mean a hold made through one being invisible to
    the other - the same slot offered twice.
    """
    return InMemoryBookingService.demo(clock=clock)


async def startup(ctx: dict[str, Any]) -> None:
    """Build everything the jobs share, once per process.

    One httpx client for the whole worker (plan assumption A8): connection reuse,
    and a client per job leaks sockets under load.

    The tenant resolver is built here rather than per job, so a broken
    WHATSAPP_TENANT_MAP is one startup failure per worker instead of a parse per
    event - while an UNMAPPED number is still a per-event dead letter, because
    that is resolve()'s job, not the constructor's.
    """
    configure_logging()
    settings = get_settings()
    for warning in startup_warnings(settings):
        logger.warning(warning)
    http = httpx.AsyncClient()
    ctx["settings"] = settings
    ctx["sessionmaker"] = get_sessionmaker()
    ctx["http"] = http
    ctx["meta"] = MetaClient(http, settings)
    # One chat client per process (plan assumption A9), holding the SDK's own
    # httpx2 pool - a separate stack from the httpx client above. Built even
    # without a key: it reports openai_api_key_unset instead of failing to
    # construct, so the worker boots with no OpenAI account at all.
    ctx["chat"] = OpenAIChatClient(settings)
    # VS-006. The clock is injected so nothing below reads the wall clock
    # directly, and the booking client is the in-memory FAKE - see the warning
    # in startup_warnings().
    ctx["clock"] = utc_now
    # VS-007: ONE stateful service for both roles, built HERE so its lock belongs to
    # arq's loop (plan check U5). Its state - holds, appointments, the replay store -
    # is lost on every restart, which is what the warning above says out loud.
    service = booking_backends(utc_now)
    ctx["booking"] = service
    ctx["patient_bookings"] = service
    ctx["resolver"] = ConfigTenantResolver.from_settings(settings)
    logger.info("worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    chat: OpenAIChatClient | None = ctx.get("chat")
    if chat is not None:
        await chat.aclose()
    http: httpx.AsyncClient | None = ctx.get("http")
    if http is not None:
        await http.aclose()
    await dispose_engine()
    logger.info("worker stopped")


class WorkerSettings:
    """arq reads these as plain class attributes."""

    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    functions = [
        ping,
        # max_tries and the timeout come from settings (hard rule 11). The
        # timeout must stay below the claim lease, or the lease expires while the
        # job is still inside the Meta call and a second worker sends the same
        # reply - Settings.claim_lease_seconds derives itself to guarantee that.
        # It must also stay ABOVE openai_timeout_seconds + meta_send_timeout_seconds,
        # which startup_warnings() checks: a job arq times out is finished as
        # failed, our except blocks never run, and the event is stranded with no
        # dead letter.
        func(
            process_inbox_event,
            name="process_inbox_event",
            max_tries=_settings.job_max_tries,
            timeout=_settings.job_timeout_seconds,
        ),
    ]
    # The same ceiling for anything registered later without its own.
    max_tries = _settings.job_max_tries
    on_startup = startup
    on_shutdown = shutdown
