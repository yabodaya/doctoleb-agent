"""Splitting one Meta envelope into the things we store.

No database and no app: this is a pure function over dicts.
"""

from app.channels.whatsapp.payloads import (
    InboundMessage,
    InboxItemKind,
    StatusUpdate,
    content_hash,
    extract_inbox_items,
)
from tests.whatsapp_factories import (
    PATIENT_TEXT,
    PHONE_NUMBER_ID,
    PROFILE_NAME,
    contact,
    envelope,
    phone,
    status_update,
    text_message,
    wamid,
)


def test_one_text_message_becomes_one_item_keyed_by_wamid():
    items = extract_inbox_items(envelope(messages=[text_message(1)]))

    assert [i.provider_event_id for i in items] == [f"msg:{wamid(1)}"]
    assert items[0].kind is InboxItemKind.MESSAGE


def test_a_status_is_keyed_by_wamid_and_status():
    # The msg: / status: prefixes are not decoration: a message and its own
    # `sent` callback share one wamid, so without them the status would collide
    # with the message and one of the two would be silently dropped.
    items = extract_inbox_items(envelope(statuses=[status_update(1, "sent")]))

    assert [i.provider_event_id for i in items] == [f"status:{wamid(1)}:sent"]
    assert items[0].kind is InboxItemKind.STATUS


def test_three_statuses_for_one_message_are_three_distinct_items():
    """Review Focus 6. sent, delivered and read are three events, not retries."""
    statuses = [status_update(1, s) for s in ("sent", "delivered", "read")]

    items = extract_inbox_items(envelope(statuses=statuses))

    assert [i.provider_event_id for i in items] == [
        f"status:{wamid(1)}:sent",
        f"status:{wamid(1)}:delivered",
        f"status:{wamid(1)}:read",
    ]
    assert len({i.provider_event_id for i in items}) == 3


def test_messages_and_statuses_in_one_post_all_become_items():
    """Review Focus 3. One POST is not one event."""
    body = envelope(
        messages=[text_message(1), text_message(2), text_message(3)],
        statuses=[status_update(8, "delivered"), status_update(9, "read")],
    )

    items = extract_inbox_items(body)

    assert len(items) == 5
    assert [i.kind for i in items] == [InboxItemKind.MESSAGE] * 3 + [InboxItemKind.STATUS] * 2


def test_the_stored_payload_carries_everything_vs004_needs():
    """Review Focus 7. The worker sees the row, never the request."""
    items = extract_inbox_items(envelope(messages=[text_message(1)], contacts=[contact(1)]))
    payload = items[0].payload

    # Tenant resolution (hard rule 4) is impossible without this.
    assert payload["metadata"]["phone_number_id"] == PHONE_NUMBER_ID
    assert payload["kind"] == "message"
    assert payload["field"] == "messages"
    assert payload["item"]["id"] == wamid(1)
    assert payload["item"]["text"]["body"] == PATIENT_TEXT
    # The profile name lives on the contacts list, not on the message.
    assert payload["contacts"][0]["profile"]["name"] == PROFILE_NAME
    assert payload["contacts"][0]["wa_id"] == phone(1)


def test_an_item_is_still_produced_when_the_envelope_has_no_metadata():
    # Storing it is still right: the row is a durable record of what arrived, and
    # VS-004 dead-letters what it cannot resolve. Dropping it here would lose a
    # real patient message to a shape change.
    items = extract_inbox_items(envelope(messages=[text_message(1)], with_metadata=False))

    assert len(items) == 1
    assert items[0].payload["metadata"] is None


def test_unknown_keys_inside_a_message_survive_the_round_trip():
    """Review Focus 7, the half a strict model would break.

    extra="ignore" would silently delete every key we did not declare - which is
    where VS-008's audio.id and Meta's next addition live.
    """
    body = envelope(
        messages=[text_message(1, referral={"source_type": "ad"}, context={"id": wamid(7)})]
    )

    item = extract_inbox_items(body)[0]

    assert item.payload["item"]["referral"] == {"source_type": "ad"}
    assert item.payload["item"]["context"] == {"id": wamid(7)}


def test_a_field_we_do_not_handle_produces_no_items():
    # Meta sends field types nobody subscribed to. 200 and nothing stored.
    body = envelope(field="account_update")
    body["entry"][0]["changes"][0]["value"] = {"phone_number": phone(1), "event": "VERIFIED"}

    assert extract_inbox_items(body) == []


