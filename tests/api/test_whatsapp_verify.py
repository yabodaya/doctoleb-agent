"""GET /webhooks/whatsapp - Meta's subscription handshake.

Meta calls this once, from the dashboard, when the callback URL is saved. It
sends a challenge and expects it back as plain text; anything else, including a
JSON-wrapped version of the right value, fails verification.
"""

from app.config import get_settings
from tests.whatsapp_factories import VERIFY_TOKEN

PATH = "/webhooks/whatsapp"
CHALLENGE = "1158201444"


def _query(mode: str = "subscribe", token: str = VERIFY_TOKEN, challenge: str = CHALLENGE):
    return {"hub.mode": mode, "hub.verify_token": token, "hub.challenge": challenge}


async def test_the_handshake_returns_the_challenge_as_plain_text(client, configure):
    response = await client.get(PATH, params=_query())

    assert response.status_code == 200
    assert response.text == CHALLENGE
    assert response.headers["content-type"].startswith("text/plain")


async def test_the_challenge_is_not_wrapped_in_json(client, configure):
    # A JSON body would be `"1158201444"` - quoted - and Meta's string comparison
    # would fail with no useful error anywhere.
    response = await client.get(PATH, params=_query())

    assert '"' not in response.text


async def test_a_wrong_verify_token_is_forbidden(client, configure):
    response = await client.get(PATH, params=_query(token="not-the-token"))

    assert response.status_code == 403
    assert CHALLENGE not in response.text


async def test_a_mode_other_than_subscribe_is_forbidden(client, configure):
    response = await client.get(PATH, params=_query(mode="unsubscribe"))

    assert response.status_code == 403


async def test_missing_query_parameters_are_forbidden_not_unprocessable(client, configure):
    # Declared optional on purpose: required parameters would answer 422 with a
    # field-by-field description of our API to an unauthenticated caller, and an
    # incomplete handshake is a failed handshake either way.
    for params in (
        {},
        {"hub.mode": "subscribe"},
        {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN},
    ):
        response = await client.get(PATH, params=params)
        assert response.status_code == 403, params


async def test_an_unset_verify_token_forbids_even_an_empty_token(client, configure):
    """Review Focus 2.

    compare_digest("", "") is True. Without an explicit guard, an app deployed
    with META_VERIFY_TOKEN unset would hand the handshake to anyone who sent
    hub.verify_token= - and then accept their webhook configuration.
    """
    configure(meta_verify_token="")

    for token in ("", VERIFY_TOKEN, "anything"):
        response = await client.get(PATH, params=_query(token=token))
        assert response.status_code == 403, token


async def test_the_verify_token_comes_from_the_environment(client, monkeypatch):
    """The dependency-override tests above would pass even if the handler read a
    hardcoded constant. This one proves the wiring from Settings."""
    monkeypatch.setenv("META_VERIFY_TOKEN", "token-from-the-environment")
    get_settings.cache_clear()
    try:
        response = await client.get(PATH, params=_query(token="token-from-the-environment"))
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.text == CHALLENGE
