# VS-001 Project Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable FastAPI project with Docker Compose (api, worker, postgres, redis), env-driven config, and health/readiness endpoints.

**Architecture:** One Python package (`app`) and one Docker image, run as two processes: `api` (uvicorn serving the FastAPI app) and `worker` (arq polling Redis). Configuration comes only from environment variables via pydantic-settings. `GET /health` is liveness and does no I/O; `GET /health/ready` is readiness and probes Postgres and Redis through FastAPI dependencies, which is what makes both outcomes testable without running either service.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2 + pydantic-settings, SQLAlchemy 2.x (async) + asyncpg, redis-py (asyncio), arq, uv, pytest + pytest-asyncio, ruff, Docker Compose.

**Spec:** `docs/slices/VS-001.md` (scope and acceptance), with `CLAUDE.md` (hard rules) and `docs/architecture.md` (system shape) as binding context.

## Global Constraints

- Python 3.12 only. `requires-python = ">=3.12,<3.13"`, and a `.python-version` file containing `3.12`.
- Dependency manager is **uv**. `pyproject.toml` + a committed `uv.lock`. Docker installs with `uv sync --frozen`.
- The uv version is pinned in the Dockerfile: `ghcr.io/astral-sh/uv:0.12.19`, the version installed on this machine. The tag is hardcoded with a comment saying to keep it in sync with the local uv (`uv --version`), and Task 4 verifies the container's uv matches.
- Readiness probes are bounded: every dependency probe runs under `asyncio.timeout(2)`, so a hung dependency produces a 503 rather than a request that never returns. This is the readiness-endpoint case of hard rule 11 (external calls have timeouts).
- Secrets and configuration come only from environment variables (hard rule 9). No credential, token or DSN literal in `app/` source. `.env` is never committed; `.env.example` lists every key.
- Model names, tokens and URLs are never hardcoded (`CLAUDE.md` Stack).
- Logs carry identifiers, never patient content, and never secrets (hard rules 8 and 9). Probe failures log the dependency name only, never the exception text.
- Linting: `ruff check .` must be clean and `ruff format .` must leave no diff. Line length 100, target `py312`.
- Tests: `pytest` must pass with no real Postgres or Redis running.
- Stay inside VS-001 scope. Out of scope: DB models, migrations, webhooks, OpenAI, Meta client, booking client. Anything that looks needed but is out of scope goes under "Follow-ups" in `docs/slices/VS-001.md`, not into the code.
- Every commit message ends with the trailer:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

### One documented deviation from the slice text

VS-001 says "empty arq worker that starts". arq's `Worker` refuses to start when neither a function nor a cron job is registered. Task 4 therefore registers exactly one trivial job, `ping`, which returns `"pong"`. This is the minimum needed to satisfy "a worker that starts", and it gives Task 4 a way to prove the worker actually consumes from Redis. No other job is added; VS-004 owns the real jobs.

## Review Focus

Five conditions the slice implies but does not spell out. Each has a test in the task that owns the code.

1. **A `.env` holding keys this slice does not model must not crash startup.** `.env.example` already lists `META_*`, `OPENAI_*` and `BOOKING_*` keys for later slices. pydantic-settings defaults to `extra="forbid"` and raises `ValidationError` on unknown dotenv keys, so the app would refuse to boot from the project's own example file. Test in Task 1.
2. **A failing dependency probe must not leak the database password into logs.** The DSN is embedded in connection errors; logging the exception text would write `doctoleb:<password>@postgres` to stdout, breaking hard rule 9. Test in Task 3.
3. **`/health` must stay 200 while Postgres and Redis are down.** If liveness touched dependencies, a database blip would make an orchestrator kill a perfectly healthy API process. Test in Task 3.
4. **`/health/ready` must answer 503, not 500, for any probe failure — including exception types nobody predicted.** A readiness endpoint that raises is a readiness endpoint that tells you nothing. Test in Task 3 with a non-connection exception.
5. **A missing required setting must fail loudly at startup, not silently start a broken API.** Test in Task 1.
6. **A dependency that hangs rather than refusing must not hang the readiness endpoint.** A Postgres accepting TCP but never answering, or a Redis wedged mid-failover, makes an unbounded probe block until the client gives up — the probe reports nothing and the request never returns. Bounded with `asyncio.timeout(2)`; test in Task 3.

---

### Task 1: Project scaffolding, tooling and configuration

Sets up the package, the dependency manager, lint/test config, and the settings object every later module imports. Ends with a green (if small) test suite.

