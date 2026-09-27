"""Meta webhook payload models, and the split into things we store.

Two properties, deliberately in tension:

Tolerant. Meta adds fields and sends field types nobody subscribed to. A model
that rejected them would turn every schema change on Meta's side into lost
patient messages, so every model rejects nothing, every list defaults to empty,
and extract_inbox_items() never raises. "We do not understand this" becomes an
empty list, which the endpoint turns into a plain 200.

Lossless. The models locate and validate items; they do NOT define what gets
stored. model_dump() of a narrowed model would delete exactly the keys we failed
to predict - text.body's siblings, VS-008's audio.id, a status's pricing and
errors - from the only place VS-004 and VS-008 can read them. Hence
extra="allow" throughout AND a raw dict as the stored payload: the models say
what we require (a message needs an id; a status needs an id and a status), and
the original object is what lands in webhook_inbox.payload.

Nothing is dropped either. An item that fails its model is stored under a
content-hash key rather than skipped, because a skipped item is a lost patient
message with only a WARNING to show for it.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class InboxItemKind(StrEnum):
    MESSAGE = "message"
    STATUS = "status"


@dataclass(frozen=True)
class InboxItem:
    """One thing to store: its dedupe key, its kind, and its self-contained payload."""

    provider_event_id: str
    kind: InboxItemKind
    payload: dict[str, Any]


class _Tolerant(BaseModel):
    # allow, not ignore. See the module docstring: ignore would strip the keys we
    # did not think of, and those are precisely the ones later slices need.
    # Nothing here ever reads an extra key by attribute; we only dump.
    model_config = ConfigDict(extra="allow")


class Metadata(_Tolerant):
    # phone_number_id is how VS-004 resolves the tenant (hard rule 4). Optional
    # here because a missing one must not cost us the message.
    phone_number_id: str | None = None
    display_phone_number: str | None = None


class InboundMessage(_Tolerant):
    """One inbound message, as far as this slice needs to understand it.

    `id` is the only required field: it is the dedupe key (hard rule 2), and it is
    the one thing we cannot do without. Everything else is optional because Meta
    changes message shapes without notice and because this slice interprets
    nothing - `type` is carried for VS-004's routing and `text` for its message
    row, and a voice note (type="audio") must validate here in VS-003 so that
    VS-008 finds it already in webhook_inbox.

    `from_` is aliased: `from` is a Python keyword. The trailing underscore never
    reaches the database - the stored payload is the raw dict.

    min_length=1 on `id`: an empty string would otherwise satisfy `str` and
    produce the key `msg:`, which every id-less message in the world would share.
    An empty id must take the content-hash path instead.
    """

    id: str = Field(min_length=1)
    from_: str | None = Field(default=None, alias="from")
    timestamp: str | None = None
    type: str | None = None
    text: dict[str, Any] | None = None


class StatusUpdate(_Tolerant):
    """One delivery-status callback.

    `id` (the wamid of the message WE sent) and `status` are both required: both
    are part of the dedupe key, because sent/delivered/read for one message are
    three separate events. `errors` is declared so VS-004 can see why a send
    failed without re-deriving the shape. min_length on both key fields, for the
    same reason as InboundMessage.id.
    """

    id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    recipient_id: str | None = None
    timestamp: str | None = None
    errors: list[Any] | None = None


class ChangeValue(_Tolerant):
    messaging_product: str | None = None
    metadata: Metadata | None = None
    # list[Any], not list[dict]: one malformed element must not invalidate the
    # whole change and lose the messages next to it. The extractor filters.
    contacts: list[Any] = Field(default_factory=list)
    messages: list[Any] = Field(default_factory=list)
    statuses: list[Any] = Field(default_factory=list)


class Change(_Tolerant):
    field: str | None = None
    value: ChangeValue | None = None


class Entry(_Tolerant):
    id: str | None = None
    changes: list[Change] = Field(default_factory=list)


class MetaWebhookEnvelope(_Tolerant):
    object: str | None = None
    entry: list[Entry] = Field(default_factory=list)


def extract_inbox_items(payload: Any) -> list[InboxItem]:
    """Split one webhook body into one InboxItem per message and per status.

    Never raises, and never logs content (hard rule 8): the most it says about an
    unusable payload is its shape.

    Returning [] means "nothing here to store", which the caller turns into a 200
    with no rows - never a 500. Meta would retry a 500 forever for a payload that
    will never change.
    """
    if not isinstance(payload, dict):
        logger.warning("whatsapp webhook: body is not a JSON object")
        return []
    try:
        envelope = MetaWebhookEnvelope.model_validate(payload)
    except ValidationError:
        # Only a structurally broken envelope reaches here (entry not a list of
        # objects). Unknown *fields* never do - that is what extra="allow" and
        # the list[Any] leaves are for.
        logger.warning("whatsapp webhook: unrecognised envelope shape")
        return []

    items: list[InboxItem] = []
    for entry in envelope.entry:
        for change in entry.changes:
            if change.value is None:
                continue
            items.extend(_items_from_change(envelope.object, entry.id, change))
    return items


def _items_from_change(obj: str | None, entry_id: str | None, change: Change) -> list[InboxItem]:
    value = change.value
    if value is None:
        return []
    # Dumped once, so the stored metadata keeps any key Meta added to it.
    raw_metadata = value.model_dump().get("metadata")
    contacts = [c for c in value.contacts if isinstance(c, dict)]
    envelope_fields: dict[str, Any] = {
        "object": obj,
        "entry_id": entry_id,
        "field": change.field,
        "metadata": raw_metadata,
    }

    items: list[InboxItem] = []
    for message in value.messages:
        key = _message_key(message)
        if key is None:
            continue
        items.append(
            InboxItem(
                provider_event_id=key,
                kind=InboxItemKind.MESSAGE,
                payload={
                    "kind": InboxItemKind.MESSAGE.value,
                    **envelope_fields,
                    # The profile name lives here, not on the message. VS-004
                    # matches it to the message by wa_id.
                    "contacts": contacts,
                    # The RAW dict, not the validated model.
                    "item": message,
                },
            )
        )
    for status in value.statuses:
        key = _status_key(status)
        if key is None:
            continue
        items.append(
            InboxItem(
                provider_event_id=key,
                kind=InboxItemKind.STATUS,
                payload={
                    "kind": InboxItemKind.STATUS.value,
                    **envelope_fields,
                    "item": status,
                },
            )
        )
    return items


def content_hash(item: dict[str, Any]) -> str:
    """sha256 over the item's CANONICAL JSON.

    sort_keys plus the tight separators mean two byte-different renderings of the
    same object hash identically, so a redelivery that reordered keys or changed
    spacing still deduplicates (hard rule 2). default=str so an unexpected
    non-JSON value cannot raise on the fallback path - the fallback exists to stop
    us losing data, and it must not be the thing that loses it.
    """
    canonical = json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _message_key(item: Any) -> str | None:
    """`msg:<wamid>`, or `msg:sha256:<hex>` when the item fails its model.

    An item we cannot read is still stored. The hash key keeps hard rule 2 true
    for it - an identical redelivery hashes identically and the unique constraint
    collapses the two.

    None only for something that is not a JSON object at all, which carries
    nothing to store. The log names the kind; the item is patient content.
    """
    if not isinstance(item, dict):
        logger.warning("whatsapp webhook: skipping non-object item kind=%s", InboxItemKind.MESSAGE)
        return None
    try:
        return f"msg:{InboundMessage.model_validate(item).id}"
    except ValidationError:
        logger.warning("whatsapp webhook: message item has no usable id, keying by content hash")
        return f"msg:sha256:{content_hash(item)}"


def _status_key(item: Any) -> str | None:
    """`status:<wamid>:<status>`, or `status:sha256:<hex>` on a model failure.

    The status word is part of the key: sent, delivered and read for one message
    are three events, while the same status delivered twice is one.
    """
    if not isinstance(item, dict):
        logger.warning("whatsapp webhook: skipping non-object item kind=%s", InboxItemKind.STATUS)
        return None
    try:
        status = StatusUpdate.model_validate(item)
    except ValidationError:
        logger.warning(
            "whatsapp webhook: status item has no usable id or status, keying by content hash"
        )
        return f"status:sha256:{content_hash(item)}"
    return f"status:{status.id}:{status.status}"
