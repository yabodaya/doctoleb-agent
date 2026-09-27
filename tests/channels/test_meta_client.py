"""The Meta client, against httpx.MockTransport. Nothing here touches a network.

Every test asserts on a `SendResult`, never on a status code: the whole point of
the module is that the status code is interpreted in one place.
"""

import json
import logging

import httpx
import pytest

from app.channels.whatsapp.client import (
    MetaClient,
    SendOutcome,
    classify_exception,
    classify_status,
)
from app.config import Settings
from tests.whatsapp_factories import PATIENT_TEXT, PHONE_NUMBER_ID, phone, wamid

ACCESS_TOKEN = "test-access-token-not-a-real-one"


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
    """A MockTransport handler that counts requests and replays a scripted reply."""

    def __init__(self, *responses, exception: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self._responses = list(responses)
        self._exception = exception

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._exception is not None:
            raise self._exception
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def client_for(handler, **setting_overrides) -> tuple[MetaClient, Recorder]:
    settings = make_settings(**setting_overrides)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return MetaClient(http, settings), handler


def ok_body(n: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": phone(n), "wa_id": phone(n)}],
            "messages": [{"id": wamid(n), "message_status": "accepted"}],
        },
    )


async def test_a_successful_send_returns_the_wamid():
    client, _ = client_for(Recorder(ok_body()))

    result = await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    assert result.outcome is SendOutcome.SUCCESS
    assert result.provider_message_id == wamid(1)


async def test_the_send_url_uses_the_phone_number_id_it_was_given():
    """Assumption A7. The reply goes out on the number the message ARRIVED on.

    With more than one clinic, META_PHONE_NUMBER_ID is simply the wrong number,
    and a reply from another clinic's number is worse than no reply at all.
    """
    client, recorder = client_for(Recorder(ok_body()), meta_phone_number_id="999999999999999")

    await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    url = str(recorder.requests[0].url)
    assert url == f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    assert "999999999999999" not in url


async def test_the_request_carries_the_bearer_token_and_the_whatsapp_shape():
    client, recorder = client_for(Recorder(ok_body()))

    await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    request = recorder.requests[0]
    assert request.headers["authorization"] == f"Bearer {ACCESS_TOKEN}"
    body = json.loads(request.content)
    assert body["messaging_product"] == "whatsapp"
    assert body["type"] == "text"
    assert body["to"] == phone(1)
    assert body["text"]["body"] == PATIENT_TEXT
    # A link in a reply must not make Meta fetch it on our behalf.
    assert body["text"]["preview_url"] is False


async def test_the_client_makes_exactly_one_attempt_for_a_500():
    """The single most important test in this module (Review Focus 6).

    A retry loop in here would make the backoff curve, max_tries and the dead
    letter all lie: five internal attempts inside five job tries is twenty-five
    sends, and the attempts column would say 5.
    """
    client, recorder = client_for(Recorder(httpx.Response(500, json={"error": {"code": 1}})))

    result = await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    assert len(recorder.requests) == 1
    assert result.outcome is SendOutcome.RETRYABLE


async def test_the_client_makes_exactly_one_attempt_for_a_timeout():
    client, recorder = client_for(Recorder(exception=httpx.ReadTimeout("too slow")))

    result = await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    assert len(recorder.requests) == 1
    assert result.outcome is SendOutcome.RETRYABLE


async def test_a_transport_error_is_retryable():
    client, _ = client_for(Recorder(exception=httpx.ConnectError("no route to host")))

    result = await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    assert result.outcome is SendOutcome.RETRYABLE
    assert "ConnectError" in result.reason


async def test_an_error_that_is_not_ours_escapes():
    """A bug in our own code must not be retried five times and dead-lettered as
    if Meta had been unreachable."""
    client, _ = client_for(Recorder(exception=ValueError("a bug in our serialisation")))

    with pytest.raises(ValueError):
        await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)


