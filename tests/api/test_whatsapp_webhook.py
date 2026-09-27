"""POST /webhooks/whatsapp - everything that happens before a row exists."""

from tests.whatsapp_factories import APP_SECRET, digest, envelope, signed, text_message, to_bytes

PATH = "/webhooks/whatsapp"


async def test_a_missing_signature_header_is_unauthorised(client, configure, no_database):
    raw = to_bytes(envelope(messages=[text_message()]))

    response = await client.post(PATH, content=raw)

    assert response.status_code == 401


async def test_an_invalid_signature_is_unauthorised(client, configure, no_database):
    raw = to_bytes(envelope(messages=[text_message()]))
    headers = {"X-Hub-Signature-256": f"sha256={digest(raw, 'someone-elses-secret')}"}

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 401


async def test_an_unset_app_secret_rejects_a_correctly_signed_body(client, configure, no_database):
    """Review Focus 2, at the endpoint rather than the function."""
    configure(meta_app_secret="")
    raw, headers = signed(envelope(messages=[text_message()]), secret="")

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 401


async def test_the_signature_is_checked_before_the_body_is_parsed(client, configure, no_database):
    """Review Focus 1.

    401, not 400 and not 422: an unauthenticated request must be rejected before
    anything looks at its body. A 400 here would mean we parsed a stranger's
    input first; a 422 would mean FastAPI did.
    """
    response = await client.post(
        PATH,
        content=b"{not json at all",
        headers={"X-Hub-Signature-256": "sha256=deadbeef"},
    )

    assert response.status_code == 401


async def test_a_malformed_json_body_with_a_valid_signature_is_a_bad_request(
    client, configure, no_database
):
    # Assumption A4: someone holding our app secret sent a broken body. Retrying
    # will not fix it, so do not ask Meta to retry - and do not pretend it was
    # stored either.
    raw, headers = signed(b"{not json at all")

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 400


async def test_a_payload_shape_we_do_not_model_returns_200_not_422(client, configure, no_database):
    """Review Focus 5.

    A 422 here would prove the handler declares a pydantic body parameter, which
    would mean FastAPI parsed and validated the body before the signature was
    checked. A 500 would make Meta retry a payload that will never change.
    """
    raw, headers = signed({"object": "whatsapp_business_account", "entry": [{"changes": []}]})

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 200


async def test_an_unmodelled_payload_never_touches_the_database(client, configure, no_database):
    # `no_database` is a session that raises if it is used at all: proof, not
    # inference, that nothing in this path opens a transaction.
    body = envelope(field="account_update")
    body["entry"][0]["changes"][0]["value"] = {"event": "VERIFIED"}
    raw, headers = signed(body)

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 200


async def test_the_endpoint_accepts_the_exact_bytes_meta_sent(client, configure, no_database):
    """Review Focus 1, from the other side.

    A body json.dumps() would never produce - different spacing - must still
    verify, because the digest is over what arrived.
    """
    raw = b'{"object": "whatsapp_business_account" ,  "entry" : [ ]  }'
    headers = {"X-Hub-Signature-256": f"sha256={digest(raw, APP_SECRET)}"}

    response = await client.post(PATH, content=raw, headers=headers)

    assert response.status_code == 200
