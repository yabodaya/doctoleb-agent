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
COPY alembic.ini ./
COPY migrations ./migrations
# The EXAMPLE, never .env — it holds key names and no values, and .dockerignore
# keeps the real one out. Copied in because tests/test_config.py asserts that
# every setting the code reads is documented there, and that assertion has to
# hold in the container the acceptance run happens in.
COPY .env.example ./
RUN uv sync --frozen

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
