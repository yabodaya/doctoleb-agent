"""The media client, against httpx.MockTransport. Nothing here touches a network.

Two groups of tests, and they are not the same kind of test.

`check_media_url`'s cases are **adversarial**: that function is the whole of
what stops our access token being sent to a host named in a third party's
response body, so the interesting inputs are the ones designed to read as one
host and reach another. The corpus is plan appendix B.

The rest are behavioural: one attempt, one classified result, a hard cap, and
nothing of the URL or the audio in a log line (hard rule 8).
"""

import json
import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from app.channels.whatsapp.media import (
    MEDIA_HOST_SUFFIXES,
    MEDIA_ID,
    SUPPORTED_AUDIO_TYPES,
    MediaClient,
    MediaOutcome,
    MediaRef,
    MediaResult,
    base_mime_type,
    check_media_url,
)
from app.config import Settings
from tests.whatsapp_factories import (
    MEDIA_URL,
    OGG_MIME,
    PHONE_NUMBER_ID,
    SYNTHETIC_OGG,
    media_lookup_body,
)

ACCESS_TOKEN = "test-access-token-not-a-real-one"
EVENT_ID = "00000000-0000-0000-0000-00000000e0e0"
SOME_MEDIA_ID = "media-id-0000001"


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+asyncpg://user:pw@db:5432/doctoleb",
        "redis_url": "redis://cache:6379/1",
        "meta_access_token": ACCESS_TOKEN,
        "meta_api_version": "v21.0",
        "meta_api_base_url": "https://graph.facebook.com",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class Recorder:
    """A MockTransport handler that records every request it was given.

    It records the Authorization header as a BOOLEAN, not a value: "the token
    reached the CDN" and "the token reached nowhere else" are both assertions
    this module makes, and neither needs the token itself in a test file.
    """

    def __init__(self, *responses: httpx.Response, exception: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.authorized: list[bool] = []
        self._responses = list(responses)
        self._exception = exception

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.authorized.append("authorization" in request.headers)
        if self._exception is not None:
            raise self._exception
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def client_for(handler, **setting_overrides) -> MediaClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MediaClient(http, make_settings(**setting_overrides))


def ok_lookup(**body_overrides) -> httpx.Response:
    return httpx.Response(200, json=media_lookup_body(**body_overrides))


def ref(url: str = MEDIA_URL, mime_type: str = OGG_MIME, file_size: int | None = None) -> MediaRef:
    return MediaRef(url=url, mime_type=mime_type, file_size=file_size)


# --------------------------------------------------------------------------
# check_media_url: plan appendix B's corpus, as tests
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        MEDIA_URL,
        "https://mmg.whatsapp.net/v/t62.1/x.enc?ccb=1",
        "https://scontent.fbcdn.net/v/t1/x",
        "https://graph.facebook.com/v21.0/x",
        # Case and a trailing dot: the same host to DNS, so the same host here.
        "https://LOOKASIDE.FBSBX.COM./x",
        # The bare apex of each suffix, allowed without a subdomain.
        "https://fbsbx.com/x",
        # An explicit default port is not a "non-default port".
        "https://lookaside.fbsbx.com:443/x",
    ],
)
def test_check_media_url_accepts_every_allowed_host(url):
    assert check_media_url(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://lookaside.fbsbx.com/x", "not_https"),
        # The two shapes an attacker actually reaches for. `evil-fbsbx.com`
        # CONTAINS "fbsbx.com" and must not match; so does
        # `fbsbx.com.evil.test`, which merely starts with it.
        ("https://evil-fbsbx.com/x", "host_not_allowed"),
        ("https://fbsbx.com.evil.test/x", "host_not_allowed"),
        ("https://evil.test/x", "host_not_allowed"),
        # Userinfo and a non-default port: both make a URL read as one host and
        # reach another.
        ("https://user:pw@lookaside.fbsbx.com/x", "url_shape"),
        ("https://lookaside.fbsbx.com:8443/x", "url_shape"),
        ("https:///x", "url_shape"),
        ("not a url at all", "not_https"),
        ("", "not_https"),
    ],
)
def test_check_media_url_refuses_these(url, reason):
    assert check_media_url(url) == reason


def test_a_malformed_port_is_a_reason_and_not_an_exception():
    """urlsplit raises from `.port`, not at parse time.

    A bare `parts.port` would therefore be an unhandled exception in the middle
    of a job rather than a refusal - which is why the port is read inside its
    own try.
    """
    assert check_media_url("https://lookaside.fbsbx.com:notaport/x") == "url_shape"


