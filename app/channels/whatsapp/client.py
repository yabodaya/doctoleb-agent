"""The Meta WhatsApp Cloud API client: one attempt, one timeout, one classifier.

Hard rule 11: every external call has a timeout and bounded retries. The bound
lives in the job, not here — see `MetaClient.send_text`. The timeout is a
WALL-CLOCK deadline, not httpx's per-phase one (VS-005 amendment A2).
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.channels.whatsapp.redact import error_reason
from app.config import Settings

logger = logging.getLogger(__name__)


class SendOutcome(StrEnum):
    """What kind of result a send attempt produced.

    Three values, because there are exactly three things a caller can do: carry
    on, try again later, or stop trying.
    """

    SUCCESS = "SUCCESS"
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"


@dataclass(frozen=True)
class SendResult:
    """The outcome of ONE attempt, plus the wamid if Meta gave us one.

    `reason` is a short code built from numeric fields only (see redact.py). It
    is written to logs and to dead_letter_jobs.error, so it must be as safe to
    keep as an identifier.
    """

    outcome: SendOutcome
    provider_message_id: str | None = None
    reason: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome is SendOutcome.SUCCESS


def classify_status(status_code: int) -> SendOutcome:
    """The single place an HTTP status becomes a retry decision (requirement 4).

    Retryable: 5xx (Meta had a problem) and 429 (Meta asked us to slow down).
    Permanent: every other 4xx - a bad token, a bad recipient, a retired API
    version, a number not on the allowed list. None of those change because we
    ask again, and retrying them burns the rate limit that the 429 path needs.

    Kept here and nowhere else so that "is a 429 retryable?" has exactly one
    answer in this codebase. A test asserts app/worker/ contains no HTTP status
    literals at all.
    """
    if status_code >= 500 or status_code == 429:
        return SendOutcome.RETRYABLE
    if status_code >= 400:
        return SendOutcome.PERMANENT
    return SendOutcome.SUCCESS


def classify_exception(error: Exception) -> SendOutcome | None:
    """Transport failures, and only transport failures.

    Returns None for anything that is not ours, so a genuine bug in our own code
    escapes and is seen, instead of being quietly retried five times and
    dead-lettered as if Meta had been unreachable.

    A timeout is RETRYABLE even though it is genuinely ambiguous - Meta may have
    accepted the message. Calling it permanent would trade a rare duplicate reply
    for a routine silent loss, which is the worse of the two (see "The
    duplicate-reply gap" in docs/plans/VS-004-plan.md).

    The built-in TimeoutError is OUR OWN deadline firing (VS-005 amendment A2),
    not httpx's. It has to be listed, or send_text would re-raise it as though
    it were a bug in our code - which is the one thing the None return exists to
    let through.
    """
    if isinstance(error, httpx.TimeoutException):
        return SendOutcome.RETRYABLE
    if isinstance(error, httpx.TransportError):
        return SendOutcome.RETRYABLE
    if isinstance(error, TimeoutError):
        return SendOutcome.RETRYABLE
    return None


def _transport_reason(error: Exception) -> str:
    """A short code for a failure that never reached a status line.

    Our own deadline gets `http_timeout` rather than `transport_TimeoutError`:
    it is the same family of fact as `http_429`, it reads as a timeout in a
    dead letter, and it is what the README's triage table names.
    """
    if isinstance(error, TimeoutError) and not isinstance(error, httpx.TimeoutException):
        return "http_timeout"
    return f"transport_{type(error).__name__}"


class MetaClient:
    """Sends WhatsApp messages. Makes no decisions beyond classifying the result.

    The httpx client is injected rather than created here: the worker builds one
    per process and closes it on shutdown (assumption A8), and every test passes
    one wired to an httpx.MockTransport, so nothing in this repo's test suite can
    reach the network.
    """

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings

    def _url(self, phone_number_id: str) -> str:
        base = self._settings.meta_api_base_url.rstrip("/")
        return f"{base}/{self._settings.meta_api_version}/{phone_number_id}/messages"

    async def send_text(self, phone_number_id: str, to: str, text: str) -> SendResult:
        """One attempt to send one text message. Never a retry.

        Retries live in the job envelope, where the backoff, the try count and
        the dead letter are. A loop in here would be invisible to all three: five
        internal attempts inside five job tries is twenty-five sends, and the
        `attempts` column would say 5.

        `phone_number_id` is the number the patient's message ARRIVED on, not
        META_PHONE_NUMBER_ID (assumption A7). With more than one clinic the
        setting is simply the wrong number, and a reply from another clinic's
        number is worse than no reply.

        Never raises for a Meta-side failure: it returns a classified SendResult,
        because "what kind of failure was this" is a decision with one home and an
        exception type is a worse way to carry it. A bug in OUR code still raises.

        Nothing here logs `to`, `text`, or the access token.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            # preview_url off: a link in a reply must not make Meta fetch it.
            "text": {"preview_url": False, "body": text},
        }
        headers = {"Authorization": f"Bearer {self._settings.meta_access_token}"}

        deadline = self._settings.meta_send_timeout_seconds
        try:
            # asyncio.timeout on top of httpx's, because httpx's float applies
            # PER CONNECTION PHASE - connect, write, read and pool each get the
            # whole value - so one send can legitimately take several times
            # META_SEND_TIMEOUT_SECONDS (VS-005 amendment A2). JOB_TIMEOUT_SECONDS
            # is supposed to cover OPENAI_TIMEOUT_SECONDS plus this, and a job
            # arq times out is finished as FAILED and never retried: no dead
            # letter, no lease release, the event simply stranded.
            async with asyncio.timeout(deadline):
                response = await self._http.post(
                    self._url(phone_number_id),
                    json=payload,
                    headers=headers,
                    timeout=deadline,
                )
        except Exception as error:  # noqa: BLE001 - re-raised unless it is ours
            outcome = classify_exception(error)
            if outcome is None:
                raise
            reason = _transport_reason(error)
            logger.warning("meta send failed phone_number_id=%s reason=%s", phone_number_id, reason)
            return SendResult(outcome, reason=reason)

        outcome = classify_status(response.status_code)
        if outcome is not SendOutcome.SUCCESS:
            reason = error_reason(response.status_code, response.content)
            logger.warning(
                "meta send rejected phone_number_id=%s outcome=%s reason=%s",
                phone_number_id,
                outcome.value,
                reason,
            )
            return SendResult(outcome, reason=reason)

        provider_message_id = _first_message_id(response)
        if provider_message_id is None:
            # Meta accepted the message and we cannot read the id it gave it.
            # Still SUCCESS: retrying would send a second copy of a message that
            # is already on its way. The cost is that no status callback will
            # ever match this row. See "The duplicate-reply gap".
            logger.warning(
                "meta accepted a send with no message id phone_number_id=%s", phone_number_id
            )
            return SendResult(SendOutcome.SUCCESS, reason="accepted_without_id")

        return SendResult(SendOutcome.SUCCESS, provider_message_id=provider_message_id)


def _first_message_id(response: httpx.Response) -> str | None:
    """`messages[0].id` from a 2xx body, or None if it is not there.

    Tolerant on purpose: a body we cannot read is not a reason to send the
    message again.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    if not isinstance(first, dict):
        return None
    message_id = first.get("id")
    return message_id if isinstance(message_id, str) and message_id else None