@pytest.mark.parametrize("status_code", [500, 502, 503, 504, 429])
def test_server_errors_and_rate_limits_are_retryable(status_code):
    assert classify_status(status_code) is SendOutcome.RETRYABLE


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 410, 422])
def test_other_client_errors_are_permanent(status_code):
    """A bad token, a bad recipient or a retired API version does not improve
    with repetition, and retrying it burns the rate limit the 429 path needs."""
    assert classify_status(status_code) is SendOutcome.PERMANENT


@pytest.mark.parametrize("status_code", [200, 201])
def test_success_statuses_classify_as_success(status_code):
    assert classify_status(status_code) is SendOutcome.SUCCESS


def test_classify_exception_returns_none_for_anything_that_is_not_transport():
    assert classify_exception(httpx.ReadTimeout("x")) is SendOutcome.RETRYABLE
    assert classify_exception(httpx.ConnectError("x")) is SendOutcome.RETRYABLE
    assert classify_exception(ValueError("x")) is None


async def test_a_429_is_retryable_through_the_client():
    client, _ = client_for(Recorder(httpx.Response(429, json={"error": {"code": 131056}})))

    result = await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    assert result.outcome is SendOutcome.RETRYABLE
    assert result.reason == "http_429 code_131056"


async def test_a_400_is_permanent_through_the_client():
    client, _ = client_for(
        Recorder(httpx.Response(400, json={"error": {"code": 131030, "error_subcode": 2494010}}))
    )

    result = await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    assert result.outcome is SendOutcome.PERMANENT
    assert result.reason == "http_400 code_131030 subcode_2494010"


async def test_a_401_is_permanent():
    """A wrong access token. Retrying it five times just delays the dead letter."""
    client, _ = client_for(Recorder(httpx.Response(401, json={})))

    result = await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    assert result.outcome is SendOutcome.PERMANENT


async def test_a_2xx_with_no_message_id_is_success_with_no_wamid():
    """Meta accepted the message and we cannot read the id it gave it.

    SUCCESS on purpose: retrying would send a second copy of a message that is
    already on its way. The cost - no status callback will ever match the row -
    is the lesser one, and it is written down in the plan's duplicate-reply gap.
    """
    client, _ = client_for(Recorder(httpx.Response(200, json={"messaging_product": "whatsapp"})))

    result = await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    assert result.outcome is SendOutcome.SUCCESS
    assert result.provider_message_id is None
    assert result.reason == "accepted_without_id"


async def test_a_2xx_with_an_unreadable_body_is_success_with_no_wamid():
    client, _ = client_for(Recorder(httpx.Response(200, content=b"not json")))

    result = await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    assert result.outcome is SendOutcome.SUCCESS
    assert result.provider_message_id is None


async def test_no_log_line_contains_the_access_token_the_recipient_or_the_text(caplog):
    """Hard rule 8 and 9 on the one path that is tempting to over-log.

    A failing send is exactly when someone reaches for "log the request so we can
    see what went wrong", and the request carries a bearer token, the patient's
    number and the patient's message.
    """
    body = {"error": {"message": f"Bad recipient {phone(1)}", "code": 131030}}
    client, _ = client_for(Recorder(httpx.Response(400, json=body)))

    with caplog.at_level(logging.DEBUG):
        await client.send_text(PHONE_NUMBER_ID, phone(1), PATIENT_TEXT)

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert ACCESS_TOKEN not in rendered
    assert phone(1) not in rendered
    assert PATIENT_TEXT not in rendered
    # The clinic's own account id IS loggable, and is what makes the line useful.
    assert PHONE_NUMBER_ID in rendered


def test_the_worker_package_contains_no_http_status_literals():
    """Review Focus 6: classification lives in one place.

    Asserted against the source, because the failure mode is a second opinion
    creeping in - `if response.status_code == 429` in a handler - which no
    behavioural test would catch until the two disagreed.
    """
    import pathlib
    import re

    worker = pathlib.Path("app/worker")
    if not worker.exists():  # the package arrives in Task 6
        pytest.skip("app/worker/ does not exist yet")

    offenders = []
    for path in worker.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"\bstatus_code\b|\b(4\d\d|5\d\d)\b", line) and "#" not in line:
                offenders.append(f"{path}:{number}")
    assert offenders == []