def test_the_allow_list_is_suffixes_with_leading_dots_and_is_never_a_wildcard():
    """The list IS the boundary, so its shape is pinned.

    Every entry starts with a dot, which is what makes matching a real suffix
    test rather than a substring one. And `*` is never in it: plan check U3
    says to ADD an observed hostname, never to widen the list.
    """
    assert MEDIA_HOST_SUFFIXES
    for suffix in MEDIA_HOST_SUFFIXES:
        assert suffix.startswith(".")
        assert "*" not in suffix


# --------------------------------------------------------------------------
# The media id, before it reaches a URL path
# --------------------------------------------------------------------------


@pytest.mark.parametrize("media_id", ["media-id-0000001", "A", "a.b_c:d=e-f", "1234567890"])
def test_a_plausible_media_id_is_accepted(media_id):
    assert MEDIA_ID.match(media_id)


@pytest.mark.parametrize(
    "media_id",
    [
        "",
        "../other",
        "id/with/slashes",
        "id?phone_number_id=x",
        "id#fragment",
        "id%2Fencoded",
        "id with spaces",
        ".leading-dot",
        "a" * 257,
    ],
)
def test_a_media_id_that_could_change_the_url_is_refused(media_id):
    """The same defence as VS-007's OPAQUE_ID.

    A media id comes out of a third party's payload and goes into a URL PATH
    SEGMENT. Without this, `../` or a `?` would let that payload choose which
    Graph endpoint we call with our token attached.
    """
    assert not MEDIA_ID.match(media_id)