**Files:**
- Create: `pyproject.toml`
- Create: `.python-version`
- Create: `uv.lock` (generated, committed)
- Create: `.env` (local only, git-ignored — copied from `.env.example`)
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `app/logging_config.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Modify: `CLAUDE.md` (add uv to the Stack section)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `app.config.Settings` — pydantic-settings model with fields `app_env: str`, `log_level: str`, `database_url: str` (required), `redis_url: str` (required).
  - `app.config.get_settings() -> Settings` — process-wide cached accessor.
  - `app.logging_config.configure_logging() -> None` — idempotent root logger setup.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "doctoleb-agent"
version = "0.1.0"
description = "WhatsApp messaging + AI layer of Doctoleb"
readme = "README.md"
requires-python = ">=3.12,<3.13"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30",
    "redis>=5.2",
    "arq>=0.26",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
    "ruff>=0.7",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

`[dependency-groups]` (not `optional-dependencies`) is uv's native dev group: `uv sync` installs it by default, which is what we want, because `CLAUDE.md` runs `pytest` inside the api container.

- [ ] **Step 2: Create `.python-version`**

```
3.12
```

The host may have a different Python. uv reads this file and fetches 3.12 automatically.

- [ ] **Step 3: Confirm the installed uv version**

uv is already installed. Check it:

```bash
uv --version
```

Expected: `uv 0.12.19 (...)`. This is the version Task 4 hardcodes as the Docker image tag
(`ghcr.io/astral-sh/uv:0.12.19`), so the uv that builds the image is the same uv that runs
on this machine. If the local uv is ever upgraded, bump the tag in the Dockerfile to match.

- [ ] **Step 4: Generate the lockfile and install**

Run:
```bash
uv lock
uv sync
```
Expected: `uv.lock` is created and a `.venv` appears with all dependencies.

- [ ] **Step 5: Create the local `.env`**

```bash
cp .env.example .env
```

Expected: `.env` exists and is ignored by git (`.gitignore` already lists it — confirm with `git status --short`, which must not show it).

Do this now, not later: from Task 2 onward, anything that starts the app for real calls `get_settings()`, and `DATABASE_URL` and `REDIS_URL` are required with no defaults. Without `.env` the app refuses to boot. The shipped values point at the hostnames `postgres` and `redis`, which only resolve inside Compose — that is fine, because the only pre-Compose step that starts the app (Task 2, Step 8) hits `/health`, which does no I/O.

- [ ] **Step 6: Create the package markers**

```bash
mkdir -p app tests
touch app/__init__.py tests/__init__.py
```

- [ ] **Step 7: Write the failing config tests**

Create `tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def test_settings_reads_values_from_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://user:pw@db:5432/doctoleb\n"
        "REDIS_URL=redis://cache:6379/1\n"
    )

    settings = Settings(_env_file=env_file)

    assert settings.database_url == "postgresql+asyncpg://user:pw@db:5432/doctoleb"
    assert settings.redis_url == "redis://cache:6379/1"


def test_settings_ignores_env_file_keys_this_slice_does_not_model(tmp_path, monkeypatch):
    """Review Focus 1.

    .env.example already lists keys later slices need. pydantic-settings
    defaults to extra="forbid" and would reject the whole file because of them.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://user:pw@db:5432/doctoleb\n"
        "REDIS_URL=redis://cache:6379/1\n"
        "META_ACCESS_TOKEN=placeholder\n"
        "OPENAI_CHAT_MODEL=placeholder\n"
        "BOOKING_CLIENT=fake\n"
    )

    settings = Settings(_env_file=env_file)

    assert settings.redis_url == "redis://cache:6379/1"


def test_settings_fails_loudly_when_a_required_value_is_missing(monkeypatch):
    """Review Focus 5. A broken config must stop the process, not start a half-app."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_get_settings_returns_the_same_object_every_call():
    assert get_settings() is get_settings()
```

- [ ] **Step 8: Run the tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.config'`.

- [ ] **Step 9: Write `app/config.py`**

```python
"""Application settings.

Hard rule 9: configuration and secrets come only from environment variables.
Nothing in this module carries a literal credential.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore" is required, not cosmetic. pydantic-settings defaults to
    # extra="forbid" and raises on any dotenv key the model does not declare.
    # .env.example already lists META_*, OPENAI_* and BOOKING_* keys for later
    # slices, so without this the app cannot boot from its own example file.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"

    # No defaults: a missing DSN must stop the process rather than silently
    # point at something wrong, and a default would put a credential in source.
    database_url: str
    redis_url: str


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, building them on first use."""
    return Settings()
```

- [ ] **Step 10: Write `tests/conftest.py`**

```python
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
```

- [ ] **Step 11: Run the tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS, 4 passed.

- [ ] **Step 12: Write `app/logging_config.py`**

```python
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
```

- [ ] **Step 13: Add uv to the Stack section of `CLAUDE.md`**

`CLAUDE.md`'s Stack section does not mention a dependency manager. Add one line so the
repo's own instructions match how it is actually built. Insert immediately after the
`- pytest + pytest-asyncio, ruff` line:

