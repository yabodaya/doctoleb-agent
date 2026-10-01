"""The media side of the Cloud API: a media id in, audio bytes out.

A WhatsApp `audio` message does not carry the audio. It carries a **media id**,
and getting the bytes takes two calls:

    GET {base}/{version}/{media_id}?phone_number_id=...   -> a signed CDN URL
    GET <that URL>                                        -> the bytes

Both carry our access token. Meta's media URLs live five minutes and the media
ids in a webhook live seven days, which is why nothing here stores a URL: a job
retried later re-does the lookup rather than reusing a link that has expired.

Built exactly like `client.py`: one attempt per call, one WALL-CLOCK deadline,
one classifier, an injected `httpx.AsyncClient`, and never an exception for a
Meta-side failure (hard rule 11 - the retry curve and the dead letter live in
the job).

**Two things in here are security, not plumbing.**

1. The URL in that lookup response is *observed content*: it comes out of a
   third party's response body. Sending our access token to whatever host
   appears there would turn one mistaken or poisoned body into an access-token
   leak. So `check_media_url` runs **before the Authorization header exists**,
   redirects are not followed, and the URL - a signed link, which is to say a
   credential - is never logged, never stored and never put in a repr.
2. The download is capped three times (plan risk R5): against the declared
   `file_size` before a byte is fetched, against the running total while
   streaming, and by abandoning the stream the moment the total is passed. A
   response that lies about its length therefore costs one cap's worth of
   memory and nothing more.

Hard rule 8: no audio byte and no URL reaches a log line. What is logged is a
hostname, a byte count, a base mime type and a code.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

import httpx

from app.channels.whatsapp.client import SendOutcome, classify_exception, classify_status
from app.channels.whatsapp.redact import error_reason, scrub
from app.config import Settings

logger = logging.getLogger(__name__)

# Hosts the download may be sent to. An ALLOW-LIST, and never widened to "*":
# this tuple is the whole of what stops our access token leaving Meta.
#
# Meta does not document the hostname its lookup answers with (plan check U3),
# which is why there are four. Each is a suffix WITH its leading dot, so
# matching is a real suffix test: `evil-fbsbx.com` must not match `.fbsbx.com`.
MEDIA_HOST_SUFFIXES: tuple[str, ...] = (
    ".fbsbx.com",  # lookaside.fbsbx.com - what inbound media is observed on
    ".whatsapp.net",  # mmg.whatsapp.net - historically used for media
    ".fbcdn.net",  # Meta's CDN
    ".facebook.com",  # graph.facebook.com, if a lookup ever answers with itself
)

# The BASE mime types Meta documents for an inbound audio message. Compared
# with the parameters stripped, so `audio/ogg; codecs=opus` - which is what a
# WhatsApp voice note actually arrives as - is the same thing as `audio/ogg`.
SUPPORTED_AUDIO_TYPES: frozenset[str] = frozenset(
    {"audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/amr"}
)

# A media id goes into a URL PATH SEGMENT, so it is validated before it gets
# there - the same defence as VS-007's OPAQUE_ID. No spaces, no `/`, `?`, `#`
# or `%`, so a media id from a third party's payload can never be smuggled into
# another path segment or a query string.
MEDIA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:=-]{0,255}$")

# How much of a FAILED response we are willing to read in order to build a
# reason code. The same rule as the audio cap, for the same reason: an error
# page is a response body too, and "never read a whole response into memory"
# has no exceptions. 64 KiB is far more than Meta's error object needs.
_ERROR_BODY_LIMIT = 64 * 1024


class MediaOutcome(StrEnum):
    """What kind of result one media call produced.

    Its own three-value enum rather than `SendOutcome`, because a download is
    not a send and `SendOutcome.SUCCESS` on a media result reads wrong. The
    DECISION behind it is still shared: `classify_status` and
    `classify_exception` in `client.py` stay the only answer in this codebase to
    "is a 429 retryable?", and `_as_media` below is the one-line adapter.
    """

    SUCCESS = "SUCCESS"
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"


def _as_media(outcome: SendOutcome) -> MediaOutcome:
    """The shared classifier's answer, in this module's vocabulary."""
    return MediaOutcome(outcome.value)


@dataclass(frozen=True)
class MediaRef:
    """What a lookup told us about one media object.

    `url` is `repr=False` AND excluded from every log line: it is a SIGNED link,
    which is to say a credential, and a signed link in a log is a signed link
    anybody who can read logs can fetch. The repr prints the base mime type and
    the declared size, which is enough to tell two refs apart in a traceback.
    """

    url: str = field(repr=False)
    mime_type: str
    file_size: int | None


@dataclass(frozen=True)
class MediaResult:
    """The outcome of ONE media call, classified.

    `data` is `repr=False` for hard rule 8: it is a recording of a patient's
    voice, and pytest prints reprs on a failed assertion.
    """

    outcome: MediaOutcome
    reason: str = ""
    ref: MediaRef | None = None
    data: bytes | None = field(default=None, repr=False)
    byte_size: int = 0

    @property
    def succeeded(self) -> bool:
        return self.outcome is MediaOutcome.SUCCESS


def base_mime_type(mime_type: str | None) -> str:
    """`audio/ogg` from `audio/ogg; codecs=opus`, lower-cased.

    A parameter is a detail of Meta's encoder - `codecs=opus` today, something
    else tomorrow - and the only thing we decide with is the base type (plan
    check U7). Splitting on `;` means a parameter Meta adds later cannot turn
    a voice note we can transcribe into one we refuse.
    """
    if not mime_type:
        return ""
    return mime_type.split(";", 1)[0].strip().lower()


def check_media_url(url: str) -> str | None:
    """Why this URL must not receive our access token, or None for "acceptable".

    A pure function with its own tests, because it is the single thing standing
    between the token and the internet, and because the interesting cases are
    all adversarial rather than behavioural.

    Called BEFORE the request is constructed - i.e. before the Authorization
    header exists - so a refusal means no request happened at all.

        not_https        the scheme is anything but https
        host_not_allowed the hostname is not under MEDIA_HOST_SUFFIXES
        url_shape        userinfo, a non-default port, or no hostname
        url_unparseable  urlsplit refused it

    Userinfo and a non-default port are both refused because both are ways to
    make a URL *read* as one host and *reach* another.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "url_unparseable"
    if parts.scheme != "https":
        return "not_https"
    if parts.username or parts.password:
        return "url_shape"
    try:
        port = parts.port
    except ValueError:
        # urlsplit raises from .port rather than at parse time for a malformed
        # port, so it is read inside its own try. A bare parts.port here would
        # be an unhandled exception in the middle of a job.
        return "url_shape"
    if port not in (None, 443):
        return "url_shape"
    # Lower-cased and with any trailing dot stripped, because `FBSBX.COM.` and
    # `fbsbx.com` are the same host to DNS and must be the same host here.
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return "url_shape"
    # The apex itself is allowed; everything else must END WITH a dotted
    # suffix. Never `in`: a substring test would accept `evil-fbsbx.com` and
    # `fbsbx.com.evil.test`, which are exactly the two shapes an attacker
    # reaches for.
    apexes = tuple(suffix.lstrip(".") for suffix in MEDIA_HOST_SUFFIXES)
    if host in apexes or host.endswith(MEDIA_HOST_SUFFIXES):
        return None
    return "host_not_allowed"