async def test_an_invalid_media_id_is_permanent_and_sends_no_request():
    recorder = Recorder(ok_lookup())
    client = client_for(recorder)

    result = await client.lookup("../other", PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.PERMANENT
    assert result.reason == "media_id_invalid"
    assert recorder.requests == []


async def test_the_reason_never_echoes_the_media_id():
    """It came from a third party's payload, so it is not ours to repeat."""
    recorder = Recorder(ok_lookup())
    client = client_for(recorder)

    result = await client.lookup("../SENTINEL", PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert "SENTINEL" not in result.reason


# --------------------------------------------------------------------------
# lookup()
# --------------------------------------------------------------------------


async def test_the_lookup_sends_the_token_and_the_phone_number_id():
    """The URL shape, the header, and the one id that belongs in the query.

    `phone_number_id` is the CLINIC's number - the one the message arrived on.
    The patient's number is nowhere near this call, which is why it is safe for
    it to be in a query string at all.
    """
    recorder = Recorder(ok_lookup())
    client = client_for(recorder)

    result = await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert result.succeeded
    request = recorder.requests[0]
    assert request.url.path == f"/v21.0/{SOME_MEDIA_ID}"
    assert request.url.params["phone_number_id"] == PHONE_NUMBER_ID
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_the_lookup_reads_url_mime_type_and_file_size():
    recorder = Recorder(ok_lookup())
    client = client_for(recorder)

    result = await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert result.ref is not None
    assert result.ref.url == MEDIA_URL
    assert result.ref.mime_type == OGG_MIME
    assert result.ref.file_size == len(SYNTHETIC_OGG)


@pytest.mark.parametrize(
    "body",
    [
        b"not json at all",
        json.dumps([1, 2, 3]).encode(),
        json.dumps({"messaging_product": "whatsapp"}).encode(),  # no url
        json.dumps({"url": ""}).encode(),
        json.dumps({"url": 7}).encode(),
    ],
)
async def test_a_lookup_body_we_cannot_read_is_retryable(body):
    """A proxy's error page arrives with a 200 often enough to matter.

    RETRYABLE rather than permanent, because that is transient; five tries then
    a dead letter, which is the job's business and not this module's.
    """
    recorder = Recorder(httpx.Response(200, content=body))
    client = client_for(recorder)

    result = await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.RETRYABLE
    assert result.reason == "media_lookup_unreadable"


async def test_a_file_size_that_is_not_an_integer_becomes_unknown():
    """The cap compares against it, so a string or a bool is worse than nothing.

    Unknown is safe: the streamed check still catches an oversized body, which
    is why there are three cap checks and not one.
    """
    recorder = Recorder(ok_lookup(file_size="lots"))
    client = client_for(recorder)

    result = await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert result.ref is not None
    assert result.ref.file_size is None


async def test_a_lookup_404_is_permanent():
    """A media id Meta no longer knows will not come back.

    Webhook media ids expire after seven days, and an id can be consumed, so
    retrying a 404 five times is five calls to learn the same thing.
    """
    recorder = Recorder(httpx.Response(404, json={"error": {"code": 100}}))
    client = client_for(recorder)

    result = await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.PERMANENT
    assert "http_404" in result.reason


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_a_lookup_429_or_5xx_is_retryable(status):
    """The same answer `classify_status` gives a send, from the same function."""
    recorder = Recorder(httpx.Response(status, json={"error": {"code": 4}}))
    client = client_for(recorder)

    result = await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.RETRYABLE
    assert f"http_{status}" in result.reason


async def test_a_lookup_timeout_is_retryable():
    recorder = Recorder(exception=httpx.ConnectTimeout("too slow"))
    client = client_for(recorder)

    result = await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.RETRYABLE
    assert result.reason.startswith("transport_")


async def test_our_own_deadline_fires_as_a_timeout():
    """A MockTransport does not honour httpx's own timeout, so this is the only
    thing proving `asyncio.timeout` is the deadline that actually bites."""
    import asyncio

    async def stall(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return ok_lookup()

    client = client_for(stall, meta_media_timeout_seconds=0.01)

    result = await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.RETRYABLE
    assert result.reason == "http_timeout"


async def test_a_bug_in_our_code_still_raises():
    """`classify_exception` returning None is what lets a real bug be seen.

    A ValueError from our own code must not be retried five times and
    dead-lettered as though Meta had been unreachable.
    """
    recorder = Recorder(exception=ValueError("a bug, not an outage"))
    client = client_for(recorder)

    with pytest.raises(ValueError):
        await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)


# --------------------------------------------------------------------------
# download(): the token, the cap, the redirect
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/x",
        "https://evil-fbsbx.com/x",
        "http://lookaside.fbsbx.com/x",
        "https://user:pw@lookaside.fbsbx.com/x",
        "https://lookaside.fbsbx.com:8443/x",
    ],
)
async def test_a_rejected_url_never_receives_the_token(url):
    """The single most important test in this module.

    The transport records every request it is given. On a rejected host there
    is NO request at all - not a request without a header, not a request that
    was cancelled: none. The check runs before the Authorization header is
    built, so there is nothing to leak even if the refusal were ignored.
    """
    recorder = Recorder(httpx.Response(200, content=SYNTHETIC_OGG))
    client = client_for(recorder)

    result = await client.download(ref(url=url), event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.PERMANENT
    assert result.reason == "media_url_rejected"
    assert recorder.requests == []
    assert recorder.authorized == []


async def test_an_allowed_url_does_receive_the_token():
    """The other half: the allow-list is a gate, not a wall."""
    recorder = Recorder(httpx.Response(200, content=SYNTHETIC_OGG))
    client = client_for(recorder)

    result = await client.download(ref(), event_id=EVENT_ID)

    assert result.succeeded
    assert recorder.authorized == [True]
    assert recorder.requests[0].headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_a_redirect_is_permanent_and_is_not_followed():
    """A 302 to another host is the exact shape of a token leak.

    `follow_redirects=False` makes it a RESPONSE rather than a second request,
    so the transport sees ONE request and the token went only to the host the
    allow-list approved.
    """
    recorder = Recorder(httpx.Response(302, headers={"Location": "https://evil.test/x"}))
    client = client_for(recorder)

    result = await client.download(ref(), event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.PERMANENT
    assert result.reason == "media_redirected"
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("mime_type", ["video/mp4", "image/jpeg", "application/pdf", "", "audio"])
async def test_an_unsupported_mime_type_is_permanent(mime_type):
    recorder = Recorder(httpx.Response(200, content=SYNTHETIC_OGG))
    client = client_for(recorder)

    result = await client.download(ref(mime_type=mime_type), event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.PERMANENT
    assert result.reason == "media_unsupported_type"
    assert recorder.requests == []


@pytest.mark.parametrize(
    "mime_type",
    ["audio/ogg", "audio/ogg; codecs=opus", "audio/ogg;codecs=opus", "AUDIO/OGG", "  audio/ogg  "],
)
async def test_the_mime_type_is_compared_without_its_parameters(mime_type):
    """A parameter is a detail of Meta's encoder (plan check U7).

    `codecs=opus` is what a WhatsApp voice note arrives as today; if Meta adds
    another parameter tomorrow, that must not turn a note we can transcribe
    into one we refuse.
    """
    recorder = Recorder(httpx.Response(200, content=SYNTHETIC_OGG))
    client = client_for(recorder)

    result = await client.download(ref(mime_type=mime_type), event_id=EVENT_ID)

    assert result.succeeded


@pytest.mark.parametrize("mime_type", ["audio/ogg; codecs=opus", "AUDIO/OGG", None, ""])
def test_base_mime_type_strips_parameters_and_case(mime_type):
    assert base_mime_type(mime_type) in SUPPORTED_AUDIO_TYPES | {""}


async def test_a_declared_file_size_over_the_cap_is_refused_before_any_request():
    """Cap check one of three, and the cheapest.

    A forged payload claiming a huge file never gets a request at all, so the
    worker spends nothing on it.
    """
    recorder = Recorder(httpx.Response(200, content=SYNTHETIC_OGG))
    client = client_for(recorder, voice_note_max_bytes=100)

    result = await client.download(ref(file_size=101), event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.PERMANENT
    assert result.reason == "media_too_large"
    assert result.byte_size == 101
    assert recorder.requests == []


async def test_a_declared_file_size_exactly_at_the_cap_is_allowed():
    """`>` and not `>=`: the cap is a maximum, not an exclusive bound."""
    payload = bytes(100)
    recorder = Recorder(httpx.Response(200, content=payload))
    client = client_for(recorder, voice_note_max_bytes=100)

    result = await client.download(ref(file_size=100), event_id=EVENT_ID)

    assert result.succeeded
    assert result.byte_size == 100


class ChunkCounter:
    """A transport whose body is an async generator, counting what was consumed.

    "Not every chunk was read" is the assertion that distinguishes a cap that
    stops reading from a cap that drains the response and then complains.
    """

    def __init__(self, chunks: int, size: int):
        self.consumed = 0
        self._chunks = chunks
        self._size = size

    async def _body(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.consumed += 1
            yield bytes(self._size)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=self._body())


async def test_a_stream_that_exceeds_the_cap_stops_reading():
    """Cap checks two and three: the running total, and the stream ABANDONED.

    Ten chunks of 100 bytes against a 250-byte cap: three chunks take the total
    past it, so the remaining seven are never pulled. That is the difference
    between one cap's worth of memory and the whole body (plan risk R5).
    """
    counter = ChunkCounter(chunks=10, size=100)
    client = client_for(counter, voice_note_max_bytes=250)

    result = await client.download(ref(), event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.PERMANENT
    assert result.reason == "media_too_large"
    assert counter.consumed == 3
    assert counter.consumed < 10


async def test_a_lying_content_length_cannot_exceed_the_cap():
    """A response is not a promise.

    `Content-Length: 1` with far more than one byte behind it is the whole
    reason the running total exists: trusting the header - or calling
    `response.content` - would buy an unbounded allocation inside the job.
    """
    body = bytes(5000)
    recorder = Recorder(httpx.Response(200, content=body, headers={"Content-Length": "1"}))
    client = client_for(recorder, voice_note_max_bytes=100)

    result = await client.download(ref(file_size=1), event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.PERMANENT
    assert result.reason == "media_too_large"


async def test_zero_bytes_is_permanent():
    """Nothing to transcribe, and asking again will not create some."""
    recorder = Recorder(httpx.Response(200, content=b""))
    client = client_for(recorder)

    result = await client.download(ref(), event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.PERMANENT
    assert result.reason == "media_empty"


async def test_a_successful_download_returns_the_bytes_and_their_count():
    recorder = Recorder(httpx.Response(200, content=SYNTHETIC_OGG))
    client = client_for(recorder)

    result = await client.download(ref(), event_id=EVENT_ID)

    assert result.succeeded
    assert result.data == SYNTHETIC_OGG
    assert result.byte_size == len(SYNTHETIC_OGG)


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (403, MediaOutcome.PERMANENT),
        (404, MediaOutcome.PERMANENT),
        (429, MediaOutcome.RETRYABLE),
        (500, MediaOutcome.RETRYABLE),
    ],
)
async def test_a_download_status_is_classified_by_the_shared_classifier(status, outcome):
    """`classify_status` again, so "is a 429 retryable?" has one answer.

    The job is what turns a 403 here into a RETRYABLE `voice_media_download_failed`
    (plan table row 12, risk R6: a media URL lives five minutes, so a 403 can
    mean the URL expired and a fresh lookup would succeed). This module's job is
    to report what the status was, not to guess why.
    """
    recorder = Recorder(httpx.Response(status, json={"error": {"code": 4}}))
    client = client_for(recorder)

    result = await client.download(ref(), event_id=EVENT_ID)

    assert result.outcome is outcome
    assert f"http_{status}" in result.reason


async def test_a_download_timeout_is_retryable():
    recorder = Recorder(exception=httpx.ReadTimeout("too slow"))
    client = client_for(recorder)

    result = await client.download(ref(), event_id=EVENT_ID)

    assert result.outcome is MediaOutcome.RETRYABLE


async def test_a_bug_in_our_code_still_raises_on_download():
    recorder = Recorder(exception=ValueError("a bug, not an outage"))
    client = client_for(recorder)

    with pytest.raises(ValueError):
        await client.download(ref(), event_id=EVENT_ID)


# --------------------------------------------------------------------------
# Hard rule 8: the URL is a credential, the audio is a patient
# --------------------------------------------------------------------------


def _ours(caplog) -> str:
    """Only this module's own log records.

    httpx's own logger prints the full URL of every request at INFO, which from
    VS-008 means a signed media link. That is a real leak and it is fixed where
    it belongs - app/logging_config.py pins `httpx` to WARNING, and
    tests/test_logging_config.py proves it end to end with a real
    `configure_logging` call. This helper keeps THIS module's tests about THIS
    module's log calls, instead of silently re-testing the floor.
    """
    return "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith("app.channels")
    )


async def test_nothing_logs_the_url_or_its_query_string(caplog):
    """The URL is a SIGNED link, i.e. a credential.

    A signed link in a log is a signed link anybody who can read logs can
    fetch. The sentinel is inside the query string, which is where the
    signature lives and the part a careless `%s` of the URL would carry.
    """
    signed = "https://lookaside.fbsbx.com/attachments/?mid=x&hash=SENTINELSIGNATURE"
    recorder = Recorder(
        httpx.Response(200, json=media_lookup_body(url=signed)),
        httpx.Response(200, content=SYNTHETIC_OGG),
    )
    client = client_for(recorder)

    with caplog.at_level(logging.DEBUG):
        looked_up = await client.lookup(SOME_MEDIA_ID, PHONE_NUMBER_ID, event_id=EVENT_ID)
        assert looked_up.ref is not None
        await client.download(looked_up.ref, event_id=EVENT_ID)

    ours = _ours(caplog)
    assert "SENTINELSIGNATURE" not in ours
    assert "hash=" not in ours
    assert "mid=" not in ours
    # The HOSTNAME is logged, deliberately: plan check U3 is "what host does a
    # real lookup answer with", and a hostname is not a credential.
    assert "lookaside.fbsbx.com" in ours


async def test_nothing_logs_a_byte_of_the_audio(caplog):
    """The audio is a recording of a patient's voice (hard rule 8).

    What a log line may carry is the COUNT, which is also the only thing the
    operator can act on.
    """
    recorder = Recorder(httpx.Response(200, content=b"OggS" + b"SENTINELAUDIO" + bytes(16)))
    client = client_for(recorder)

    with caplog.at_level(logging.DEBUG):
        await client.download(ref(), event_id=EVENT_ID)

    ours = _ours(caplog)
    assert "SENTINELAUDIO" not in ours
    assert "bytes=33" in ours


def test_the_ref_repr_hides_the_url():
    """pytest prints reprs on a failed assertion, which is how a credential
    reaches a CI log. The mime type and the size are enough to tell two refs
    apart in a traceback."""
    printed = repr(ref(url="https://lookaside.fbsbx.com/x?hash=SENTINELSIGNATURE", file_size=64))

    assert "SENTINELSIGNATURE" not in printed
    assert "audio/ogg" in printed
    assert "64" in printed


def test_the_result_repr_hides_the_audio():
    printed = repr(
        MediaResult(MediaOutcome.SUCCESS, data=b"SENTINELAUDIO", byte_size=13, ref=ref())
    )

    assert "SENTINELAUDIO" not in printed
    assert "SUCCESS" in printed
    assert "13" in printed