```markdown
- uv for dependencies and virtualenvs (`pyproject.toml` + committed `uv.lock`; the uv version is pinned in the Dockerfile tag)
```

Change nothing else in `CLAUDE.md`. The Commands section still works as written: the
Docker image puts the virtualenv on `PATH`, so `docker compose exec api pytest` runs
unchanged.

- [ ] **Step 14: Lint and format**

Run:
```bash
uv run ruff format .
uv run ruff check .
```
Expected: format reports files reformatted or unchanged; `check` reports `All checks passed!`.

- [ ] **Step 15: Commit**

```bash
git status --short          # confirm .env is NOT listed
git add pyproject.toml uv.lock .python-version CLAUDE.md app tests
git commit -m "feat(VS-001): project scaffolding, uv tooling and env-driven settings

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: App factory and the liveness endpoint

**Files:**
- Create: `app/main.py`
- Create: `app/api/__init__.py`
- Create: `app/api/health.py`
- Modify: `tests/conftest.py` (add the `app` and `client` fixtures)
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: `app.logging_config.configure_logging()` from Task 1.
- Produces:
  - `app.main.create_app() -> FastAPI` — the app factory.
  - `app.main.app` — a module-level instance, the uvicorn target `app.main:app`.
  - `app.api.health.router: APIRouter` — carries `GET /health`.
  - `app.api.health.HealthResponse` — pydantic model, field `status: Literal["ok"]`.

- [ ] **Step 1: Add the app fixtures to `tests/conftest.py`**

Append below the existing `os.environ` block:

```python
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
def app():
    """A fresh app per test, so dependency_overrides never leak between tests."""
    return create_app()


@pytest.fixture
async def client(app):
    """An in-process HTTP client. ASGITransport calls the app directly, so no
    port is bound and the lifespan does not run — tests need neither."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
```

- [ ] **Step 2: Write the failing liveness test**

Create `tests/test_health.py`:

```python
async def test_health_returns_ok(client):
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.main'`.

- [ ] **Step 4: Create `app/api/__init__.py`**

```bash
mkdir -p app/api
touch app/api/__init__.py
```

- [ ] **Step 5: Write `app/api/health.py`**

```python
"""Health endpoints.