class MediaClient:
    """Looks a media id up and downloads the bytes. Makes no other decision.

    The httpx client is injected for the same two reasons `MetaClient`'s is: the
    worker builds ONE per process and closes it on shutdown (VS-004's A8), and
    every test passes one wired to an `httpx.MockTransport`, so nothing in this
    repo's test suite can reach Meta.

    Holds no per-call state and no lock, so one instance serves every job.
    """

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._settings = settings

    def _lookup_url(self, media_id: str) -> str:
        base = self._settings.meta_api_base_url.rstrip("/")
        return f"{base}/{self._settings.meta_api_version}/{media_id}"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.meta_access_token}"}

    async def lookup(self, media_id: str, phone_number_id: str, *, event_id: str) -> MediaResult:
        """One attempt to turn a media id into a signed URL. Never a retry.

        `event_id` is carried for the log line only - it is the `webhook_inbox`
        row, so a log line and a `voice_notes` row are joinable by hand (Q8).

        `phone_number_id` is the clinic's number, the one the message arrived
        on. The patient's number is nowhere near this call.

        A 404 is PERMANENT: a media id Meta no longer knows - older than seven
        days, or already consumed - will not come back. A body we cannot read is
        RETRYABLE once, because a proxy's error page is transient; five tries
        then dead-letter it.
        """
        if not MEDIA_ID.match(media_id or ""):
            # Checked here as well as in the job, because this is the function
            # that would put it in a URL path. The id itself is NEVER echoed
            # into the reason: it came out of a third party's payload.
            logger.warning("media lookup refused event_id=%s reason=media_id_invalid", event_id)
            return MediaResult(MediaOutcome.PERMANENT, reason="media_id_invalid")

        deadline = self._settings.meta_media_timeout_seconds
        try:
            # asyncio.timeout on top of httpx's, because httpx's float applies
            # per connection phase, so one call can legitimately take several
            # times META_MEDIA_TIMEOUT_SECONDS. This is one of the three terms
            # JOB_TIMEOUT_SECONDS has to cover.
            async with asyncio.timeout(deadline):
                response = await self._http.get(
                    self._lookup_url(media_id),
                    params={"phone_number_id": phone_number_id},
                    headers=self._headers,
                    follow_redirects=False,
                    timeout=deadline,
                )
        except Exception as error:  # noqa: BLE001 - re-raised unless it is ours
            outcome = classify_exception(error)
            if outcome is None:
                raise
            reason = _transport_reason(error)
            logger.warning("media lookup failed event_id=%s reason=%s", event_id, reason)
            return MediaResult(_as_media(outcome), reason=reason)

        status_outcome = classify_status(response.status_code)
        if status_outcome is not SendOutcome.SUCCESS:
            reason = error_reason(response.status_code, response.content)
            logger.warning(
                "media lookup rejected event_id=%s outcome=%s reason=%s",
                event_id,
                status_outcome.value,
                reason,
            )
            return MediaResult(_as_media(status_outcome), reason=reason)

        ref = _read_media_ref(response)
        if ref is None:
            logger.warning(
                "media lookup event_id=%s outcome=RETRYABLE reason=media_lookup_unreadable",
                event_id,
            )
            return MediaResult(MediaOutcome.RETRYABLE, reason="media_lookup_unreadable")

        # The HOSTNAME, for plan check U3 - never the URL, which is signed.
        logger.info(
            "media lookup event_id=%s outcome=SUCCESS reason= host=%s",
            event_id,
            scrub(urlsplit(ref.url).hostname or ""),
        )
        return MediaResult(MediaOutcome.SUCCESS, ref=ref)

    async def download(self, ref: MediaRef, *, event_id: str) -> MediaResult:
        """One attempt to fetch the bytes. Never a retry.

        The order matters and is the order of the checks that cost nothing:
        the URL, then the type, then the declared size, and only then a request.
        Every refusal above that line means **the token was never sent**.
        """
        rejected = check_media_url(ref.url)
        if rejected is not None:
            # No header has been built at this point, and none will be. The
            # reason names the SHAPE of the problem, never the URL.
            logger.warning(
                "media download refused event_id=%s reason=media_url_rejected detail=%s",
                event_id,
                rejected,
            )
            return MediaResult(MediaOutcome.PERMANENT, reason="media_url_rejected")

        base_type = base_mime_type(ref.mime_type)
        if base_type not in SUPPORTED_AUDIO_TYPES:
            logger.warning(
                "media download refused event_id=%s reason=media_unsupported_type type=%s",
                event_id,
                scrub(base_type),
            )
            return MediaResult(MediaOutcome.PERMANENT, reason="media_unsupported_type")

        cap = self._settings.voice_note_max_bytes
        if ref.file_size is not None and ref.file_size > cap:
            # Cap check one of three, and the cheapest: a forged payload
            # claiming a huge file never gets a request at all.
            logger.warning(
                "media download refused event_id=%s reason=media_too_large bytes=%d",
                event_id,
                ref.file_size,
            )
            return MediaResult(
                MediaOutcome.PERMANENT, reason="media_too_large", byte_size=ref.file_size
            )

        deadline = self._settings.meta_media_timeout_seconds
        try:
            async with asyncio.timeout(deadline):
                return await self._stream(ref, cap=cap, deadline=deadline, event_id=event_id)
        except Exception as error:  # noqa: BLE001 - re-raised unless it is ours
            outcome = classify_exception(error)
            if outcome is None:
                raise
            reason = _transport_reason(error)
            logger.warning("media download failed event_id=%s reason=%s", event_id, reason)
            return MediaResult(_as_media(outcome), reason=reason)

    async def _stream(
        self, ref: MediaRef, *, cap: int, deadline: float, event_id: str
    ) -> MediaResult:
        """The streamed body, under a running cap. Called inside the deadline.

        `follow_redirects=False`, so a 302 to another host is a RESPONSE and not
        a second request carrying our token. A redirect is therefore PERMANENT:
        Meta does not need to redirect us, and if it starts to, that is a
        decision to make deliberately rather than a token to hand over.
        """
        async with self._http.stream(
            "GET",
            ref.url,
            headers=self._headers,
            follow_redirects=False,
            timeout=deadline,
        ) as response:
            if 300 <= response.status_code < 400:
                logger.warning(
                    "media download refused event_id=%s reason=media_redirected status=%d",
                    event_id,
                    response.status_code,
                )
                return MediaResult(MediaOutcome.PERMANENT, reason="media_redirected")

            status_outcome = classify_status(response.status_code)
            if status_outcome is not SendOutcome.SUCCESS:
                reason = error_reason(response.status_code, await _bounded_body(response))
                logger.warning(
                    "media download rejected event_id=%s outcome=%s reason=%s",
                    event_id,
                    status_outcome.value,
                    reason,
                )
                return MediaResult(_as_media(status_outcome), reason=reason)

            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > cap:
                    # Cap checks two and three: the running total, and the
                    # stream abandoned here rather than drained. The context
                    # manager closes the connection on the way out, so a
                    # response that lies about Content-Length costs one cap's
                    # worth of memory and nothing more.
                    logger.warning(
                        "media download refused event_id=%s reason=media_too_large bytes=%d",
                        event_id,
                        len(buffer),
                    )
                    return MediaResult(
                        MediaOutcome.PERMANENT, reason="media_too_large", byte_size=len(buffer)
                    )

        if not buffer:
            logger.warning("media download refused event_id=%s reason=media_empty", event_id)
            return MediaResult(MediaOutcome.PERMANENT, reason="media_empty")

        logger.info(
            "media downloaded event_id=%s bytes=%d type=%s",
            event_id,
            len(buffer),
            base_mime_type(ref.mime_type),
        )
        return MediaResult(MediaOutcome.SUCCESS, data=bytes(buffer), byte_size=len(buffer), ref=ref)


