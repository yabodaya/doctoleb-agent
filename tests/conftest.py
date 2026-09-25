# ruff: noqa: E402
"""Shared test setup.

app.config.Settings has required fields, so the environment must be populated
before anything imports it. That is why these assignments sit above the imports.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://doctoleb:doctoleb@localhost:5432/doctoleb"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