/health is liveness: is this process alive? It does no I/O on purpose, so a
database or Redis outage never makes an orchestrator kill a healthy API.
/health/ready (Task 3) is readiness: can this process actually serve traffic?
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok"]


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness. Touches nothing external."""
    return HealthResponse(status="ok")
```

- [ ] **Step 6: Write `app/main.py`**

```python
"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.logging_config import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    yield


def create_app() -> FastAPI:
    """Build the app. A factory, not a module-level app, so each test gets a
    clean instance and dependency overrides cannot leak between tests."""
    app = FastAPI(title="Doctoleb WhatsApp Agent", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    return app


# uvicorn target: `uvicorn app.main:app`
app = create_app()
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `uv run pytest -v`
Expected: PASS, 5 passed.

- [ ] **Step 8: Verify the app actually serves**

Run `uv run uvicorn app.main:app --port 8000` in one terminal, then in another:
```bash
curl -i http://localhost:8000/health
```
Expected: `HTTP/1.1 200 OK` and body `{"status":"ok"}`. Stop the server.

- [ ] **Step 9: Lint, format and commit**

```bash
uv run ruff format . && uv run ruff check .
git add app tests
git commit -m "feat(VS-001): FastAPI app factory and GET /health

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Dependency probes and the readiness endpoint

Adds the async database engine and Redis client — only as far as a readiness probe needs them — and the `/health/ready` endpoint that reports on both. VS-002 builds models and sessions on this same engine.

**Files:**
- Create: `app/db/__init__.py`
- Create: `app/db/session.py`
- Create: `app/queue/__init__.py`
- Create: `app/queue/redis.py`
- Modify: `app/api/health.py` (add probes and `/health/ready`)
- Modify: `app/main.py` (dispose the engine and client on shutdown)
- Test: `tests/test_readiness.py`
- Test: `tests/test_health.py` (add the liveness-isolation test)

**Interfaces:**
- Consumes: `app.config.get_settings()`, `app.api.health.router`, `app.main.create_app()`.
- Produces:
  - `app.db.session.get_engine() -> AsyncEngine` — lazily built, one per process.
  - `app.db.session.ping_database() -> None` — async, raises if Postgres is unreachable.
  - `app.db.session.dispose_engine() -> None` — async, closes the pool.
  - `app.queue.redis.get_redis() -> Redis` — lazily built, one per process.
  - `app.queue.redis.ping_redis() -> None` — async, raises if Redis is unreachable.
  - `app.queue.redis.close_redis() -> None` — async, closes the client.
  - `app.api.health.PROBE_TIMEOUT_SECONDS: float` — `2.0`, the bound on every probe.
  - `app.api.health.database_status() -> Literal["ok", "error"]` — FastAPI dependency, never raises, never runs longer than `PROBE_TIMEOUT_SECONDS`.
  - `app.api.health.redis_status() -> Literal["ok", "error"]` — same contract.
  - `app.api.health.ReadinessResponse` — fields `status: Literal["ok", "degraded"]`, `database`, `redis`.

- [ ] **Step 1: Write the failing readiness tests**

Create `tests/test_readiness.py`:

```python
import asyncio
import logging
import time

import pytest

from app.api.health import PROBE_TIMEOUT_SECONDS, database_status, redis_status


async def test_ready_returns_200_when_both_dependencies_are_up(app, client):
    app.dependency_overrides[database_status] = lambda: "ok"
    app.dependency_overrides[redis_status] = lambda: "ok"

    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


async def test_ready_returns_503_when_the_database_is_down(app, client):
    app.dependency_overrides[database_status] = lambda: "error"
    app.dependency_overrides[redis_status] = lambda: "ok"

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "error", "redis": "ok"}


async def test_ready_returns_503_when_redis_is_down(app, client):
    app.dependency_overrides[database_status] = lambda: "ok"
    app.dependency_overrides[redis_status] = lambda: "error"

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "ok", "redis": "error"}


async def test_database_status_reports_error_when_the_probe_raises(monkeypatch):
    async def unreachable() -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("app.api.health.ping_database", unreachable)

    assert await database_status() == "error"


async def test_redis_status_reports_error_when_the_probe_raises(monkeypatch):
    async def unreachable() -> None:
        raise OSError("connection refused")

    monkeypatch.setattr("app.api.health.ping_redis", unreachable)

    assert await redis_status() == "error"


@pytest.mark.parametrize("failure", [ValueError("odd"), RuntimeError("odder")])
async def test_ready_answers_503_for_unexpected_probe_failures(app, client, monkeypatch, failure):
    """Review Focus 4. A readiness endpoint that raises a 500 tells you nothing."""

    async def unreachable() -> None:
        raise failure

    monkeypatch.setattr("app.api.health.ping_database", unreachable)
    app.dependency_overrides[redis_status] = lambda: "ok"

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["database"] == "error"


async def test_probe_failure_does_not_log_the_connection_string(monkeypatch, caplog):
    """Review Focus 2, hard rule 9. Connection errors carry the DSN, password included."""
    secret = "sup3rsecret"

    async def unreachable() -> None:
        raise OSError(f"could not connect to postgresql://doctoleb:{secret}@postgres:5432/db")

    monkeypatch.setattr("app.api.health.ping_database", unreachable)

    with caplog.at_level(logging.WARNING):
        assert await database_status() == "error"

    assert secret not in caplog.text
    assert "database" in caplog.text


async def test_ready_answers_503_when_a_probe_hangs(app, client, monkeypatch, caplog):
    """Review Focus 6.

    A dependency that accepts the connection but never answers must not hold the
    request open. The probe is bounded, so /health/ready answers in about
    PROBE_TIMEOUT_SECONDS rather than waiting out the ten-second sleep.
    """

    async def hangs() -> None:
        await asyncio.sleep(10)

    monkeypatch.setattr("app.api.health.ping_database", hangs)
    app.dependency_overrides[redis_status] = lambda: "ok"

    with caplog.at_level(logging.WARNING):
        started = time.monotonic()
        response = await client.get("/health/ready")
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "error", "redis": "ok"}
    # Generous upper bound: the point is "bounded", not "exactly 2s on a loaded CI box".
    assert elapsed < PROBE_TIMEOUT_SECONDS + 3
    assert "timed out" in caplog.text
    assert "database" in caplog.text
```

- [ ] **Step 2: Add the liveness-isolation test to `tests/test_health.py`**

Append:

```python
async def test_health_does_not_touch_dependencies(client, monkeypatch):
    """Review Focus 3. Liveness must stay 200 while Postgres and Redis are down."""

    async def must_not_be_called() -> None:
        raise AssertionError("liveness must not touch external dependencies")

    monkeypatch.setattr("app.api.health.ping_database", must_not_be_called)
    monkeypatch.setattr("app.api.health.ping_redis", must_not_be_called)

    response = await client.get("/health")

    assert response.status_code == 200
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_readiness.py tests/test_health.py -v`
Expected: FAIL — `ImportError: cannot import name 'PROBE_TIMEOUT_SECONDS' from 'app.api.health'`.

- [ ] **Step 4: Write `app/db/session.py`**

Create the package marker first:
```bash
mkdir -p app/db
touch app/db/__init__.py
```

Then `app/db/session.py`:

```python
"""Async SQLAlchemy engine.

