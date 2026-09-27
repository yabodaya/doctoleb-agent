"""The WhatsApp webhook.

Hard rule 1: this endpoint verifies, deduplicates, stores and returns 200. That
is all. No OpenAI call, no Meta call, no media download, no tenant resolution,
no reply - and no enqueue yet: VS-004 adds it at the seam marked in receive().

Hard rule 8: every log line here carries identifiers and counts. A wamid is an
opaque Meta identifier, not patient content; a message body, a profile name and
a phone number are, and none of them may appear in a log, an exception, or a
response body.
"""

import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.whatsapp.payloads import extract_inbox_items
from app.channels.whatsapp.signature import SIGNATURE_HEADER, verify_signature
from app.config import Settings, get_settings
from app.db.repositories import WebhookInboxRepository
from app.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _matches(candidate: str, expected: str) -> bool:
    """Constant-time comparison of two secrets.

    Compared as bytes: hmac.compare_digest raises TypeError on a non-ASCII str,
    which a hostile query string supplies for free.
    """
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _forbidden() -> PlainTextResponse:
    return PlainTextResponse("forbidden", status_code=status.HTTP_403_FORBIDDEN)


@router.get("/whatsapp", response_class=PlainTextResponse)
async def verify(
    settings: Annotated[Settings, Depends(get_settings)],
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> PlainTextResponse:
    """Meta's subscription handshake, called once when the callback URL is saved.

    Returns hub.challenge as plain text, byte for byte. A JSON body would be the
    right value in quotes, and Meta's comparison would fail with no error
    anywhere that explains why.

    Every parameter is optional so that a missing one answers 403 rather than a
    422 describing our API to an unauthenticated caller.
    """
    expected = settings.meta_verify_token
    if not expected:
        # compare_digest("", "") is True, so an unset token would otherwise
        # authenticate anyone who sent an empty one.
        logger.warning("whatsapp handshake rejected: META_VERIFY_TOKEN is not set")
        return _forbidden()
    if hub_mode != "subscribe" or not hub_verify_token or not hub_challenge:
        logger.warning("whatsapp handshake rejected: incomplete request")
        return _forbidden()
    if not _matches(hub_verify_token, expected):
        logger.warning("whatsapp handshake rejected: verify token mismatch")
        return _forbidden()

    logger.info("whatsapp handshake verified")
    return PlainTextResponse(hub_challenge)


@router.post("/whatsapp", status_code=status.HTTP_200_OK)
async def receive(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    session: Annotated[AsyncSession, Depends(get_session)],
    signature: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
) -> dict[str, str]:
    """Verify, split, store, 200.

    Note what this signature does NOT contain: a pydantic body model. FastAPI
    would read and validate the body before this function ran - untrusted input
    parsed before authentication, and 422 answered to a request we never
    authenticated. The raw bytes are read here, by us, first.
    """
    raw_body = await request.body()

    if not verify_signature(raw_body, signature, settings.meta_app_secret):
        # No detail about which check failed, and never the header value: a
        # rejected caller learns "no" and nothing else.
        logger.warning("whatsapp webhook rejected: signature missing or invalid")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")

    try:
        payload = json.loads(raw_body)
    except ValueError:
        # Not 503: a retry cannot fix a body that is not JSON. Neither the log nor
        # the response echoes the body (hard rule 8).
        logger.warning("whatsapp webhook rejected: body is not valid JSON")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="malformed body"
        ) from None

    items = extract_inbox_items(payload)
    if not items:
        # A field type nobody subscribed to, or a shape Meta added. 200 with
        # nothing stored is the honest answer, and it stops Meta retrying
        # something that can never succeed.
        logger.info("whatsapp webhook accepted with no events to store")
        return {"status": "ok"}

    inbox = WebhookInboxRepository(session)
    new_event_ids: list[str] = []
    try:
        for item in items:
            # store_if_new is INSERT ... ON CONFLICT DO NOTHING (hard rule 2):
            # the database resolves the race between two deliveries, so there is
            # no SELECT-then-INSERT window and no IntegrityError to catch.
            row = await inbox.store_if_new(item.provider_event_id, item.payload)
            if row is not None:
                new_event_ids.append(item.provider_event_id)
        # One commit for the whole delivery: either every event in this request
        # is durable, or none is.
        await session.commit()
    except Exception as error:
        # The exception CLASS NAME only, and nothing else, ever.
        #
        # hide_parameters=True on the engine keeps SQLAlchemy from appending the
        # bound parameters, but it does not touch what PostgreSQL itself puts in
        # the message: the CONTEXT line on an invalid jsonb value quotes a snippet
        # of the JSON, and here that JSON is a patient's message (hard rule 8).
        # Logged before the rollback, so the original cause is recorded even if
        # the rollback then fails too.
        logger.error("whatsapp webhook storage failed error=%s", type(error).__name__)
        try:
            await session.rollback()
        except Exception as rollback_error:
            # A rollback can fail in its own right - a connection dropped mid
            # transaction is the ordinary case. Letting it escape would replace
            # our 503 with an unhandled 500 whose traceback carries the ORIGINAL
            # storage error as its context, printed by uvicorn: exactly the leak
            # this branch exists to prevent. Class name only, then carry on.
            logger.error("whatsapp webhook rollback failed error=%s", type(rollback_error).__name__)
        # 503, not a re-raise. Re-raising would answer 500 and hand uvicorn's
        # exception logger the full traceback, message included - which is the
        # leak above, written to the log by a component we do not control.
        #
        # What actually keeps that traceback out of the logs is that Starlette
        # handles HTTPException itself and turns it into an ordinary response, so
        # nothing ever formats it as an unhandled error. `from None` is belt and
        # braces on top: it sets __suppress_context__, so a formatter that did
        # print this exception would not chain back to the storage error.
        # It does NOT clear __context__ - the reference is still there.
        #
        # Still a retryable status: Meta treats 503 like 500 and redelivers, and
        # dedupe makes the retry safe. Answering 200 here would lose the message
        # permanently to a transient database outage.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="storage unavailable"
        ) from None

    logger.info(
        "whatsapp webhook stored events=%d new=%d ids=%s",
        len(items),
        len(new_event_ids),
        ",".join(new_event_ids),
    )

    # --- VS-004 seam -------------------------------------------------------
    # VS-004 enqueues one job per id in `new_event_ids`, right here: after the
    # commit (a job must never see a row that is not committed yet) and before
    # the return. Nothing above this line changes, and duplicates are already
    # filtered out - `new_event_ids` holds only rows this request created.
    # ----------------------------------------------------------------------
    return {"status": "ok"}