async def _bounded_body(response: httpx.Response) -> bytes:
    """At most _ERROR_BODY_LIMIT bytes of a failed streamed response.

    Enough for Meta's error object, and bounded for the same reason the audio
    is: an error page is a response body too.
    """
    buffer = bytearray()
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        if len(buffer) >= _ERROR_BODY_LIMIT:
            break
    return bytes(buffer[:_ERROR_BODY_LIMIT])


def _read_media_ref(response: httpx.Response) -> MediaRef | None:
    """`url`, `mime_type` and `file_size` from a 2xx body, or None.

    Tolerant like everything else that reads a Meta payload: `url` must be a
    non-empty `str` or there is nothing to download, and `file_size` is taken
    only when it is an `int`, because the cap compares against it. A body we
    cannot read becomes a retryable code rather than an exception in the middle
    of a job.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    url = body.get("url")
    if not isinstance(url, str) or not url:
        return None
    mime_type = body.get("mime_type")
    file_size = body.get("file_size")
    return MediaRef(
        url=url,
        mime_type=mime_type if isinstance(mime_type, str) else "",
        # bool is an int in Python, and `file_size: true` is not a size.
        file_size=file_size
        if isinstance(file_size, int) and not isinstance(file_size, bool)
        else None,
    )


def _transport_reason(error: Exception) -> str:
    """A short code for a failure that never reached a status line.

    The same shape as `client.py`'s, deliberately: our own deadline gets
    `http_timeout` rather than `transport_TimeoutError`, so a dead letter from
    a media call reads like one from a send.
    """
    if isinstance(error, TimeoutError) and not isinstance(error, httpx.TimeoutException):
        return "http_timeout"
    return f"transport_{type(error).__name__}"