VS-001 needs this only so readiness can prove Postgres is reachable.
VS-002 adds models, a session factory and repositories on the same engine.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide engine, creating it on first use.

    Built lazily rather than at import time, so importing app.db.session never
    requires a configured environment.
    """
    global _engine
    if _engine is None:
        # pool_pre_ping discards connections the database closed while idle,
        # which is what makes readiness report the truth rather than a stale
        # pooled socket.
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


async def ping_database() -> None:
    """Raise if Postgres is not reachable. Returns None when it is."""
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


async def dispose_engine() -> None:
    """Close the connection pool. Called on app shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
```

- [ ] **Step 5: Write `app/queue/redis.py`**

Create the package marker first:
```bash
mkdir -p app/queue
touch app/queue/__init__.py
```

Then `app/queue/redis.py`:

```python
"""Redis client.

VS-001 needs this only so readiness can prove Redis is reachable. VS-004 adds
the enqueue interface in this package, which is why it lives under app/queue/
rather than somewhere generic.
"""

from redis.asyncio import Redis, from_url

from app.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    """Return the process-wide Redis client, creating it on first use."""
    global _redis
    if _redis is None:
        _redis = from_url(get_settings().redis_url)
    return _redis


async def ping_redis() -> None:
    """Raise if Redis is not reachable. Returns None when it is."""
    await get_redis().ping()


async def close_redis() -> None:
    """Close the client. Called on app shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
```

- [ ] **Step 6: Rewrite `app/api/health.py`**

```python
"""Health endpoints.

/health is liveness: is this process alive? It does no I/O on purpose, so a
database or Redis outage never makes an orchestrator kill a healthy API.
/health/ready is readiness: can this process actually serve traffic?
"""

import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from app.db.session import ping_database
from app.queue.redis import ping_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

DependencyStatus = Literal["ok", "error"]

# Hard rule 11 applied to readiness: every probe is bounded. A dependency that
# accepts the connection and then goes quiet is the case a plain `except` never
# catches — without this, the request hangs until the caller gives up and the
# endpoint reports nothing at all.
PROBE_TIMEOUT_SECONDS = 2.0


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: DependencyStatus
    redis: DependencyStatus


async def database_status() -> DependencyStatus:
    """Probe Postgres. A FastAPI dependency, so tests can override it.

    Bounded and total: it always returns within PROBE_TIMEOUT_SECONDS and never
    raises. A probe exists to report a verdict, never to propagate a 500
    (Review Focus 4) and never to block the request (Review Focus 6).
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await ping_database()
    except TimeoutError:
        # Split from the general case so the log says which failure this was:
        # "refused" and "accepted then went silent" need different fixes.
        logger.warning("readiness probe timed out for dependency=database")
        return "error"
    except Exception:
        # Hard rule 9: the dependency name only. Connection errors embed the
        # DSN, password included, so the exception text must not be logged.
        logger.warning("readiness probe failed for dependency=database")
        return "error"
    return "ok"


