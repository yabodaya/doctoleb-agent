"""Hard rule 8, defended against the loggers we do not own.

`configure_logging` calls `basicConfig(force=True)`, which removes every root
handler - including pytest's caplog handler. That is why the behavioural test
attaches its own collecting handler AFTER calling it, and why a positive
control is asserted: without one, a handler that captured nothing at all would
make "no leak" pass vacuously.
"""

import logging

import httpx2
import pytest

from app.config import Settings
from app.integrations.openai.chat import OpenAIChatClient
from app.logging_config import configure_logging

_WATCHED = ("openai", "httpx2", "httpcore2", "httpcore", "httpx")


@pytest.fixture(autouse=True)
def restore_logging():
    """Put the logging tree back, or every later test runs without caplog."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {name: logging.getLogger(name).level for name in _WATCHED}
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


class Collector(logging.Handler):
    def __init__(self):
        super().__init__(level=0)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def test_debug_does_not_open_the_sdk_or_transport_loggers():
    """LOG_LEVEL=DEBUG is something a developer sets to debug OUR code.

    It must not also turn on httpcore2's DEBUG traces, which print response
    headers, or a future SDK version's request logging.

    VS-008 moved `httpx` into this list, from INFO. Its one line per request
    prints the whole URL, and from this slice one of the URLs we give it is a
    signed media link - a credential. See the comment in app/logging_config.py.
    """
    configure_logging("DEBUG")

    for name in ("openai", "httpx2", "httpcore2", "httpcore", "httpx"):
        assert logging.getLogger(name).level >= logging.WARNING, name


def test_a_quieter_root_level_still_wins():
    """A floor, never a way to make a quiet deployment noisier."""
    configure_logging("ERROR")

    for name in _WATCHED:
        assert logging.getLogger(name).level >= logging.ERROR, name


def test_configure_logging_overrides_a_level_the_sdk_set_itself():
    """`import openai` runs setup_logging(), which sets the `openai` logger's
    level when OPENAI_LOG is in the environment (verified in 3.20.0).

    The worker imports the SDK at module level, so the floors are applied after
    that has already happened - and must win.
    """
    logging.getLogger("openai").setLevel(logging.DEBUG)

    configure_logging("DEBUG")

    assert logging.getLogger("openai").level == logging.WARNING


async def test_a_debug_run_of_the_openai_client_logs_nothing_it_should_not():
    """The whole rule, exercised rather than asserted on levels.

    One successful call with a sentinel prompt, patient text, key and reply,
    and one 400 whose `message` holds a sentinel - the two ways OpenAI content
    could reach a log line.
    """
    sentinels = {
        "SENTINEL-system-prompt",
        "SENTINEL-patient-text",
        "SENTINEL-generated-reply",
        "SENTINEL-openai-error-body",
        "sk-SENTINEL-not-a-real-key",
    }
    configure_logging("DEBUG")
    collector = Collector()
    logging.getLogger().addHandler(collector)

    # The positive control: without it, a handler that captured nothing would
    # make every assertion below pass for the wrong reason.
    logging.getLogger("app.test").debug("positive control")

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:pw@localhost:5432/doctoleb",
        redis_url="redis://localhost:6379/0",
        openai_api_key="sk-SENTINEL-not-a-real-key",
        openai_chat_model="test-model",
    )
    responses = iter(
        [
            httpx2.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1730000000,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "SENTINEL-generated-reply",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                },
            ),
            httpx2.Response(
                400,
                json={
                    "error": {
                        "message": "SENTINEL-openai-error-body",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": "context_length_exceeded",
                    }
                },
            ),
        ]
    )

    async def transport(request):
        return next(responses)

    from app.integrations.openai import ChatMessage

    client = OpenAIChatClient(
        settings, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
    )
    messages = [
        ChatMessage("system", "SENTINEL-system-prompt"),
        ChatMessage("user", "SENTINEL-patient-text"),
    ]
    assert (await client.complete(messages)).text == "SENTINEL-generated-reply"
    assert (await client.complete(messages)).outcome.value == "PERMANENT"

    lines = [record.getMessage() for record in collector.records]
    assert "positive control" in lines
    for sentinel in sentinels:
        assert not any(sentinel in line for line in lines), sentinel
    noisy = [
        record.name
        for record in collector.records
        if record.levelno < logging.WARNING
        and (record.name.startswith(("openai", "httpx2", "httpcore2")))
    ]
    assert noisy == []


async def test_a_debug_run_with_tool_calls_logs_nothing_it_should_not():
    """The same rule, now across a whole tool round trip (VS-006).

    Tool calling adds three new ways content could reach a log line: the
    ARGUMENTS the model writes (which can quote the patient), the RESULTS we
    send back (clinic data, and in VS-011 clinic-configured free text), and an
    unknown tool NAME, which is model-written too.

    Two calls: one that asks for a tool with a sentinel name and sentinel
    arguments, and one that sends a sentinel tool result back and gets a
    sentinel reply.
    """
    sentinels = {
        "SENTINEL-tool-arguments",
        "SENTINELNAME-unknown-tool",
        "SENTINEL-tool-result",
        "SENTINEL-interim-text",
        "SENTINEL-final-reply",
        "sk-SENTINEL-not-a-real-key",
    }
    configure_logging("DEBUG")
    collector = Collector()
    logging.getLogger().addHandler(collector)
    logging.getLogger("app.test").debug("positive control")  # as above

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:pw@localhost:5432/doctoleb",
        redis_url="redis://localhost:6379/0",
        openai_api_key="sk-SENTINEL-not-a-real-key",
        openai_chat_model="test-model",
    )

    def body(message: dict, finish: str) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1730000000,
                "model": "test-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            },
        )

    responses = iter(
        [
            body(
                {
                    "role": "assistant",
                    "content": "SENTINEL-interim-text",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "SENTINELNAME-unknown-tool",
                                "arguments": '{"note":"SENTINEL-tool-arguments"}',
                            },
                        }
                    ],
                },
                "tool_calls",
            ),
            body({"role": "assistant", "content": "SENTINEL-final-reply"}, "stop"),
        ]
    )

    async def transport(request):
        return next(responses)

    from app.integrations.openai import ChatMessage, ToolSpec

    client = OpenAIChatClient(
        settings, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport))
    )
    tools = (
        ToolSpec(
            "list_doctors",
            "List the clinic's doctors.",
            {"type": "object", "properties": {}, "additionalProperties": False},
        ),
    )
    first = await client.complete([ChatMessage("user", "is Dr. Karim free?")], tools)
    assert first.tool_calls[0].name == "SENTINELNAME-unknown-tool"

    second = await client.complete(
        [
            ChatMessage("user", "is Dr. Karim free?"),
            ChatMessage("assistant", first.text, tool_calls=first.tool_calls),
            ChatMessage("tool", '{"error":"SENTINEL-tool-result"}', tool_call_id="call_1"),
        ],
        tools,
    )
    assert second.text == "SENTINEL-final-reply"

    lines = [record.getMessage() for record in collector.records]
    assert "positive control" in lines
    for sentinel in sentinels:
        assert not any(sentinel in line for line in lines), sentinel
    noisy = [
        record.name
        for record in collector.records
        if record.levelno < logging.WARNING
        and (record.name.startswith(("openai", "httpx2", "httpcore2")))
    ]
    assert noisy == []


# --------------------------------------------------------------------------
# VS-008: the media URL is a credential, and httpx used to print it
# --------------------------------------------------------------------------


async def test_a_debug_media_download_never_logs_the_signed_url():
    """The leak this slice found, and the reason `httpx` is now at WARNING.

    httpx logs one INFO line per request containing the FULL URL. Until VS-008
    every URL this repo gave httpx was
    `graph.facebook.com/<version>/<phone_number_id>/messages` - a clinic id and
    a path, which is why VS-004 deliberately left httpx at INFO. VS-008 hands
    the same client a media URL, which is a short-lived SIGNED link: a
    credential that fetches the patient's audio. One httpx INFO line would have
    undone the whole of why app/channels/whatsapp/media.py never logs, stores or
    reprs that URL itself.

    Driven through the REAL client with a real `configure_logging("DEBUG")` -
    the worst case, since DEBUG is what a developer sets while chasing exactly
    this kind of problem - because the floor is the thing under test and
    `caplog.at_level` would bypass it.

    The positive control matters as much as the assertion: without it, a
    collector that captured nothing would make "no leak" pass vacuously.
    """
    import httpx

    from app.channels.whatsapp.media import MediaClient
    from tests.whatsapp_factories import SYNTHETIC_OGG, media_lookup_body

    signed = "https://lookaside.fbsbx.com/attachments/?mid=x&hash=SENTINELSIGNEDLINK"
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:pw@db:5432/doctoleb",
        redis_url="redis://cache:6379/1",
        meta_access_token="sk-SENTINEL-not-a-real-token",
    )

    def transport(request: httpx.Request) -> httpx.Response:
        if "lookaside" in request.url.host:
            return httpx.Response(200, content=SYNTHETIC_OGG)
        return httpx.Response(200, json=media_lookup_body(url=signed))

    configure_logging("DEBUG")
    collector = Collector()
    logging.getLogger().addHandler(collector)
    logging.getLogger("app.test").debug("positive control")

    client = MediaClient(httpx.AsyncClient(transport=httpx.MockTransport(transport)), settings)
    looked_up = await client.lookup("media-id-0000001", "100000000000001", event_id="e")
    assert looked_up.ref is not None
    assert (await client.download(looked_up.ref, event_id="e")).succeeded

    lines = [record.getMessage() for record in collector.records]
    assert "positive control" in lines
    for sentinel in ("SENTINELSIGNEDLINK", "sk-SENTINEL-not-a-real-token", "hash="):
        assert not any(sentinel in line for line in lines), sentinel
    # And httpx is silent below WARNING, which is what makes the above true for
    # every future URL rather than only for the two in this test.
    noisy = [
        record.name
        for record in collector.records
        if record.levelno < logging.WARNING and record.name.startswith(("httpx", "httpcore"))
    ]
    assert noisy == []
