"""Logging setup, called once by the api app and once by the worker."""

import logging

from app.config import get_settings


def configure_logging() -> None:
    """Configure the root logger from LOG_LEVEL.

    Hard rule 8: we log identifiers, never patient content. This formatter
    prints only what a caller explicitly passes, so keep message bodies,
    transcripts, names and phone numbers out of log calls in later slices.
    """
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