async def redis_status() -> DependencyStatus:
    """Probe Redis. Same contract as database_status()."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await ping_redis()
    except TimeoutError:
        logger.warning("readiness probe timed out for dependency=redis")
        return "error"
    except Exception:
        logger.warning("readiness probe failed for dependency=redis")
        return "error"
    return "ok"


@router.get("/health")
async def health() -> HealthResponse:
    """Liveness. Touches nothing external."""
    return HealthResponse(status="ok")


@router.get("/health/ready")
async def ready(
    response: Response,
    database: Annotated[DependencyStatus, Depends(database_status)],
    redis: Annotated[DependencyStatus, Depends(redis_status)],
) -> ReadinessResponse:
    """Readiness. 200 when every dependency answers, 503 when any does not.

    The body names the failing dependency either way, so the response alone is
    enough to debug from without reading logs.
    """
    healthy = database == "ok" and redis == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(
        status="ok" if healthy else "degraded",
        database=database,
        redis=redis,
    )
```

- [ ] **Step 7: Close the connections on shutdown in `app/main.py`**

Replace the imports and the `lifespan` function:

```python
from app.api.health import router as health_router
from app.db.session import dispose_engine
from app.logging_config import configure_logging
from app.queue.redis import close_redis


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    yield
    await dispose_engine()
    await close_redis()
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS, 15 passed. The whole run takes a few seconds — the hang test costs
about 2s, not 10, which is the point of it.

- [ ] **Step 9: Lint, format and commit**

```bash
uv run ruff format . && uv run ruff check .
git add app tests
git commit -m "feat(VS-001): database and redis probes behind GET /health/ready

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Docker image, arq worker and Compose stack

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.yml`
- Create: `app/worker/__init__.py`
- Create: `app/worker/main.py`
- Modify: `.env.example` (add the local Postgres container variables)
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `app.config.get_settings()`, `app.logging_config.configure_logging()`.
- Produces:
  - `app.worker.main.ping(ctx: dict) -> str` — returns `"pong"`.
  - `app.worker.main.startup(ctx: dict) -> None`, `app.worker.main.shutdown(ctx: dict) -> None`.
  - `app.worker.main.WorkerSettings` — the arq entrypoint, `arq app.worker.main.WorkerSettings`.

- [ ] **Step 1: Write the failing worker test**

Create `tests/test_worker.py`:

```python
import os

from arq.connections import RedisSettings

from app.worker.main import WorkerSettings, ping


async def test_ping_job_returns_pong():
    assert await ping({}) == "pong"


def test_worker_registers_at_least_one_function():
    """arq refuses to start a worker with no functions and no cron jobs."""
    assert len(WorkerSettings.functions) >= 1


def test_worker_redis_settings_come_from_the_environment():
    """Hard rule 9: the worker reads REDIS_URL, it does not hardcode a host.

    Compared against whatever REDIS_URL actually holds, not a literal, so this
    passes on the host (redis://localhost:6379/0, from conftest) and inside the
    container (redis://redis:6379/0, from .env) without being two tests.
    """
    expected = RedisSettings.from_dsn(os.environ["REDIS_URL"])

    assert WorkerSettings.redis_settings.host == expected.host
    assert WorkerSettings.redis_settings.port == expected.port
    assert WorkerSettings.redis_settings.database == expected.database
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.worker'`.

- [ ] **Step 3: Write `app/worker/main.py`**

Create the package marker first:
```bash
mkdir -p app/worker
touch app/worker/__init__.py
```

Then `app/worker/main.py`:

```python
"""arq worker entrypoint: `arq app.worker.main.WorkerSettings`.

Run as its own process, separate from the api, so slow work never happens
inside a webhook request (hard rule 1). VS-004 registers the real jobs here.

VS-001 asks for an "empty" worker, but arq refuses to start with no functions
and no cron jobs registered, so a single trivial `ping` job stands in. It also
gives us a way to prove the worker really consumes from Redis.
"""

import logging
from typing import Any

from arq.connections import RedisSettings

from app.config import get_settings
from app.logging_config import configure_logging

logger = logging.getLogger(__name__)


async def ping(ctx: dict[str, Any]) -> str:
    """A no-op job, used to verify the worker is consuming from Redis."""
    return "pong"


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    logger.info("worker started")


async def shutdown(ctx: dict[str, Any]) -> None:
    logger.info("worker stopped")


class WorkerSettings:
    """arq reads these as plain class attributes."""

    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    functions = [ping]
    on_startup = startup
    on_shutdown = shutdown
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -v`
Expected: PASS, 18 passed.

- [ ] **Step 5: Write `.dockerignore`**

```
.git
.venv
venv
__pycache__
*.py[cod]
.pytest_cache
.ruff_cache
.mypy_cache
*.egg-info
.env
.env.*
!.env.example
docs
media
recordings
.vscode
.idea
```

- [ ] **Step 6: Write the `Dockerfile`, pinning uv to 0.12.19**

```dockerfile
# One image, two processes: the api runs uvicorn, the worker runs arq.
# Same code, same dependencies, different command.
FROM python:3.12-slim

# Pinned, not :latest — the uv that builds this image must be the same version as
# the developer's. Keep this tag in sync with the local uv (`uv --version`).
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/srv/.venv \
    PATH="/srv/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Dependencies first, in their own layer: Docker reuses it on every build where
# pyproject.toml and uv.lock are unchanged, which is almost every build.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project

# Then the source, which changes constantly.
COPY app ./app
COPY tests ./tests
RUN uv sync --frozen

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`PATH` puts the virtualenv first, so `pytest`, `uvicorn`, `arq` and later `alembic` work as bare commands — which is what `CLAUDE.md` documents (`docker compose exec api pytest`).

- [ ] **Step 7: Add the Postgres container variables to `.env.example` and `.env`**

Insert immediately after the existing `# Database / Redis` block in `.env.example`:

```
# Local Postgres container (docker-compose only; the app itself reads DATABASE_URL)
POSTGRES_USER=doctoleb
POSTGRES_PASSWORD=doctoleb
POSTGRES_DB=doctoleb
```

These configure the `postgres` service, not the app. Keeping them in `.env` rather than in `docker-compose.yml` honours hard rule 9: no credential literal is committed.

Then add the same three lines to the local `.env` created in Task 1, Step 5 — it was
copied before these keys existed:

```bash
printf '\n# Local Postgres container\nPOSTGRES_USER=doctoleb\nPOSTGRES_PASSWORD=doctoleb\nPOSTGRES_DB=doctoleb\n' >> .env
grep POSTGRES .env
```

Expected: the three keys are present in `.env`. Compose reads `.env` from the project
root for `${...}` substitution, so without them the defaults in `docker-compose.yml`
silently take over — fine here, but only because they happen to match.

- [ ] **Step 8: Write `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-doctoleb}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-doctoleb}
      POSTGRES_DB: ${POSTGRES_DB:-doctoleb}
    ports:
      # Bound to the loopback interface, not 0.0.0.0. A bare "5432:5432" would
      # expose a database with a known dev password to the whole local network.
      - "127.0.0.1:5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-doctoleb} -d ${POSTGRES_DB:-doctoleb}"]
      interval: 5s
      timeout: 3s
      retries: 10

  redis:
    image: redis:7-alpine
    ports:
      # Loopback only. Redis has no auth configured here.
      - "127.0.0.1:6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    env_file: .env
    ports:
      # Loopback only, like postgres and redis. Tunnels (ngrok, cloudflared) run
      # on the host and reach the api through localhost, and WhatsApp webhooks
      # arrive through the tunnel, so nothing needs LAN access to this port.
      - "127.0.0.1:8000:8000"
    volumes:
      # Bind-mount the source so --reload picks up edits without a rebuild.
      - ./app:/srv/app
      - ./tests:/srv/tests
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  worker:
    build: .
    command: arq app.worker.main.WorkerSettings
    env_file: .env
    volumes:
      - ./app:/srv/app
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

volumes:
  postgres_data:
```

`depends_on: condition: service_healthy` is what stops api and worker from booting against a Postgres that is still initialising.

- [ ] **Step 9: Bring the stack up**

```bash
docker compose up --build
```
Expected: four services start. `api` logs `Uvicorn running on http://0.0.0.0:8000`; `worker` logs `Starting worker for 1 functions: ping` followed by `worker started`.

`.env` already exists from Task 1, Step 5, extended in Step 7. Inside the containers, `DATABASE_URL` and `REDIS_URL` must use the service hostnames `postgres` and `redis` — which is exactly what `.env.example` ships. Leave them as they are.

- [ ] **Step 10: Verify the acceptance criteria against the running stack**

In a second terminal:
```bash
docker compose ps
curl -i http://localhost:8000/health
curl -i http://localhost:8000/health/ready
```
Expected: `docker compose ps` lists all four services as running; `/health` returns `200 {"status":"ok"}`; `/health/ready` returns `200 {"status":"ok","database":"ok","redis":"ok"}`.

- [ ] **Step 11: Verify readiness reports a real outage**

```bash
docker compose stop redis
curl -i http://localhost:8000/health
curl -i http://localhost:8000/health/ready
docker compose logs api --tail 20
docker compose start redis
```
Expected: `/health` stays `200` (Review Focus 3). `/health/ready` returns `503` with `{"status":"degraded","database":"ok","redis":"error"}` (Review Focus 4). The api logs show `readiness probe failed for dependency=redis` and contain no password (Review Focus 2). This is the proof against the real services rather than a mock.

- [ ] **Step 12: Verify the documented test command works in the container**

```bash
docker compose exec api pytest
```
Expected: all 18 tests pass inside the container. In particular
`test_worker_redis_settings_come_from_the_environment` passes here too, where
`REDIS_URL` is `redis://redis:6379/0` rather than the host's `localhost` — that is
exactly why it compares against the env value instead of a literal.

- [ ] **Step 13: Verify the uv pin actually took**

```bash
docker compose exec api uv --version
uv --version
```
Expected: the container's uv version is 0.12.19 and matches the host's exactly. If it
does not, the Dockerfile tag drifted from the local uv, or the image was not rebuilt.

- [ ] **Step 14: Tear down, lint and commit**

```bash
docker compose down
uv run ruff format . && uv run ruff check .
git status --short          # confirm .env is still NOT listed
git add Dockerfile .dockerignore docker-compose.yml .env.example app tests
git commit -m "feat(VS-001): docker image, arq worker and compose stack

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Documentation and slice close-out

**Files:**
- Modify: `README.md` (add a "Run it locally" section)
- Modify: `docs/slices/VS-001.md` (Status, Notes, Follow-ups)
- Modify: `docs/slices/README.md` (VS-001 → DONE)

**Interfaces:**
- Consumes: everything built in Tasks 1–4.
- Produces: nothing importable.

- [ ] **Step 1: Run the full verification suite**

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
docker compose up --build -d
curl -s http://localhost:8000/health/ready
docker compose down
```
Expected: pytest all green; `All checks passed!`; the format check reports nothing would be reformatted; readiness returns `{"status":"ok","database":"ok","redis":"ok"}`.

Do not proceed until every one of these passes. Record the actual output — a claim of "done" without it is not a close-out.

- [ ] **Step 2: Add a "Run it locally" section to `README.md`**

Insert after the existing "Getting started" section:

````markdown
## Run it locally
```bash
cp .env.example .env     # fill in values; the Postgres/Redis defaults work as-is
docker compose up --build
```
- API: http://localhost:8000
- Liveness: `GET /health` -> `200 {"status":"ok"}`
- Readiness: `GET /health/ready` -> `200` when Postgres and Redis answer, `503` otherwise

Tests and lint:
```bash
docker compose exec api pytest
docker compose exec api ruff check .
```

Working outside Docker needs [uv](https://docs.astral.sh/uv/): `uv sync`, then `uv run pytest`.
````

- [ ] **Step 3: Update `docs/slices/VS-001.md`**

Set `Status: DONE` and append:

```markdown
## Notes
- Dependency manager is uv (`pyproject.toml` + committed `uv.lock`). The Docker image
  installs with `uv sync --frozen` and puts `/srv/.venv/bin` on PATH, so `pytest`,
  `uvicorn`, `arq` and later `alembic` run as bare commands inside the container.
- `Settings` uses `extra="ignore"`. pydantic-settings defaults to `extra="forbid"`,
  which would reject `.env` outright because `.env.example` already lists the Meta,
  OpenAI and Booking keys that later slices need.
- `/health` does no I/O, so it stays 200 during a dependency outage. `/health/ready`
  probes both dependencies through FastAPI dependencies and returns 503 when either
  fails, naming the failing one in the body. Probe failures log the dependency name
  only — connection errors embed the DSN password (hard rule 9).
- Each probe runs under `asyncio.timeout(2)`. A dependency that accepts the connection
  and then goes silent is the case a plain `except` never catches; without the bound,
  `/health/ready` would hang instead of answering. Timeouts log a distinct message from
  refusals, because the two need different fixes.
- The Dockerfile pins uv with a hardcoded tag, `ghcr.io/astral-sh/uv:0.12.19`, matching
  the uv installed locally. The tag carries a comment to keep the two in sync, and
  `docker compose exec api uv --version` checks it.
- All three published ports bind `127.0.0.1` only. Postgres and Redis run with well-known
  dev credentials (Redis with none at all), so `0.0.0.0` would hand the local network a
  database. The api is loopback too: tunnels (ngrok, cloudflared) run on the host and
  reach it through localhost, and WhatsApp webhooks arrive through the tunnel.
- `DATABASE_URL` and `REDIS_URL` are required with no defaults, so a missing config
  stops the process instead of starting a half-working API.
- The worker registers one trivial `ping` job. arq refuses to start a worker with no
  functions and no cron jobs, so a literally empty worker was not possible.
- `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` were added to `.env.example`.
  They configure the local Postgres container only; the app reads `DATABASE_URL`.

## Follow-ups
- Container healthchecks for `api` and `worker` (VS-001 only added them for postgres
  and redis).
- A multi-stage Dockerfile and a non-root user before anything goes to production;
  the current image is a single stage, runs as root and ships the dev dependency group.
- Structured JSON logging with a request id, once there is a request worth tracing.
```

- [ ] **Step 4: Update `docs/slices/README.md`**

Change the VS-001 row's Status from `IN PROGRESS` to `DONE`. Leave VS-002 as `TODO` — the next slice is started deliberately, not automatically.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/slices/VS-001.md docs/slices/README.md
git commit -m "docs(VS-001): local run instructions and slice close-out

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Acceptance criteria mapped to tasks

| VS-001 requirement | Where it is built | Where it is proven |
|---|---|---|
| pyproject with deps, ruff, pytest config | Task 1, Step 1 | Task 1, Steps 11 and 14 |
| app factory | Task 2, Step 6 | Task 2, Step 7 |
| config via pydantic-settings | Task 1, Step 9 | Task 1, Step 11 |
| `.env.example` | pre-existing; extended in Task 4, Step 7 | Task 4, Step 9 |
| `GET /health` -> 200 `{"status":"ok"}` | Task 2, Step 5 | Task 2, Steps 7-8; Task 4, Step 10 |
| `GET /health/ready` checks DB + Redis | Task 3, Step 6 | Task 3, Step 8; Task 4, Steps 10-11 |
| Dockerfile | Task 4, Step 6 | Task 4, Steps 9 and 13 |
| docker-compose.yml, 4 services | Task 4, Step 8 | Task 4, Steps 9-10 |
| arq worker that starts | Task 4, Step 3 | Task 4, Steps 4 and 9 |
| one test for /health | Task 2, Step 2 | Task 2, Step 7 |
| `docker compose up --build` starts all 4 | Task 4, Step 8 | Task 4, Step 10 |
| `pytest` passes | every task | Task 5, Step 1 |
| uv pinned to one version | Task 4, Step 6 (hardcoded tag) | Task 4, Step 13 |
| probes bounded (hard rule 11) | Task 3, Step 6 | Task 3, Step 8 |
| uv recorded in `CLAUDE.md` Stack | Task 1, Step 13 | Task 1, Step 15 |