def test_shapes_we_do_not_model_produce_no_items_and_no_exception():
    """Review Focus 5. None of these may raise; all of them answer 200 upstream."""
    shapes = [
        {},
        {"object": "whatsapp_business_account"},
        {"object": "whatsapp_business_account", "entry": []},
        {"entry": [{}]},
        {"entry": [{"changes": []}]},
        {"entry": [{"changes": [{"field": "messages"}]}]},
        {"entry": [{"changes": [{"field": "messages", "value": {}}]}]},
        # Structurally broken: entry is not a list of objects.
        {"entry": "nope"},
        {"entry": ["nope"]},
        {"entry": [{"changes": {"field": "messages"}}]},
        # Right shape, wrong element type.
        {"entry": [{"changes": [{"field": "messages", "value": {"messages": ["nope"]}}]}]},
        # Not even an object.
        [],
        "nope",
        None,
    ]
    for shape in shapes:
        assert extract_inbox_items(shape) == [], repr(shape)


def test_the_item_models_accept_unknown_fields():
    """The models say what we REQUIRE, not what Meta may send.

    A required `id` is the dedupe key; everything else is optional because Meta
    changes it without notice. If these models rejected unknown fields, every new
    WhatsApp feature would become a rejected patient message - and note that the
    extractor stores the raw dict either way, so a field missing from the model is
    still stored.
    """
    message = InboundMessage.model_validate(
        text_message(1, referral={"source_type": "ad"}, some_future_key=[1, 2, 3])
    )
    assert message.id == wamid(1)
    assert message.from_ == phone(1)
    assert message.type == "text"

    status = StatusUpdate.model_validate(
        status_update(1, "failed", errors=[{"code": 131047}], pricing={"billable": True})
    )
    assert status.id == wamid(1)
    assert status.status == "failed"
    assert status.recipient_id == phone(1)

    # Only the dedupe fields are required.
    assert InboundMessage.model_validate({"id": wamid(2)}).text is None
    assert StatusUpdate.model_validate({"id": wamid(2), "status": "sent"}).timestamp is None


def test_a_non_text_message_type_is_accepted_with_its_media_keys_intact():
    """VS-008 arrives as one of these, and must already be in webhook_inbox by
    the time anyone writes it. `text` is optional precisely so an audio note is
    not a validation failure."""
    audio = {
        "from": phone(1),
        "id": wamid(5),
        "timestamp": "1730000000",
        "type": "audio",
        "audio": {"id": "media-id-0001", "mime_type": "audio/ogg; codecs=opus", "voice": True},
    }

    item = extract_inbox_items(envelope(messages=[audio]))[0]

    assert item.provider_event_id == f"msg:{wamid(5)}"
    assert item.payload["item"]["type"] == "audio"
    assert item.payload["item"]["audio"]["id"] == "media-id-0001"


def test_an_item_without_an_id_is_stored_under_a_content_hash():
    """Assumption A1.

    An item that fails its model still has to be stored: skipping it would lose a
    patient message with nothing but a WARNING to show for it. The key is a hash
    of the item so hard rule 2 still holds - see the next test.
    """
    nameless = text_message(1)
    del nameless["id"]
    statusless = {"id": wamid(3), "recipient_id": phone(3)}
    body = envelope(messages=[nameless, text_message(2)], statuses=[statusless])

    items = extract_inbox_items(body)

    assert [i.provider_event_id for i in items] == [
        f"msg:sha256:{content_hash(nameless)}",
        f"msg:{wamid(2)}",
        f"status:sha256:{content_hash(statusless)}",
    ]
    # Stored raw and whole, exactly like an item that had an id.
    assert items[0].payload["item"] == nameless
    assert items[0].kind is InboxItemKind.MESSAGE
    assert items[2].kind is InboxItemKind.STATUS


def test_an_identical_redelivery_of_an_id_less_item_hashes_the_same():
    """Hard rule 2 for the fallback key.

    The hash is over canonical JSON - sorted keys, no whitespace - so Meta
    reordering the object between deliveries still produces one row. A genuinely
    different item must not collide with it.
    """
    nameless = text_message(1)
    del nameless["id"]
    reordered = dict(reversed(list(nameless.items())))
    assert list(reordered) != list(nameless)

    assert content_hash(reordered) == content_hash(nameless)

    different = dict(nameless, timestamp="1730000099")
    assert content_hash(different) != content_hash(nameless)
