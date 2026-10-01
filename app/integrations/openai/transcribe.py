"""The one module in this repo that calls the OpenAI audio endpoint.

The same discipline as `chat.py`, for the same reasons: one attempt, one
wall-clock deadline, one classifier, and nothing outside this file ever sees an
SDK exception or an HTTP status. The job gets a `TranscriptionResult` with an
outcome and a reason code, and decides from those alone (hard rule 11).

Deliberately NOT re-exported from `app/integrations/openai/__init__.py`:
`app/agent/` imports that package, and this module imports the SDK. The worker
imports this class explicitly, exactly as it does `OpenAIChatClient`.
"""

import asyncio

import httpx2
from openai import AsyncOpenAI

from app.config import Settings
from app.integrations.openai.errors import classify_openai_error
from app.integrations.openai.interface import ChatOutcome, TranscriptionResult


def read_transcription(answer: object) -> TranscriptionResult:
    """The only place a 2xx becomes a transcript, or a reason it is not one.

    `getattr` throughout rather than attribute access, for the same reason
    `read_completion` uses it: the SDK's response validation is non-strict, so
    a 2xx whose body is not what we asked for comes back as a plain `str`
    rather than raised (verified in 3.20.0). A 200 that is not a transcription
    is most likely a proxy's error page, so it is RETRYABLE.

    The text is returned EXACTLY as the API gave it. Deciding whether it is a
    message at all belongs to `transcripts.unusable_reason`, which the job calls
    - normalising here would mean the transcript we store is not the transcript
    we were given.
    """
    text = getattr(answer, "text", None)
    if not isinstance(text, str):
        return TranscriptionResult(ChatOutcome.RETRYABLE, "openai_transcribe_bad_response")
    usage = getattr(answer, "usage", None)
    seconds = getattr(usage, "seconds", None)
    return TranscriptionResult(
        ChatOutcome.SUCCESS,
        "ok",
        text,
        float(seconds)
        if isinstance(seconds, int | float) and not isinstance(seconds, bool)
        else None,
    )


class OpenAITranscribeClient:
    """TranscribeClient over the OpenAI SDK. One per worker process.

    The httpx2 client is injectable for the same reason `OpenAIChatClient`'s is:
    every test passes one wired to an `httpx2.MockTransport`, so nothing in this
    repo's test suite can reach OpenAI.

    `max_retries=0`: one retry layer, the job's. Five attempts inside five job
    tries is twenty-five calls behind a dead letter that says five - and each
    one of those is a transcription somebody pays for.
    """

    def __init__(self, settings: Settings, http_client: httpx2.AsyncClient | None = None) -> None:
        self._model = settings.openai_transcribe_model.strip()
        self._deadline = settings.openai_transcribe_timeout_seconds
        # Not built without a key: the SDK raises "Missing credentials" for an
        # empty one, and the worker must boot with no OpenAI account at all.
        # transcribe() reports it as openai_api_key_unset instead.
        self._sdk = (
            AsyncOpenAI(
                api_key=settings.openai_api_key,
                max_retries=0,
                timeout=self._deadline,
                http_client=http_client,
            )
            if settings.openai_api_key
            else None
        )

    async def transcribe(
        self, audio: bytes, *, filename: str, content_type: str
    ) -> TranscriptionResult:
        """One attempt at one transcription. Never a retry.

        Nothing here logs: the job writes one line per voice step, because only
        the job knows the `webhook_inbox` row id every other line carries. And
        what it would have to log is the transcript (hard rule 8).

        `file` is a `(filename, bytes, content_type)` tuple, which the SDK
        accepts - so the audio never needs a file on disk, which is what makes
        W1's "the audio is never stored" true all the way down.

        Four things are deliberately NOT sent:

          * `language` - the patient's language is unknown and often mixed.
            `language="ar"` would help Arabic and break English, French and
            Arabizi, all of which are expected in Lebanon.
          * `prompt` - it biases the output toward whatever we wrote, and it is
            a place clinic data could leak into a third party's request.
          * `temperature`, `timestamp_granularities`, `stream`,
            `chunking_strategy` - nothing here needs them.

        `response_format="json"` because that is the only format the current
        models support, which is also why there is no `duration` to read (plan
        conflict C15).
        """
        if self._sdk is None:
            return TranscriptionResult(ChatOutcome.PERMANENT, "openai_api_key_unset")
        if not self._model:
            return TranscriptionResult(ChatOutcome.PERMANENT, "openai_transcribe_model_unset")
        try:
            # The SDK's timeout is per connection phase; this is the whole call.
            # It sits OUTSIDE the turn budget - the turn has not started yet -
            # and is one of the three terms JOB_TIMEOUT_SECONDS has to cover.
            async with asyncio.timeout(self._deadline):
                answer = await self._sdk.audio.transcriptions.create(
                    model=self._model,
                    file=(filename, audio, content_type),
                    response_format="json",
                )
        except Exception as error:  # noqa: BLE001 - re-raised unless it is ours
            classified = classify_openai_error(error)
            if classified is None:
                raise
            return TranscriptionResult(*classified)
        return read_transcription(answer)

    async def aclose(self) -> None:
        """Close the SDK's own connection pool, which is not the worker's httpx one."""
        if self._sdk is not None:
            await self._sdk.close()
