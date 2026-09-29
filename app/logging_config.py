"""Logging setup, called once by the api app and once by the worker."""

import logging

from app.config import get_settings

# Third-party loggers that must never follow LOG_LEVEL down to DEBUG (hard rule 8,
# VS-005 requirement 8). Verified against openai 3.20.0: at DEBUG the SDK itself
# logs method, status and request id only, never a body - so this is defence in
# depth against a future SDK version, and against httpcore2's DEBUG traces, which
# print response headers.
#
# httpx keeps INFO: its one line per request names the Meta URL and the status,
# never a body, and it is useful when a reply does not arrive. httpx2's line for
# OpenAI adds nothing the job's own "reply generated" line does not say.
_THIRD_PARTY_FLOORS: dict[str, int] = {
    "openai": logging.WARNING,
    "httpx2": logging.WARNING,
    "httpcore2": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpx": logging.INFO,
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
