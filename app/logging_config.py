"""Logging setup, called once by the api app and once by the worker."""

import logging

from app.config import get_settings

# Third-party loggers that must never follow LOG_LEVEL down to DEBUG (hard rule 8,
# VS-005 requirement 8). Verified against openai 3.20.0: at DEBUG the SDK itself
# logs method, status and request id only, never a body - so this is defence in
# depth against a future SDK version, and against httpcore2's DEBUG traces, which
# print response headers.
#
# httpx2's line for OpenAI adds nothing the job's own "reply generated" line
# does not say.
#
# httpx was at INFO until VS-008, for a reason that has stopped being true. Its
# one line per request prints the full URL, and until this slice every URL we
# gave httpx was `graph.facebook.com/<version>/<phone_number_id>/messages` - a
# clinic id and a path, useful when a reply does not arrive and harmless to
# keep. VS-008 hands the same client a MEDIA URL, which is a short-lived SIGNED
# link, which is to say a CREDENTIAL: anybody who can read the logs can fetch
# the patient's audio with it. One line of httpx INFO would undo the whole of
# why app/channels/whatsapp/media.py never logs, stores or reprs that URL
# itself (hard rule 8). So httpx joins the others at WARNING, and what replaces
# that line is media.py's own: an event id, a hostname, a byte count and a code.
_THIRD_PARTY_FLOORS: dict[str, int] = {
    "openai": logging.WARNING,
    "httpx2": logging.WARNING,
    "httpcore2": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpx": logging.WARNING,
}


def configure_logging(level: str | None = None) -> None:
    """Configure the root logger from LOG_LEVEL, and pin the loggers we do not own.

    Hard rule 8: we log identifiers, never patient content. This formatter
    prints only what a caller explicitly passes, so keep message bodies,
    transcripts, names and phone numbers out of log calls in later slices.

    `level` is for tests and for callers that already know what they want;
    None reads LOG_LEVEL from settings, which is what both processes do.
    """
    logging.basicConfig(
        level=(level or get_settings().log_level).upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    root_level = logging.getLogger().getEffectiveLevel()
    # max(): a floor, not an override - LOG_LEVEL=ERROR must stay quiet. Set on
    # every call, and always after `import openai` has run (the worker imports it
    # at module level), so an OPENAI_LOG=debug in the environment is undone.
    for name, floor in _THIRD_PARTY_FLOORS.items():
        logging.getLogger(name).setLevel(max(floor, root_level))
