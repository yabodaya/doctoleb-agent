# VS-002 Messaging Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task, checkpointing with the developer after every task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** This repo's own messaging tables — webhook inbox, contacts and identities, conversations, messages, dead-letter jobs — with Alembic migrations and tenant-scoped repositories.

**Architecture:** SQLAlchemy 2.x declarative models on the async engine VS-001 already built, one Alembic migration chain with the DSN injected from `app.config` rather than `alembic.ini`, and a thin repository layer that takes `tenant_id` in its constructor so a query cannot be written without one. The database enforces the rules that matter (unique `provider_event_id`, unique `provider_message_id`, one identity per tenant, one open conversation per contact); the repositories are a convenience on top, not the safety net.

**Tech Stack:** Python 3.12, SQLAlchemy 2.1.1 (async) + asyncpg 0.31, Alembic, PostgreSQL 16, pytest + pytest-asyncio, ruff, Docker Compose.

**Spec:** `docs/slices/VS-002.md` (scope and acceptance), with `CLAUDE.md` (hard rules), `docs/architecture.md` (ownership and flows) and `docs/booking-contract.md` (open questions) as binding context. `docs/slices/VS-001.md` Notes carry the environment facts this slice builds on.

**Sequencing constraint:** Task 1 may run now. **Task 2 must not start until assumption A3 (the type of `tenant_id`) is confirmed** — it is schema, and getting it wrong is a type change across five tables.

---

## Global Constraints

- Python 3.12 only. Dependency manager is **uv**; `pyproject.toml` + committed `uv.lock`, Docker installs with `uv sync --frozen`. The uv pin in the Dockerfile (`ghcr.io/astral-sh/uv:0.12.19`) does not change in this slice.
- **`alembic>=1.14` goes in `[project].dependencies`, not the dev group.** Migrations run in the deployed api container (`docker compose exec api alembic upgrade head` is a documented command in `CLAUDE.md`), so alembic is runtime, not tooling.
- **The image and the compose mounts must carry the migrations.** The VS-001 Dockerfile copies only `app/` and `tests/`, and the api service bind-mounts only those. Alembic needs `alembic.ini` and `migrations/` inside the container, and needs `migrations/versions/` bind-mounted so `--autogenerate` writes to the host. Task 3 does both, and rebuilds.
- **No DSN, password or tenant literal in committed source or in `alembic.ini`** (hard rule 9). `alembic.ini` ships with `sqlalchemy.url =` empty; `migrations/env.py` fills it from `app.config.get_settings().database_url`, or from the value a caller passed via `Config.set_main_option`.
- **Patient content never reaches logs, error trackers or fixtures built from real data** (hard rule 8). Concretely in this slice: no model defines a `__repr__` that includes `text`, `display_name` or `external_id`; repositories never re-raise a raw `IntegrityError` where the conflicting values are patient content; every fixture uses synthetic identifiers.
- **`tenant_id` is never an argument a caller can forget.** Tenant-scoped repositories take it in `__init__` and filter every statement by it (hard rule 4). It is never derived from anything the LLM produced — this slice has no LLM, but the shape is set here.
- **Every timestamp column is `TIMESTAMPTZ`** (`sa.DateTime(timezone=True)`) with `server_default=sa.func.now()`. Never a naive `TIMESTAMP`.
- **Every constraint and index is named by convention**, via a `naming_convention` on the shared `MetaData`. Unnamed CHECK constraints get random PostgreSQL names, which makes Alembic autogenerate produce spurious diffs forever and makes it impossible to report a violated constraint by name.
- **Models and migrations must not drift** for everything autogenerate can see. `alembic.autogenerate.compare_metadata` against `head` must return an empty diff, and Task 3 tests it. CHECK constraints are outside what it compares — see Review Focus 5 and the runtime test in Task 4.
- Linting: `ruff check .` clean, `ruff format .` leaves no diff. Line length 100, target `py312`.
- **`pytest` must still pass with no Postgres and no Redis running** (VS-001 global constraint, unchanged). Tests that need a real database are marked `@pytest.mark.db` and skip with an explicit reason when the database is unreachable. See "Running the tests" below.
- Stay inside VS-002 scope. Out of scope: clinic/doctor/service/schedule/appointment tables (Booking Service owns them), the webhook endpoint (VS-003), enqueue and jobs (VS-004), OpenAI (VS-005), the Meta client (VS-004), object storage for audio (VS-008), staff/dashboard tables (VS-010). Anything that looks needed but is out of scope goes under "Follow-ups" in `docs/slices/VS-002.md`, not into the code.
- Every commit message ends with the trailer:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

---

## Running the tests: host vs container

VS-001 established that `pytest` passes with nothing running. VS-002 is the first slice with tests that genuinely need PostgreSQL — a unique constraint cannot be proven against a mock. Both facts have to stay true at once.

**The split**

| Test group | Needs Postgres | Lives in | Marker |
|---|---|---|---|
| Enums, declarative base, mixins, `__repr__` | no | `tests/db/test_base.py` | none |
| Model metadata (table names, columns, constraints declared) | no | `tests/db/test_models.py` | none |
| Repository construction contract (`tenant_id` required) | no | `tests/db/test_repository_contract.py` | none |
| Migration lifecycle (upgrade / downgrade / drift) | **yes** | `tests/db/test_migrations.py` | `@pytest.mark.db` |
| Constraint behaviour (duplicates, CHECKs, FKs, partial index) | **yes** | `tests/db/test_constraints.py` | `@pytest.mark.db` |
| Repository behaviour | **yes** | `tests/db/test_repositories.py` | `@pytest.mark.db` |
| Everything from VS-001 | no | `tests/test_*.py` | none |

Model metadata tests need no database because SQLAlchemy builds the full `Table` objects at import time. Asserting that `messages.provider_message_id` carries a unique constraint is a check on `Base.metadata`, not on a server. The repository construction contract needs none either: `TenantScopedRepository.__init__` raises before the session is ever touched, so the test passes `None` as the session. That is why the slice's second acceptance criterion — *repository queries require tenant_id* — is provable with nothing running.

**Which database**

Tests never touch the development database. They use a second database on the same server, named `doctoleb_test`, addressed by `TEST_DATABASE_URL`. `tests/db/conftest.py` derives a default by swapping the database name on `DATABASE_URL`, so nobody has to set it by hand, and `.env.example` documents the override.

`tests/db/conftest.py` owns a session-scoped, **synchronous** fixture that:

1. connects to the `postgres` maintenance database with raw asyncpg and issues `CREATE DATABASE doctoleb_test` if it is missing;
2. runs `alembic upgrade head` against it once, through Alembic's Python API;
3. on any connection failure, calls `pytest.skip(...)`, so every `@pytest.mark.db` test reports `SKIPPED` with the reason `no PostgreSQL at <host>:<port>` and the rest of the suite is untouched.

The fixture is synchronous on purpose. Alembic's async template calls `asyncio.run()` inside `env.py`; invoking it from inside a pytest-asyncio coroutine raises `RuntimeError: asyncio.run() cannot be called from a running event loop`. A sync fixture runs outside the loop and sidesteps the problem entirely. Task 3, Step 8 names this trap again where the code is written.

Per-test isolation is an outer transaction that is always rolled back: the `db_session` fixture opens a connection, begins a transaction, binds an `AsyncSession` to that connection with `join_transaction_mode="create_savepoint"` (so a test may call `session.commit()` without ending the outer transaction), and rolls back in `finally`. The schema is migrated once per session; no test sees another test's rows.

One consequence to keep in mind while writing tests: PostgreSQL's `now()` is the **transaction** start time, constant for the whole test. Any assertion of the form "this timestamp column moved" is vacuous inside one transaction unless the column is first set to a fixed past value. Tasks 4 and 5 do exactly that where it matters.

**Two ways to run, both supported**

```bash
# 1. Host. Requires the compose stack up — VS-001 publishes Postgres on 127.0.0.1:5432.
docker compose up -d postgres
uv run pytest                      # all 68 tests run

# 2. Container. This is the full run the acceptance criteria are judged on.
docker compose up -d --build
docker compose exec api pytest     # all 68 tests run
```

Inside the container `DATABASE_URL` already points at `postgres:5432` (compose `env_file: .env`), so the derived `TEST_DATABASE_URL` resolves correctly with no extra configuration. On the host, `tests/conftest.py` already `setdefault`s `DATABASE_URL` to `localhost:5432`, which is exactly the loopback port VS-001 publishes.

**With nothing running**

```bash
uv run pytest                      # 37 passed, 31 skipped
```

The 37 are VS-001's 18 plus this slice's 19 no-database tests. The 31 database tests skip with a printed reason. `ruff check .` is unaffected. A slice is only *done* when the container run is green — a skipped test is not a passed one, and Task 6, Step 1 runs it.

**Rejected alternative:** SQLite in memory. It has no `JSONB`, no partial unique indexes, no `TIMESTAMPTZ`, and different unique-violation semantics. Passing tests on SQLite would prove nothing about the constraints this slice exists to create.

---

## Assumptions that depend on open questions in `docs/booking-contract.md`

These are listed, not decided. Each one is cheap to hold now and has a named cost if the answer comes back differently.

**A1. Open question 2 — "How is a patient identified across services?"**
*Assumption:* nothing in this slice answers it. `contacts` gets **no** `booking_patient_ref` column. The Booking Service contract's `patient_ref` is produced at call time in VS-007; until the answer is known, the only patient key we store is our own `contacts.id` plus the WhatsApp identity in `contact_identities`.
*If the answer is "a patient id you create":* VS-007 adds a nullable `contacts.booking_patient_ref` column in its own migration. Additive, cheap.
*If the answer is "the phone number":* nothing changes; `contact_identities.external_id` already holds it.
*Why not decide now:* a column holding the wrong kind of key is worse than no column, because VS-007 would write to it before anyone notices.

**A2. Open question 1 — "Who owns the tenant ↔ WhatsApp number mapping?"**
*Assumption:* this slice creates **no `tenants` table and no `whatsapp_numbers` table**. `tenant_id` is a bare `UUID` column with no foreign key. VS-004 resolves it from a config mapping (`DEV_TENANT_ID` already exists in `.env.example`), per that slice's own scope line.
*If the answer is "the Booking Service owns it":* we keep `tenant_id` opaque and call `GET /tenants/by-whatsapp/{phone_number_id}`; no schema change.
*If the answer is "we own it":* a later slice adds `tenants` and `whatsapp_numbers` and, optionally, a foreign key from each `tenant_id`. Adding an FK to an existing column is a one-line migration.

**A3. `tenant_id` is a UUID. — BLOCKS TASK 2.**
This follows from A2, not from any document. `X-Tenant-Id` in the booking contract is untyped, and `DEV_TENANT_ID` in `.env.example` is empty.
*If tenant identity turns out to be a slug or an integer:* every `tenant_id` column changes type, across five tables. Task 1 does not depend on it; Task 2 does.

**A4. Open question 5 — "Timezone handling: assume Asia/Beirut, ISO 8601 with offset?"**
*Assumption:* every timestamp we store is `TIMESTAMPTZ` and every value written is UTC-aware. We store no clinic-local wall-clock times at all in this slice, so Asia/Beirut never appears in the schema. Presentation timezone is the Agent Core's problem in VS-007.
*This assumption is safe whichever way the question resolves*, which is why it is a global constraint rather than a per-column choice.

**A5. Open question 6 — "How does the dashboard read our conversations: our API, or a shared DB?"**
*Assumption:* our API. The schema is therefore designed for our own access patterns only — no views, no dashboard-shaped denormalisation, no read-only role.
*Note:* `docs/architecture.md` already records that this repo owns contacts, conversations, messages, webhook inbox and handoff state, and that the dashboard "reads our conversation data via API; TBD". **Ownership is settled and this plan does not reopen it.** Only the access mechanism is open, and it has no effect on this slice's tables.
*If the answer is "shared DB":* a later slice adds a restricted role and grants. Additive.

**A6. Open question 3 — "Does booking need staff approval (pending state)?"**
*No effect on this slice.* Appointment state lives in the Booking Service. Listed so you can see it was checked rather than missed.

**A7. Open question 4 — "Hold duration?"**
*No effect on this slice.* Holds live in the Booking Service and are never persisted here. Same reason as A6.

**Confirmed decision (agreed):** conversation state, message direction, modality and status are stored as `VARCHAR` with a named `CHECK` constraint, driven by Python `StrEnum`, **not** as native PostgreSQL `ENUM` types. `modality` gains `VOICE_NOTE` in VS-008 and message `status` grows as Meta status callbacks are handled in VS-004. `ALTER TYPE ... ADD VALUE` has no reverse operation, which would make the "downgrade works" acceptance criterion unmeetable. Swapping a CHECK constraint is a two-line, fully reversible migration. Cost: autogenerate cannot see CHECK constraints at all (Review Focus 5), which Task 4 compensates for with a runtime test.

---

## VS-001 follow-ups: what is pulled in, and what is not

**Pulled in: none** — confirmed with the developer, including no Task 0. Every VS-001 follow-up was read and considered; the verdicts are listed so nothing is silently dropped or silently smuggled in.

| VS-001 follow-up | Verdict |
|---|---|
| Container healthchecks for `api` and `worker` | Not in scope. Compose/runtime concern, no relation to the schema. |
| Multi-stage Dockerfile, non-root user, drop dev deps | Not in scope. Pre-production hardening. Task 3 does edit the Dockerfile, but only to add `alembic.ini` and `migrations/`. |
| Structured JSON logging with a request id | Not in scope — and premature: the first request worth tracing arrives in VS-003. |
| Readiness worst case ≈ 2× `PROBE_TIMEOUT_SECONDS` (sequential probes) | Not in scope. Untouched by this slice. |
| Restrict the VS-003 tunnel / disable `/docs` | Belongs to VS-003, which owns the tunnel. |
| `restart: unless-stopped` for api and worker | Not in scope. |
| Orphaned-future `ERROR asyncio Future exception was never retrieved` | Not in scope, and not a demonstrated hard-rule-9 leak: the one observed occurrence carried a `socket.gaierror` holding a hostname only, not a DSN or a password. It remains a logging-hygiene follow-up on VS-001's probe path — an unbounded claim about what *other* exception types might carry, not a proven exposure. This slice does not exercise it either: the test skip path is a plain `asyncpg.connect` with no readiness probe and no `asyncio.timeout`, so no future is ever cancelled. Stays in VS-001. |
| No-credential log test covers only the database probe; add the Redis one | Not in scope. VS-001's probe path, not the schema. |
| `docker-compose.yml` `${POSTGRES_*:-doctoleb}` credential defaults | Not in scope. Deployment hardening, and this slice keeps using those defaults locally. |

**New follow-ups this slice will generate** (written into `docs/slices/VS-002.md` at Task 6, not acted on):

- The test database is created but never dropped; a stale `doctoleb_test` survives a schema rewrite and can mask a broken migration. Add a documented recreate flag or a teardown.
- `webhook_inbox.payload` **and `dead_letter_jobs.payload`** retain raw Meta payloads, which contain patient text and phone numbers, indefinitely. A dead letter stores the job payload, which is the same event body, so a retention policy has to cover both tables. Belongs alongside VS-008's audio retention setting.
- No index yet on `messages.status` or `webhook_inbox.status`. Add them when VS-004 has a real query pattern, rather than guessing now.
- `ConversationRepository.get_or_create_open` does check-then-insert and lets the partial unique index raise on a lost race. VS-004's caller must treat that as retryable; a helper that retries once belongs with the job code, not here.

---

## Review Focus

Seven conditions the slice implies but does not spell out. Each has a test in the task that owns the code.

1. **A unique-constraint violation must not carry a patient's phone number into a log or an error tracker.** The unique key on `contact_identities` is `(tenant_id, channel, external_id)`, and `external_id` *is* the phone number. asyncpg puts the conflicting values into the exception message, so one `logger.exception(...)` or one Sentry capture writes patient data to a third party — hard rule 8. Repositories therefore upsert with `ON CONFLICT` instead of catching violations, and where a violation must surface it becomes a `DuplicateRecordError` carrying the **constraint name only**. Test in Task 5.
2. **A model must never render patient content in its string form.** These objects end up in assertion failures, debugger output and tracebacks. A hand-written `__repr__` including `text` or `display_name` is the easiest way to break hard rule 8, and it looks helpful while doing it. Every model's `__repr__` prints class name, `id`, and `tenant_id` where present — nothing else. Test in Task 1, and per-model in Task 2.
3. **Two deliveries for the same patient must not create two conversations.** `docs/architecture.md` states Meta may deliver the same webhook more than once and out of order, and VS-004's jobs are at-least-once. Two workers processing two messages from the same patient concurrently both find no open conversation and both insert one; the `provider_event_id` constraint does not help, because these are genuinely different events. The guard is a partial unique index: one non-`CLOSED` conversation per `(tenant_id, contact_id, channel)`. Test in Task 4 with two concurrent sessions.
4. **`alembic downgrade base` must leave a database with nothing left behind.** The acceptance criterion says "downgrade works", which is usually read as "did not raise". A downgrade that leaves an index or a constraint behind makes the *next* `upgrade head` fail with a duplicate-object error, on a database that looks empty. Task 3 asserts the post-downgrade table list is exactly `{alembic_version}`.
5. **A model change that never reaches a migration must fail the build — and the drift test does not cover CHECK constraints.** `compare_metadata` detects added and removed tables, added and removed columns, type changes, nullability, indexes and unique constraints. It does **not** compare CHECK constraints at all: they are emitted into the initial `create_table` and never looked at again, so widening `MessageModality` in VS-008 without writing a migration would pass the drift test silently and then reject every voice note at runtime. Task 3 owns the drift test for everything autogenerate can see; Task 4 adds a runtime test that an invalid state is actually rejected, because that is the only way a CHECK gets proven.
6. **A tenant-scoped repository must return nothing for another tenant's row even when handed a correct primary key.** Hard rule 4 governs where `tenant_id` comes from; this governs what happens when a correct-looking id crosses a tenant boundary. A bug in VS-004's tenant resolution must produce *no data*, not another clinic's patient. Task 5 tests it with two tenants whose contacts share one phone number.
7. **`tenant_id` must be nullable exactly where it is genuinely unknown.** The webhook endpoint stores the raw event before anything resolves a tenant (hard rule 1: verify → dedupe → store → enqueue → 200). A `NOT NULL tenant_id` on `webhook_inbox` would force tenant resolution inside the request and break that rule. `webhook_inbox.tenant_id` and `dead_letter_jobs.tenant_id` are nullable; every other `tenant_id` is `NOT NULL`. Test in Task 4.

---

### Task 1: Alembic dependency, declarative base, enums and the session factory

Everything the models will sit on: the shared `MetaData` with a naming convention, the UUID primary key and timestamp mixins, the four `StrEnum`s, the content-safe `__repr__`, and an `async_sessionmaker` on VS-001's existing engine. No tables yet, so no database is needed. **Safe to run before A3 is answered.**

**Files:**
- Modify: `pyproject.toml` (add `alembic`, register the `db` marker)
- Create: `app/db/base.py`
- Create: `app/db/enums.py`
- Modify: `app/db/session.py` (add the session factory and a FastAPI dependency)
- Create: `tests/db/__init__.py`
- Test: `tests/db/test_base.py`

**Interfaces:**
- Consumes: `app.config.get_settings()`, `app.db.session.get_engine()` (both from VS-001).
- Produces:
  - `app.db.base.NAMING_CONVENTION` — the shared constraint naming map.
  - `app.db.base.Base` — `DeclarativeBase` subclass carrying `metadata` with that convention, and a content-safe `__repr__`.
  - `app.db.base.UUIDPrimaryKeyMixin` — `id: Mapped[UUID]`, primary key, **assigned at construction**.
  - `app.db.base.TimestampMixin` — `created_at` / `updated_at`, both `TIMESTAMPTZ NOT NULL server_default=now()`.
  - `app.db.enums.ConversationState` — `AI_ACTIVE`, `HUMAN_REQUESTED`, `HUMAN_ACTIVE`, `CLOSED`.
  - `app.db.enums.MessageDirection` — `INBOUND`, `OUTBOUND`.
  - `app.db.enums.MessageModality` — `TEXT`, `VOICE_NOTE`.
  - `app.db.enums.MessageStatus` — `RECEIVED`, `QUEUED`, `SENT`, `DELIVERED`, `READ`, `FAILED`.
  - `app.db.enums.InboxStatus` — `RECEIVED`, `PROCESSING`, `PROCESSED`, `FAILED`.
  - `app.db.enums.Channel` — `WHATSAPP`.
  - `app.db.enums.check_constraint(column, enum_cls, name) -> sa.CheckConstraint`.
  - `app.db.session.get_sessionmaker() -> async_sessionmaker[AsyncSession]`.
  - `app.db.session.get_session() -> AsyncIterator[AsyncSession]` — FastAPI dependency.

**Expected tests after this task: 25 (18 from VS-001 + 7 new). All pass with nothing running.**

- [ ] **Step 1: Add alembic and register the `db` marker in `pyproject.toml`**

In `[project].dependencies`, after `"arq>=0.26",`:

```toml
    "alembic>=1.14",
```

In `[tool.pytest.ini_options]`, after `asyncio_default_fixture_loop_scope = "function"`:

```toml
markers = [
    "db: needs a real PostgreSQL; skipped when TEST_DATABASE_URL is unreachable",
]
```

Then:

```bash
uv sync
uv run alembic --version
```

Expected: a version line, not `program not found`. This also rewrites `uv.lock`, which is why Task 3 must rebuild the image — the current one predates alembic entirely.

- [ ] **Step 2: Write the failing base/enum tests**

Create `tests/db/__init__.py` (empty file) and `tests/db/test_base.py`:

```python
"""The foundation every model sits on.

No database: SQLAlchemy builds Table objects at import time, so metadata is
fully inspectable with nothing running.
"""

import datetime as dt
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.base import NAMING_CONVENTION, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import (
    Channel,
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
    check_constraint,
)


class _SampleBase(DeclarativeBase):
    """A separate registry, so the throwaway model below never lands on
    Base.metadata — tests/db/test_models.py asserts the exact set of tables the
    slice creates, and a leaked `_sample` would fail it whenever both modules
    are imported into the same process."""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    __repr__ = Base.__repr__


class _Sample(UUIDPrimaryKeyMixin, TimestampMixin, _SampleBase):
    """A throwaway model used only to exercise the mixins."""

    __tablename__ = "_sample"

    tenant_id: Mapped[UUID] = mapped_column(sa.Uuid, nullable=False)
    secret_text: Mapped[str | None] = mapped_column(sa.Text)


def test_enum_values_are_the_exact_strings_stored_in_the_database():
    # These strings end up in CHECK constraints and in rows. Renaming one is a
    # migration, not a refactor, so they are pinned here.
    assert [s.value for s in ConversationState] == [
        "AI_ACTIVE",
        "HUMAN_REQUESTED",
        "HUMAN_ACTIVE",
        "CLOSED",
    ]
    assert [d.value for d in MessageDirection] == ["INBOUND", "OUTBOUND"]
    assert [m.value for m in MessageModality] == ["TEXT", "VOICE_NOTE"]
    assert [s.value for s in MessageStatus] == [
        "RECEIVED",
        "QUEUED",
        "SENT",
        "DELIVERED",
        "READ",
        "FAILED",
    ]
    assert [s.value for s in InboxStatus] == [
        "RECEIVED",
        "PROCESSING",
        "PROCESSED",
        "FAILED",
    ]
    assert [c.value for c in Channel] == ["whatsapp"]


def test_enums_are_plain_strings_so_they_bind_to_varchar_columns():
    assert ConversationState.AI_ACTIVE == "AI_ACTIVE"
    assert f"{MessageDirection.INBOUND}" == "INBOUND"


def test_check_constraint_lists_every_enum_value():
    constraint = check_constraint("state", ConversationState, "state_valid")
    rendered = str(constraint.sqltext)
    for state in ConversationState:
        assert state.value in rendered
    assert constraint.name == "state_valid"


def test_metadata_uses_a_deterministic_naming_convention():
    # Without this, PostgreSQL names CHECK constraints at random and every
    # autogenerate run produces a spurious diff.
    convention = Base.metadata.naming_convention
    assert convention["pk"] == "pk_%(table_name)s"
    assert convention["uq"] == "uq_%(table_name)s_%(column_0_N_name)s"
    assert convention["ck"] == "ck_%(table_name)s_%(constraint_name)s"
    assert convention["fk"] == "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    assert convention["ix"] == "ix_%(table_name)s_%(column_0_N_name)s"


def test_uuid_primary_key_is_assigned_at_construction():
    # mapped_column(default=...) is an INSERT-time default: with only that,
    # row.id is None until the row is flushed. Half this slice builds parent
    # and child rows in one go (a contact and its identity, a conversation and
    # its messages) and reads the parent's id before any flush, so the id has
    # to exist the moment the object does.
    row = _Sample(tenant_id=uuid4())
    assert isinstance(row.id, UUID)
    assert _Sample.__table__.c.id.primary_key is True

    # An explicitly supplied id must survive.
    fixed = uuid4()
    assert _Sample(id=fixed, tenant_id=uuid4()).id == fixed

    # Two rows do not share one.
    assert _Sample(tenant_id=uuid4()).id != _Sample(tenant_id=uuid4()).id


def test_timestamps_are_timezone_aware_with_a_server_default():
    # A naive TIMESTAMP column silently drops the offset. The 24h WhatsApp
    # free-form window is computed from these values; an hour of drift is a
    # message Meta rejects.
    for name in ("created_at", "updated_at"):
        column = _Sample.__table__.c[name]
        assert isinstance(column.type, sa.DateTime)
        assert column.type.timezone is True
        assert column.nullable is False
        assert column.server_default is not None
    assert dt.datetime.now(dt.UTC).tzinfo is not None  # sanity: we write aware values


def test_repr_never_includes_column_content():
    # Hard rule 8. These objects appear in tracebacks and assertion failures.
    row = _Sample(tenant_id=uuid4(), secret_text="my knee hurts")
    rendered = repr(row)
    assert "my knee hurts" not in rendered
    assert "_Sample" in rendered
    assert str(row.id) in rendered
```

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/db/test_base.py -v
```

Expected: collection error, `ModuleNotFoundError: No module named 'app.db.base'`.

- [ ] **Step 4: Write `app/db/enums.py`**

```python
"""Vocabularies stored in the database.

These are VARCHAR + CHECK, not native PostgreSQL ENUM types. MessageModality
gains VOICE_NOTE in VS-008 and MessageStatus grows as VS-004 handles Meta status
callbacks; ALTER TYPE ... ADD VALUE has no reverse operation, which would make
`alembic downgrade` a lie. Swapping a CHECK constraint is fully reversible.

The .value strings are the literal contents of database rows. Renaming one is a
data migration, not a refactor.
"""

from enum import StrEnum

import sqlalchemy as sa


class Channel(StrEnum):
    WHATSAPP = "whatsapp"


class ConversationState(StrEnum):
    """docs/architecture.md: AI_ACTIVE -> HUMAN_REQUESTED -> HUMAN_ACTIVE -> CLOSED."""

    AI_ACTIVE = "AI_ACTIVE"
    HUMAN_REQUESTED = "HUMAN_REQUESTED"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    CLOSED = "CLOSED"


class MessageDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageModality(StrEnum):
    TEXT = "TEXT"
    VOICE_NOTE = "VOICE_NOTE"


class MessageStatus(StrEnum):
    RECEIVED = "RECEIVED"  # inbound, stored
    QUEUED = "QUEUED"  # outbound, not yet handed to Meta
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    READ = "READ"
    FAILED = "FAILED"


class InboxStatus(StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


def check_constraint(
    column: str, enum_cls: type[StrEnum], name: str
) -> sa.CheckConstraint:
    """Build a named CHECK restricting `column` to `enum_cls`'s values.

    Named explicitly: an anonymous CHECK gets a random PostgreSQL name, which
    makes autogenerate noisy and makes it impossible to report a violated
    constraint by name instead of by conflicting value.

    Alembic's autogenerate never compares CHECK constraints, so changing an enum
    here does NOT produce a migration on its own. Widening one is a hand-written
    migration plus a new value in the runtime test in tests/db/test_constraints.py.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name)
```

- [ ] **Step 5: Write `app/db/base.py`**

```python
"""Declarative base, shared MetaData and the mixins every table uses."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic names for every constraint and index. Without this, PostgreSQL
# invents names for CHECK constraints, Alembic autogenerate reports a diff on
# every run, and a violated constraint cannot be reported by name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        """Identity only. Never column content.

        Hard rule 8: these objects surface in tracebacks, debugger output and
        assertion failures. A repr that helpfully printed `text` or
        `display_name` would put a patient's words into an error tracker.
        """
        parts: list[str] = []
        for name in ("id", "tenant_id"):
            value: Any = getattr(self, name, None)
            if value is not None:
                parts.append(f"{name}={value}")
        return f"<{type(self).__name__} {' '.join(parts)}>"


class UUIDPrimaryKeyMixin:
    # `default=` alone is an INSERT-time default: SQLAlchemy stores it as a
    # CallableColumnDefault and only evaluates it when the row is flushed, so
    # `row.id` would be None until then. It is kept as the fallback for Core
    # inserts that bypass this constructor.
    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)

    def __init__(self, **kwargs: Any) -> None:
        """Assign the id now, not at flush.

        Repositories and tests build a parent and a child in one breath — a
        contact and its identity, a conversation and its messages — and pass the
        parent's id to the child before anything is flushed. With only the
        column default that id is None and the child gets a NULL foreign key.

        MRO note: a model declared as `Contact(UUIDPrimaryKeyMixin,
        TimestampMixin, Base)` runs this first, and `super().__init__` reaches
        the declarative constructor that assigns the remaining kwargs.
        """
        kwargs.setdefault("id", uuid.uuid4())
        super().__init__(**kwargs)


class TimestampMixin:
    created_at: Mapped[Any] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )
    updated_at: Mapped[Any] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )
```

Note on `Mapped[Any]` for the timestamps: `datetime` would be more precise, but `server_default` plus `onupdate` means the attribute is unset until flush. `Any` keeps the annotation honest and keeps ruff quiet; the column type is what reaches the database either way.

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest tests/db/test_base.py -v
```

Expected: 7 passed.

- [ ] **Step 7: Add the session factory to `app/db/session.py`**

Append to the existing module (keep `get_engine` and `ping_database` exactly as they are — VS-001's readiness endpoint depends on them):

```python
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory, building it on first use."""
    global _sessionmaker
    if _sessionmaker is None:
        # expire_on_commit=False: after a commit, the caller can still read the
        # attributes of the object it just saved without a second round trip.
        # With the default, every attribute access after commit re-queries, and
        # in async code that raises MissingGreenlet instead of being merely slow.
        _sessionmaker = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, always closed."""
    async with get_sessionmaker()() as session:
        yield session
```

Also extend `dispose_engine` to clear the factory, so a disposed engine cannot be handed out again:

```python
async def dispose_engine() -> None:
    """Close the connection pool. Called on app shutdown."""
    global _engine, _sessionmaker
    _sessionmaker = None
    if _engine is not None:
        await _engine.dispose()
        _engine = None
```

- [ ] **Step 8: Run the whole suite, lint, format**

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format .
```

Expected: 25 passed.

- [ ] **Step 9: Checkpoint with the developer**

Task complete. Do not start Task 2 until A3 (the type of `tenant_id`) is confirmed.

---

### Task 2: The six models

All the tables VS-002 owns, as SQLAlchemy models. Still no database: every assertion here reads `Base.metadata`.

**BLOCKED until A3 is confirmed.** Every table in this task carries a `tenant_id` column; if that is not a UUID, this task is rewritten rather than amended.

**Files:**
- Create: `app/db/models/__init__.py`
- Create: `app/db/models/webhook_inbox.py`
- Create: `app/db/models/contact.py`
- Create: `app/db/models/conversation.py`
- Create: `app/db/models/message.py`
- Create: `app/db/models/dead_letter.py`
- Test: `tests/db/test_models.py`

**Interfaces:**
- Consumes: `app.db.base.Base`, `UUIDPrimaryKeyMixin`, `TimestampMixin`; every enum and `check_constraint` from `app.db.enums`.
- Produces (importable from `app.db.models`):
  - `WebhookInbox` — `id, provider, provider_event_id, tenant_id (nullable), payload, status, attempts, last_error, created_at, updated_at`
  - `Contact` — `id, tenant_id, display_name, created_at, updated_at`; relationship `identities`
  - `ContactIdentity` — `id, tenant_id, contact_id, channel, external_id, created_at, updated_at`
  - `Conversation` — `id, tenant_id, contact_id, channel, state, state_changed_at, last_inbound_at, created_at, updated_at`
  - `Message` — `id, tenant_id, conversation_id, direction, modality, provider_message_id, text, status, sent_at, created_at, updated_at`
  - `DeadLetterJob` — `id, tenant_id (nullable), job_name, source_event_id, payload, error, attempts, created_at, updated_at`
  - `OPEN_STATES` — the three non-`CLOSED` states.

**Expected tests after this task: 36 (25 + 11 new). All pass with nothing running.**

- [ ] **Step 1: Write the failing model tests**

Create `tests/db/test_models.py`:

```python
"""Schema assertions. No database: these read Base.metadata."""

import sqlalchemy as sa

from app.db.base import Base
from app.db.models import (
    Contact,
    ContactIdentity,
    Conversation,
    DeadLetterJob,
    Message,
    WebhookInbox,
)

ALL_MODELS = [
    WebhookInbox,
    Contact,
    ContactIdentity,
    Conversation,
    Message,
    DeadLetterJob,
]


def _unique_column_sets(table: sa.Table) -> set[frozenset[str]]:
    sets = {
        frozenset(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    sets |= {
        frozenset(c.name for c in index.columns) for index in table.indexes if index.unique
    }
    sets |= {frozenset([column.name]) for column in table.columns if column.unique}
    return sets


def test_the_slice_creates_exactly_these_tables():
    # Clinic, doctor, service, schedule and appointment tables belong to the
    # Booking Service (docs/architecture.md). If one appears here, the slice
    # has leaked out of scope.
    assert set(Base.metadata.tables) == {
        "webhook_inbox",
        "contacts",
        "contact_identities",
        "conversations",
        "messages",
        "dead_letter_jobs",
    }


def test_provider_event_id_is_unique():
    # Hard rule 2: the same Meta event delivered twice produces one stored
    # message and one reply. This constraint is what makes that true under
    # concurrency; an application-level "select then insert" does not.
    assert frozenset(["provider_event_id"]) in _unique_column_sets(WebhookInbox.__table__)


def test_provider_message_id_is_unique_and_nullable():
    column = Message.__table__.c.provider_message_id
    assert frozenset(["provider_message_id"]) in _unique_column_sets(Message.__table__)
    # Nullable: an outbound message that failed before Meta accepted it has no
    # provider id. PostgreSQL allows many NULLs under a unique constraint.
    assert column.nullable is True


def test_a_contact_identity_is_unique_per_tenant_channel_and_external_id():
    assert frozenset(["tenant_id", "channel", "external_id"]) in _unique_column_sets(
        ContactIdentity.__table__
    )


def test_only_one_open_conversation_exists_per_contact_and_channel():
    # Review Focus 3. Two concurrent jobs for the same patient must not each
    # create a conversation. Partial, so a closed conversation does not block
    # the next one.
    index = next(
        i for i in Conversation.__table__.indexes if i.name == "uq_conversations_open"
    )
    assert index.unique is True
    assert [c.name for c in index.columns] == ["tenant_id", "contact_id", "channel"]
    assert index.dialect_options["postgresql"]["where"] is not None


def test_tenant_id_is_not_null_everywhere_a_tenant_is_knowable():
    # Review Focus 7. The webhook stores the raw event before anything resolves
    # a tenant (hard rule 1), and a dead-lettered job may have died before
    # resolution, so those two are nullable. Everything else is not.
    for model in (Contact, ContactIdentity, Conversation, Message):
        assert model.__table__.c.tenant_id.nullable is False, model.__tablename__
    assert WebhookInbox.__table__.c.tenant_id.nullable is True
    assert DeadLetterJob.__table__.c.tenant_id.nullable is True


def test_every_table_has_timezone_aware_timestamps():
    for model in ALL_MODELS:
        for name in ("created_at", "updated_at"):
            column = model.__table__.c[name]
            assert column.type.timezone is True, f"{model.__tablename__}.{name}"


def test_enum_backed_columns_carry_a_named_check_constraint():
    # Declared here, but NOT protected by the drift test: autogenerate never
    # compares CHECK constraints. tests/db/test_constraints.py proves at runtime
    # that they actually reject bad values.
    expected = {
        "conversations": "ck_conversations_state_valid",
        "messages": "ck_messages_direction_valid",
        "webhook_inbox": "ck_webhook_inbox_status_valid",
    }
    for table_name, constraint_name in expected.items():
        table = Base.metadata.tables[table_name]
        names = {c.name for c in table.constraints if isinstance(c, sa.CheckConstraint)}
        assert constraint_name in names, f"{table_name}: {names}"


def test_messages_are_indexed_for_conversation_history():
    # VS-005 reads "the last N messages" on every turn. Without this index that
    # is a sequential scan of every message the clinic has ever exchanged.
    index_columns = [[c.name for c in index.columns] for index in Message.__table__.indexes]
    assert ["tenant_id", "conversation_id", "created_at"] in index_columns


def test_deleting_a_conversation_deletes_its_messages():
    fk = next(iter(Message.__table__.c.conversation_id.foreign_keys))
    assert fk.column.table.name == "conversations"
    assert fk.ondelete == "CASCADE"


def test_no_model_repr_leaks_content():
    # Hard rule 8, per model rather than on the shared base, because a future
    # model could override __repr__ without anyone noticing.
    samples = {
        Contact: {"display_name": "Reem Haddad"},
        ContactIdentity: {"external_id": "96170123456"},
        Message: {"text": "my knee hurts"},
        DeadLetterJob: {"error": "my knee hurts"},
    }
    for model, kwargs in samples.items():
        rendered = repr(model(**kwargs))
        for value in kwargs.values():
            assert value not in rendered, f"{model.__name__} leaked {value!r}"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/db/test_models.py -v
```

Expected: collection error, `ModuleNotFoundError: No module named 'app.db.models'`.

- [ ] **Step 3: Write `app/db/models/webhook_inbox.py`**

```python
"""The raw-event landing table.

Hard rule 1: the webhook endpoint only verifies, dedupes, stores here, enqueues
and returns 200. Everything downstream reads from this row, not from the request.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import Channel, InboxStatus, check_constraint


class WebhookInbox(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "webhook_inbox"
    __table_args__ = (check_constraint("status", InboxStatus, "status_valid"),)

    provider: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=Channel.WHATSAPP.value
    )
    # Hard rule 2 lives here. Unique at the database level, because two workers
    # can run "SELECT then INSERT" at the same moment and both see nothing.
    provider_event_id: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, unique=True
    )
    # Nullable: the tenant is resolved from phone_number_id by the worker
    # (hard rule 4). Requiring it here would force resolution inside the
    # webhook request, which hard rule 1 forbids.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=InboxStatus.RECEIVED.value
    )
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    # A short reason code or exception class name. Never a raw provider payload
    # excerpt: that would copy patient text out of `payload` into a column that
    # gets read casually.
    last_error: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
```

- [ ] **Step 4: Write `app/db/models/contact.py`**

```python
"""Patients, and the channel identities that point at them.

A contact is one person at one clinic. contact_identities exists rather than a
phone column on contacts because VS-008 and later channels attach more than one
identity to the same person.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import Channel, check_constraint


class Contact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (sa.Index("ix_contacts_tenant_id", "tenant_id"),)

    tenant_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    # Patient content (hard rule 8): stored, never logged, never in a repr.
    display_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    identities: Mapped[list["ContactIdentity"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan", lazy="selectin"
    )


class ContactIdentity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contact_identities"
    __table_args__ = (
        # "unique per tenant" from the slice. Two clinics may legitimately have
        # the same patient phone number; one clinic may not have it twice.
        # Named explicitly rather than by convention because the repository
        # reports it by name when a concurrent insert loses the race.
        sa.UniqueConstraint(
            "tenant_id", "channel", "external_id", name="uq_contact_identities_identity"
        ),
        check_constraint("channel", Channel, "channel_valid"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # The WhatsApp wa_id, i.e. the patient's phone number. Patient content:
    # this is the value a unique-violation message would quote, which is why
    # repositories upsert instead of catching IntegrityError.
    external_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    contact: Mapped[Contact] = relationship(back_populates="identities")
```

- [ ] **Step 5: Write `app/db/models/conversation.py`**

```python
"""One ongoing thread between a clinic and a patient on one channel."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import Channel, ConversationState, check_constraint

OPEN_STATES = (
    ConversationState.AI_ACTIVE,
    ConversationState.HUMAN_REQUESTED,
    ConversationState.HUMAN_ACTIVE,
)


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        check_constraint("state", ConversationState, "state_valid"),
        check_constraint("channel", Channel, "channel_valid"),
        # Review Focus 3. At-least-once jobs plus Meta's duplicate and
        # out-of-order delivery mean two workers can both find no open
        # conversation and both insert one. Partial, so closing a conversation
        # frees the slot for the next one.
        sa.Index(
            "uq_conversations_open",
            "tenant_id",
            "contact_id",
            "channel",
            unique=True,
            postgresql_where=sa.text("state <> 'CLOSED'"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default=ConversationState.AI_ACTIVE.value
    )
    state_changed_at: Mapped[Any] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    # WhatsApp allows free-form replies only within 24h of the patient's last
    # message (docs/architecture.md). VS-004 needs this to decide between a
    # free-form reply and a template; storing it now costs one column.
    last_inbound_at: Mapped[Any | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
```

- [ ] **Step 6: Write `app/db/models/message.py`**

```python
"""Every message in both directions, text and (from VS-008) voice."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import (
    MessageDirection,
    MessageModality,
    MessageStatus,
    check_constraint,
)


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        check_constraint("direction", MessageDirection, "direction_valid"),
        check_constraint("modality", MessageModality, "modality_valid"),
        check_constraint("status", MessageStatus, "status_valid"),
        # VS-005 reads the last N messages of a conversation on every turn.
        sa.Index(
            "ix_messages_tenant_id_conversation_id_created_at",
            "tenant_id",
            "conversation_id",
            "created_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    modality: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=MessageModality.TEXT.value
    )
    # Unique so a replayed Meta event cannot store the same message twice
    # (hard rule 2). Nullable because an outbound message that failed before
    # Meta accepted it never receives an id; PostgreSQL permits many NULLs
    # under a unique constraint.
    provider_message_id: Mapped[str | None] = mapped_column(
        sa.String(255), nullable=True, unique=True
    )
    # Patient content (hard rule 8). Also holds the transcript for a voice note
    # from VS-008 onward.
    text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    sent_at: Mapped[Any | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
```

- [ ] **Step 7: Write `app/db/models/dead_letter.py`**

```python
"""Jobs that exhausted their retries.

Hard rule 11: jobs that keep failing land here instead of retrying forever.
A row is a thing a human looks at, not something the worker reads back.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DeadLetterJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "dead_letter_jobs"
    __table_args__ = (sa.Index("ix_dead_letter_jobs_created_at", "created_at"),)

    # Nullable: a job can die before tenant resolution succeeds, and that is
    # precisely the failure most worth recording.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    job_name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    # The webhook_inbox.provider_event_id this job came from, when there is one.
    # Deliberately not a foreign key: the inbox row may be pruned by a retention
    # policy long before anyone reviews the dead letter.
    source_event_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # Same patient content as webhook_inbox.payload. The retention follow-up
    # covers both tables.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[str] = mapped_column(sa.String(1000), nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
```

- [ ] **Step 8: Write `app/db/models/__init__.py`**

```python
"""Importing this package registers every table on Base.metadata.

migrations/env.py imports it for exactly that reason: autogenerate compares the
database against Base.metadata, and a model nobody imported is a table Alembic
will cheerfully propose dropping.
"""

from app.db.models.contact import Contact, ContactIdentity
from app.db.models.conversation import OPEN_STATES, Conversation
from app.db.models.dead_letter import DeadLetterJob
from app.db.models.message import Message
from app.db.models.webhook_inbox import WebhookInbox

__all__ = [
    "OPEN_STATES",
    "Contact",
    "ContactIdentity",
    "Conversation",
    "DeadLetterJob",
    "Message",
    "WebhookInbox",
]
```

- [ ] **Step 9: Run the tests to verify they pass**

```bash
uv run pytest tests/db -v
```

Expected: 18 passed.

If `test_the_slice_creates_exactly_these_tables` fails with `_sample` in the set, `tests/db/test_base.py`'s throwaway model landed on the real `Base` — it must sit on `_SampleBase` (Task 1, Step 2), which is exactly why that separate registry exists. This only shows up when both modules are imported into one process, which is every whole-directory run.

- [ ] **Step 10: Run the whole suite, lint, format**

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format .
```

Expected: 36 passed.

- [ ] **Step 11: Checkpoint with the developer**

---

### Task 3: Alembic, the image, the initial migration, and the database test harness

Wires Alembic to the app's settings rather than to `alembic.ini`, gets alembic and the migrations *into the container*, generates the one migration that creates all six tables, and builds the fixtures every later database test uses. First task that needs a running PostgreSQL.

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/<rev>_initial_messaging_schema.py`
- Modify: `Dockerfile` (copy `alembic.ini` and `migrations/`)
- Modify: `docker-compose.yml` (bind-mount `./migrations` and `./alembic.ini` on `api`)
- Create: `tests/db/conftest.py`
- Test: `tests/db/test_migrations.py`
- Modify: `.env.example` (document `TEST_DATABASE_URL`)

**Interfaces:**
- Consumes: `app.db.base.Base`, `app.db.models` (import for side effects), `app.config.get_settings()`.
- Produces:
  - `tests/db/conftest.py::test_database_url` (session, sync) — the DSN, or `pytest.skip`.
  - `tests/db/conftest.py::migrated_database` (session, sync) — ensures the database exists and is at `head`.
  - `tests/db/conftest.py::db_engine` (function, async) — an `AsyncEngine` on the test database.
  - `tests/db/conftest.py::db_session` (function, async) — an `AsyncSession` in a transaction that is always rolled back.
  - `tests/db/conftest.py::second_session_factory` (function) — independent sessions for concurrency tests.
  - `tests/db/conftest.py::alembic_config(url) -> alembic.config.Config`.
  - `tests/db/conftest.py::_ensure_database(url)` — creates the database if missing.

**Expected tests after this task: 40 (36 + 4 new). With no database: 36 pass, 4 skip.**

- [ ] **Step 1: Generate the Alembic scaffolding**

```bash
uv run alembic init -t async migrations
```

This writes `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako` and `migrations/versions/`. The `-t async` template matters: the default template opens a synchronous connection, which asyncpg cannot serve.

- [ ] **Step 2: Edit `alembic.ini`**

Set exactly these (leave the rest of the generated file alone):

```ini
[alembic]
script_location = migrations
prepend_sys_path = .
# Deliberately empty. Hard rule 9: no DSN or password in committed source.
# migrations/env.py fills this from app.config, or a caller supplies it via
# Config.set_main_option (which is how the test harness targets doctoleb_test).
sqlalchemy.url =
```

- [ ] **Step 3: Rewrite `migrations/env.py`**

```python
"""Alembic environment.

Three things differ from the generated template:
  * the DSN comes from app.config, never from alembic.ini (hard rule 9);
  * app.db.models is imported for its side effects, so every table is on
    Base.metadata before autogenerate compares anything. A model nobody
    imported is a table Alembic proposes to DROP;
  * fileConfig is called with disable_existing_loggers=False.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.db.models  # noqa: F401  (import for side effects: registers the tables)
from app.config import get_settings
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which silences every logger
    # configured before this call. The test suite runs migrations in-process
    # (tests/db sorts before tests/test_readiness.py), so the default would
    # disable app.api.health's logger and make VS-001's caplog assertions fail
    # in the full run only — green file-by-file, red together.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """A caller-supplied URL wins; otherwise the app's configured DSN."""
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without these, a changed column type or server default is invisible to
        # autogenerate. Note that no option makes it compare CHECK constraints;
        # see Review Focus 5.
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.")
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 4: Add `TEST_DATABASE_URL` to `.env.example`**

Under the `# Database / Redis` block:

```
# Tests use a separate database on the same server. Leave unset and the test
# suite derives it by swapping the database name on DATABASE_URL to
# doctoleb_test, creating it on first run. Database tests skip when unreachable.
TEST_DATABASE_URL=
```

- [ ] **Step 5: Put alembic and the migrations inside the image**

The VS-001 `Dockerfile` copies only `app/` and `tests/`, so `alembic.ini` and `migrations/` do not exist in the container and `alembic` cannot run there at all. Add both, next to the existing source copy:

```dockerfile
# Then the source, which changes constantly.
COPY app ./app
COPY tests ./tests
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen
```

- [ ] **Step 6: Bind-mount the migrations on the api service**

In `docker-compose.yml`, under `api.volumes`, alongside the existing `./app` and `./tests` mounts:

```yaml
    volumes:
      # Bind-mount the source so --reload picks up edits without a rebuild.
      - ./app:/srv/app
      - ./tests:/srv/tests
      # Without these two, `alembic revision --autogenerate` inside the
      # container writes the new revision into the container's own filesystem
      # and it vanishes with the container. The mount is what puts the
      # generated file on the host, where it can be reviewed and committed.
      - ./migrations:/srv/migrations
      - ./alembic.ini:/srv/alembic.ini
```

The `worker` service needs neither: it never runs migrations. The image carries them anyway, which is what lets a deploy run `alembic upgrade head` from either container.

- [ ] **Step 7: Rebuild, then generate and review the initial migration**

The rebuild is mandatory and comes first. The running image predates both alembic (added to `uv.lock` in Task 1) and the two `COPY` lines above, so `docker compose exec api alembic ...` fails with `executable file not found` until it happens.

```bash
docker compose up -d --build
docker compose exec api alembic --version          # proves the rebuild took
docker compose exec api alembic revision --autogenerate -m "initial messaging schema"
ls migrations/versions/                            # proves the bind-mount took
```

Read the generated file before trusting it. It must contain, in `upgrade()`: `create_table` for all six tables; the `uq_conversations_open` index with `postgresql_where=sa.text("state <> 'CLOSED'")`; every named CHECK constraint; the `ix_messages_tenant_id_conversation_id_created_at` index. And in `downgrade()`: the exact inverse, dropping indexes before their tables.

Check specifically that `downgrade()` is not empty between the `# ###` markers. Autogenerate occasionally emits an empty downgrade when it cannot invert something; Review Focus 4 exists because "downgrade works" is usually read as "did not raise".

Check the CHECK constraints are present in `create_table` by eye. They are emitted once, here, and never compared again — the drift test in Step 9 will not notice if one is missing.

Rename the generated file to `migrations/versions/<rev>_initial_messaging_schema.py` if Alembic chose an unhelpful slug.

- [ ] **Step 8: Write `tests/db/conftest.py`**

```python
"""Fixtures for tests that need a real PostgreSQL.

Every fixture here is allowed to skip. VS-001's rule that `pytest` passes with
nothing running still holds: with no database, these tests report SKIPPED with a
reason and the rest of the suite is untouched.

The two session-scoped fixtures are SYNCHRONOUS on purpose. Alembic's async
template calls asyncio.run() inside env.py; calling it from inside a
pytest-asyncio coroutine raises
    RuntimeError: asyncio.run() cannot be called from a running event loop.
A sync fixture runs outside the loop and the problem disappears.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

TEST_DATABASE_NAME = "doctoleb_test"


def _derive_test_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    parsed = urlparse(os.environ["DATABASE_URL"])
    return urlunparse(parsed._replace(path=f"/{TEST_DATABASE_NAME}"))


def alembic_config(url: str) -> Config:
    """An Alembic Config pointed at `url`."""
    config = Config("alembic.ini")
    config.set_main_option("script_location", "migrations")
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _ensure_database(url: str) -> None:
    """Create the target database if it is missing.

    Connects with raw asyncpg to the `postgres` maintenance database: CREATE
    DATABASE cannot run inside a transaction, and SQLAlchemy wraps everything
    in one by default.
    """
    parsed = urlparse(url)
    target = parsed.path.lstrip("/")
    admin_dsn = urlunparse(parsed._replace(scheme="postgresql", path="/postgres"))
    connection = await asyncpg.connect(admin_dsn)
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target
        )
        if not exists:
            await connection.execute(f'CREATE DATABASE "{target}"')
    finally:
        await connection.close()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """The test DSN, or skip every database test with a readable reason."""
    url = _derive_test_url()
    parsed = urlparse(url)
    try:
        asyncio.run(_ensure_database(url))
    except (OSError, asyncpg.PostgresError) as error:
        # Host and port only. The DSN carries a password (hard rule 9).
        pytest.skip(
            f"no PostgreSQL at {parsed.hostname}:{parsed.port} "
            f"({type(error).__name__}); run `docker compose up -d postgres`"
        )
    return url


@pytest.fixture(scope="session")
def migrated_database(test_database_url: str) -> str:
    """Bring the test database to head, once per test session."""
    command.upgrade(alembic_config(test_database_url), "head")
    return test_database_url


@pytest.fixture
async def db_engine(migrated_database: str) -> AsyncIterator:
    """Function-scoped on purpose.

    pytest-asyncio runs this project with asyncio_default_fixture_loop_scope =
    "function"; a session-scoped async fixture would bind an engine to a loop
    that closes after the first test. Creating an engine is cheap.
    """
    engine = create_async_engine(migrated_database)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(db_engine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    join_transaction_mode="create_savepoint" lets a test call session.commit()
    to exercise real commit behaviour without ending the outer transaction, so
    the next test still starts from an empty database.

    Everything in one test therefore shares one transaction, which means
    PostgreSQL's now() is a single constant throughout. Tests that assert a
    timestamp moved must first set it to a fixed past value.
    """
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        session = factory()
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
def second_session_factory(db_engine):
    """A factory for genuinely independent sessions.

    Concurrency tests need two connections that cannot see each other's
    uncommitted rows, which the rollback-wrapped db_session cannot provide.
    Tests using this clean up after themselves.
    """
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)
```

- [ ] **Step 9: Write the failing migration tests**

Create `tests/db/test_migrations.py`:

```python
"""Acceptance: `alembic upgrade head` on an empty DB works, and downgrade works."""

import asyncio
from urllib.parse import urlparse, urlunparse

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
from tests.db.conftest import _ensure_database, alembic_config

pytestmark = pytest.mark.db

LIFECYCLE_DATABASE_NAME = "doctoleb_test_migrations"

EXPECTED_TABLES = {
    "webhook_inbox",
    "contacts",
    "contact_identities",
    "conversations",
    "messages",
    "dead_letter_jobs",
    "alembic_version",
}


@pytest.fixture
def lifecycle_url(test_database_url: str) -> str:
    """A throwaway database, so upgrading and downgrading here cannot disturb
    the shared doctoleb_test that the other database tests rely on."""
    parsed = urlparse(test_database_url)
    url = urlunparse(parsed._replace(path=f"/{LIFECYCLE_DATABASE_NAME}"))
    asyncio.run(_ensure_database(url))
    command.downgrade(alembic_config(url), "base")
    return url


def _table_names(url: str) -> set[str]:
    async def _read() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:
                return set(
                    await connection.run_sync(
                        lambda sync: sa.inspect(sync).get_table_names()
                    )
                )
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def test_upgrade_head_on_an_empty_database_creates_every_table(lifecycle_url: str):
    assert _table_names(lifecycle_url) <= {"alembic_version"}
    command.upgrade(alembic_config(lifecycle_url), "head")
    assert _table_names(lifecycle_url) == EXPECTED_TABLES


def test_downgrade_base_leaves_nothing_behind(lifecycle_url: str):
    # Review Focus 4. "downgrade works" usually means "did not raise". A leftover
    # index or constraint makes the NEXT upgrade fail on a database that looks
    # empty.
    command.upgrade(alembic_config(lifecycle_url), "head")
    command.downgrade(alembic_config(lifecycle_url), "base")
    assert _table_names(lifecycle_url) == {"alembic_version"}


def test_upgrade_downgrade_upgrade_is_repeatable(lifecycle_url: str):
    config = alembic_config(lifecycle_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert _table_names(lifecycle_url) == EXPECTED_TABLES


def test_models_and_migrations_do_not_drift(migrated_database: str):
    # Review Focus 5. A column added to a model and never migrated works on every
    # machine whose database someone fixed by hand, and fails on a fresh deploy.
    #
    # Covers: added/removed tables and columns, type changes, nullability,
    # indexes, unique constraints. Does NOT cover CHECK constraints — alembic
    # has no comparison for them at all. tests/db/test_constraints.py proves
    # those at runtime instead.
    async def _diff() -> list:
        engine = create_async_engine(migrated_database)
        try:
            async with engine.connect() as connection:
                return await connection.run_sync(_compare)
        finally:
            await engine.dispose()

    def _compare(sync_connection) -> list:
        context = MigrationContext.configure(
            sync_connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        return compare_metadata(context, Base.metadata)

    assert asyncio.run(_diff()) == []
```

- [ ] **Step 10: Run the migration tests**

```bash
docker compose up -d postgres
uv run pytest tests/db/test_migrations.py -v
```

Expected: 4 passed.

If `test_models_and_migrations_do_not_drift` fails, read the diff it prints and fix the **migration**, not the test. The usual causes are a `server_default` written as `sa.text("now()")` in one place and `sa.func.now()` in the other, or a column type mismatch.

- [ ] **Step 11: Verify the skip path**

```bash
docker compose stop postgres
uv run pytest -q
docker compose start postgres
```

Expected: `36 passed, 4 skipped`, with the skip reason naming the host and port and containing no password.

- [ ] **Step 12: Run the acceptance commands by hand, lint**

```bash
docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec api alembic downgrade base
docker compose exec api alembic upgrade head
uv run ruff check . && uv run ruff format .
```

Expected: each command exits 0 and prints its revision transitions.

- [ ] **Step 13: Checkpoint with the developer**

---

### Task 4: Constraint behaviour against a real PostgreSQL

The slice's "Understand first" line is *primary/foreign keys, unique constraints — they are what make deduplication safe*. This task proves each one behaves as claimed, including the CHECK constraints that the drift test structurally cannot see. No production code changes; if a test here fails, the fix is in Task 2's models plus a new migration.

**Files:**
- Create: `tests/db/factories.py`
- Test: `tests/db/test_constraints.py`

**Interfaces:**
- Consumes: `db_session`, `db_engine`, `second_session_factory` (Task 3); every model (Task 2).
- Produces:
  - `tests/db/factories.py::TENANT_A`, `TENANT_B` — fixed synthetic UUIDs.
  - `tests/db/factories.py::phone(n)`, `event_id(n)`, `wamid(n)` — synthetic identifiers.
  - `tests/db/factories.py::make_inbox`, `make_contact`, `make_identity`, `make_conversation`, `make_message` — unsaved model instances.
  - `tests/db/factories.py::LONG_AGO` — a fixed past timestamp for "did this move?" assertions.

**Expected tests after this task: 52 (40 + 12 new). With no database: 36 pass, 16 skip.**

- [ ] **Step 1: Write `tests/db/factories.py`**

```python
"""Synthetic test data.

Hard rule 8 forbids test fixtures built from real patient data. Everything here
is generated from an integer, so nothing in this repo's history can ever be
traced to a person.
"""

import datetime as dt
import uuid
from typing import Any

from app.db.enums import (
    Channel,
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
)
from app.db.models import Contact, ContactIdentity, Conversation, Message, WebhookInbox

TENANT_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
TENANT_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")

# PostgreSQL's now() is the TRANSACTION start time, constant for a whole test.
# Any assertion that a timestamp column moved needs the column set to a fixed
# past value first, or it compares now() against now() and can never fail.
LONG_AGO = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)


def phone(n: int) -> str:
    """A synthetic MSISDN in a documentation-safe range."""
    return f"96170{n:06d}"


def event_id(n: int) -> str:
    return f"evt-{n:08d}"


def wamid(n: int) -> str:
    return f"wamid.TEST{n:08d}"


def make_inbox(n: int, **overrides: Any) -> WebhookInbox:
    values: dict[str, Any] = {
        "provider": Channel.WHATSAPP.value,
        "provider_event_id": event_id(n),
        "payload": {"n": n},
        "status": InboxStatus.RECEIVED.value,
    }
    values.update(overrides)
    return WebhookInbox(**values)


def make_contact(tenant_id: uuid.UUID = TENANT_A, **overrides: Any) -> Contact:
    values: dict[str, Any] = {"tenant_id": tenant_id, "display_name": "Test Patient"}
    values.update(overrides)
    return Contact(**values)


def make_identity(
    contact: Contact, n: int = 1, tenant_id: uuid.UUID | None = None, **overrides: Any
) -> ContactIdentity:
    values: dict[str, Any] = {
        "tenant_id": tenant_id or contact.tenant_id,
        "contact_id": contact.id,
        "channel": Channel.WHATSAPP.value,
        "external_id": phone(n),
    }
    values.update(overrides)
    return ContactIdentity(**values)


def make_conversation(contact: Contact, **overrides: Any) -> Conversation:
    values: dict[str, Any] = {
        "tenant_id": contact.tenant_id,
        "contact_id": contact.id,
        "channel": Channel.WHATSAPP.value,
        "state": ConversationState.AI_ACTIVE.value,
    }
    values.update(overrides)
    return Conversation(**values)


def make_message(conversation: Conversation, **overrides: Any) -> Message:
    values: dict[str, Any] = {
        "tenant_id": conversation.tenant_id,
        "conversation_id": conversation.id,
        "direction": MessageDirection.INBOUND.value,
        "modality": MessageModality.TEXT.value,
        "status": MessageStatus.RECEIVED.value,
        "text": "synthetic message body",
    }
    values.update(overrides)
    return Message(**values)
```

Every factory reads `contact.id` / `conversation.id` on an object that may not be flushed yet. That only works because `UUIDPrimaryKeyMixin.__init__` assigns the id at construction (Task 1, Step 5).

- [ ] **Step 2: Write the failing constraint tests**

Create `tests/db/test_constraints.py`:

```python
"""What the database refuses to store. These are the safety net, not the
repositories on top of them."""

import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.enums import ConversationState
from app.db.models import Contact, Conversation, Message, WebhookInbox
from tests.db import factories as f

pytestmark = pytest.mark.db


async def test_duplicate_provider_event_id_is_rejected(db_session):
    # Hard rule 2: the same Meta event delivered twice produces exactly one
    # stored message and one reply.
    db_session.add(f.make_inbox(1))
    await db_session.flush()
    db_session.add(f.make_inbox(1))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_second_distinct_event_is_stored(db_session):
    db_session.add_all([f.make_inbox(1), f.make_inbox(2)])
    await db_session.flush()
    count = await db_session.scalar(sa.select(sa.func.count()).select_from(WebhookInbox))
    assert count == 2


async def test_webhook_inbox_accepts_an_unresolved_tenant(db_session):
    # Review Focus 7. The webhook stores before anything resolves a tenant
    # (hard rule 1). A NOT NULL here would force resolution inside the request.
    db_session.add(f.make_inbox(1, tenant_id=None))
    await db_session.flush()

    db_session.add(Contact(tenant_id=None))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_invalid_conversation_state_is_rejected(db_session):
    # Review Focus 5. Alembic's autogenerate never compares CHECK constraints,
    # so the drift test cannot protect them — this is the only thing standing
    # between a typo'd or silently-widened enum and rows the app cannot read
    # back. Inserted through Core so the ORM does not coerce the value first.
    contact = f.make_contact()
    db_session.add(contact)
    await db_session.flush()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            sa.insert(Conversation).values(
                id=f.uuid.uuid4(),
                tenant_id=contact.tenant_id,
                contact_id=contact.id,
                channel="whatsapp",
                state="BANANA",
            )
        )


async def test_duplicate_provider_message_id_is_rejected(db_session):
    contact = f.make_contact()
    db_session.add(contact)
    conversation = f.make_conversation(contact)
    db_session.add(conversation)
    await db_session.flush()

    db_session.add(f.make_message(conversation, provider_message_id=f.wamid(1)))
    await db_session.flush()
    db_session.add(f.make_message(conversation, provider_message_id=f.wamid(1)))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_many_messages_may_have_no_provider_message_id(db_session):
    # An outbound message that failed before Meta accepted it never gets an id.
    # PostgreSQL permits many NULLs under a unique constraint; if that were not
    # true, the second failed send would crash the worker.
    contact = f.make_contact()
    db_session.add(contact)
    conversation = f.make_conversation(contact)
    db_session.add(conversation)
    await db_session.flush()

    db_session.add_all(
        [
            f.make_message(conversation, provider_message_id=None),
            f.make_message(conversation, provider_message_id=None),
        ]
    )
    await db_session.flush()
    count = await db_session.scalar(sa.select(sa.func.count()).select_from(Message))
    assert count == 2


async def test_the_same_phone_number_may_exist_in_two_tenants(db_session):
    # A patient can be a patient of two clinics. "Unique per tenant" is the
    # whole point of the constraint's shape.
    for tenant in (f.TENANT_A, f.TENANT_B):
        contact = f.make_contact(tenant)
        db_session.add(contact)
        await db_session.flush()
        db_session.add(f.make_identity(contact, n=1))
    await db_session.flush()


async def test_the_same_phone_number_twice_in_one_tenant_is_rejected(db_session):
    first = f.make_contact(f.TENANT_A)
    second = f.make_contact(f.TENANT_A)
    db_session.add_all([first, second])
    await db_session.flush()

    db_session.add(f.make_identity(first, n=1))
    await db_session.flush()
    db_session.add(f.make_identity(second, n=1))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_two_concurrent_inserts_produce_one_open_conversation(
    db_engine, second_session_factory
):
    # Review Focus 3. Two workers processing two messages from the same patient
    # at the same moment both see no open conversation. This test uses two real
    # connections, because the rollback-wrapped db_session cannot show one
    # session what the other has not committed.
    async with second_session_factory() as setup:
        contact = f.make_contact()
        setup.add(contact)
        await setup.commit()
        contact_id = contact.id

    try:
        async with second_session_factory() as one, second_session_factory() as two:
            one.add(f.make_conversation(contact))
            await one.commit()

            two.add(f.make_conversation(contact))
            with pytest.raises(IntegrityError):
                await two.commit()

        async with second_session_factory() as check:
            count = await check.scalar(
                sa.select(sa.func.count())
                .select_from(Conversation)
                .where(Conversation.contact_id == contact_id)
            )
            assert count == 1
    finally:
        async with second_session_factory() as cleanup:
            await cleanup.execute(sa.delete(Contact).where(Contact.id == contact_id))
            await cleanup.commit()


async def test_closing_a_conversation_frees_the_slot(db_session):
    # The index is partial. Without WHERE state <> 'CLOSED', a patient who ever
    # had a conversation closed could never start another one.
    contact = f.make_contact()
    db_session.add(contact)
    await db_session.flush()

    first = f.make_conversation(contact)
    db_session.add(first)
    await db_session.flush()

    first.state = ConversationState.CLOSED.value
    await db_session.flush()

    db_session.add(f.make_conversation(contact))
    await db_session.flush()


async def test_deleting_a_conversation_deletes_its_messages(db_session):
    contact = f.make_contact()
    db_session.add(contact)
    conversation = f.make_conversation(contact)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(f.make_message(conversation))
    await db_session.flush()

    await db_session.execute(
        sa.delete(Conversation).where(Conversation.id == conversation.id)
    )
    await db_session.flush()
    count = await db_session.scalar(sa.select(sa.func.count()).select_from(Message))
    assert count == 0


async def test_stored_timestamps_come_back_timezone_aware(db_session):
    # A naive TIMESTAMP column returns a datetime with tzinfo=None, and every
    # later comparison against an aware "now" raises TypeError — or worse,
    # silently computes the 24h WhatsApp window in the wrong zone.
    row = f.make_inbox(1)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.created_at.tzinfo is not None
    assert row.created_at.utcoffset() is not None
    assert abs(row.created_at - dt.datetime.now(dt.UTC)) < dt.timedelta(minutes=5)
```

- [ ] **Step 3: Run the tests**

```bash
docker compose up -d postgres
uv run pytest tests/db/test_constraints.py -v
```

Expected: 12 passed, because the constraints were written in Task 2. If any fails, the model is wrong — fix `app/db/models/`, then:

```bash
docker compose exec api alembic revision --autogenerate -m "fix <what>"
docker compose exec api alembic upgrade head
```

and re-run. **A CHECK-constraint fix will not autogenerate** — alembic cannot see them, so write `op.drop_constraint` / `op.create_check_constraint` by hand in the new revision.

Do not edit the already-committed initial migration; add a new revision. Re-creating `doctoleb_test` from scratch is easier than fighting a half-applied schema:

```bash
docker compose exec postgres dropdb -U doctoleb --if-exists doctoleb_test
```

- [ ] **Step 4: Run the whole suite both ways, lint**

```bash
uv run pytest -q                       # 52 passed
docker compose stop postgres && uv run pytest -q && docker compose start postgres
uv run ruff check . && uv run ruff format .
```

Expected: 52 passed with Postgres up; `36 passed, 16 skipped` with it down.

- [ ] **Step 5: Checkpoint with the developer**

---

### Task 5: Tenant-scoped repositories

The slice's second acceptance criterion: *repository queries require tenant_id*. Enforced by construction — a tenant-scoped repository cannot be built without one, and every statement it emits filters on it.

**Files:**
- Create: `app/db/repositories/__init__.py`
- Create: `app/db/repositories/errors.py`
- Create: `app/db/repositories/base.py`
- Create: `app/db/repositories/webhook_inbox.py`
- Create: `app/db/repositories/contacts.py`
- Create: `app/db/repositories/conversations.py`
- Create: `app/db/repositories/messages.py`
- Create: `app/db/repositories/dead_letter.py`
- Test: `tests/db/test_repository_contract.py` (**no `db` marker**)
- Test: `tests/db/test_repositories.py`

**Interfaces:**
- Consumes: every model (Task 2); `db_session` and the factories (Tasks 3, 4).
- Produces:
  - `errors.DuplicateRecordError(constraint: str)` — `str()` is the constraint name, nothing else.
  - `errors.as_duplicate(error: IntegrityError) -> DuplicateRecordError`.
  - `base.Repository(session)` and `base.TenantScopedRepository(session, tenant_id)` — the latter raises `ValueError` on a falsy `tenant_id`.
  - `WebhookInboxRepository(session)` — `store_if_new(provider_event_id, payload, provider) -> WebhookInbox | None`, `mark(row_id, status, error=None)`, `attach_tenant(row_id, tenant_id)`.
  - `ContactRepository(session, tenant_id)` — `get(contact_id)`, `get_by_identity(channel, external_id)`, `get_or_create_by_identity(channel, external_id, display_name=None)`.
  - `ConversationRepository(session, tenant_id)` — `get(conversation_id)`, `get_open(contact_id, channel)`, `get_or_create_open(contact_id, channel)`, `set_state(conversation_id, state)`.
  - `MessageRepository(session, tenant_id)` — `add(...)`, `recent(conversation_id, limit)`, `get_by_provider_id(provider_message_id)`.
  - `DeadLetterJobRepository(session)` — `add(job_name, payload, error, attempts, tenant_id=None, source_event_id=None)`.

**Expected tests after this task: 68 (52 + 16 new, one of which needs no database). With no database: 37 pass, 31 skip.**

- [ ] **Step 1: Write the failing construction-contract test**

This is an acceptance criterion, so it must run when nothing is up. `TenantScopedRepository.__init__` raises before the session is touched, so the test can pass `None`.

Create `tests/db/test_repository_contract.py`:

```python
"""Repository construction rules. No database and no marker: the slice's
"repository queries require tenant_id" criterion has to be provable with
nothing running."""

import pytest

from app.db.repositories import (
    ContactRepository,
    ConversationRepository,
    MessageRepository,
)
from app.db.repositories.base import Repository, TenantScopedRepository


def test_a_tenant_scoped_repository_cannot_be_built_without_a_tenant():
    # Hard rule 4 made structural: there is no call site that can forget it.
    # `None` for the session is deliberate — __init__ must reject the missing
    # tenant before it ever looks at a connection.
    for repository in (ContactRepository, ConversationRepository, MessageRepository):
        for missing in (None, ""):
            with pytest.raises(ValueError, match="tenant_id"):
                repository(None, missing)


def test_only_the_pre_tenant_tables_use_the_unscoped_repository():
    # webhook_inbox and dead_letter_jobs are written before, or instead of,
    # tenant resolution. Nothing else may opt out of the tenant filter.
    for repository in (ContactRepository, ConversationRepository, MessageRepository):
        assert issubclass(repository, TenantScopedRepository)
    assert issubclass(TenantScopedRepository, Repository)
```

- [ ] **Step 2: Write the failing repository behaviour tests**

Create `tests/db/test_repositories.py`:

```python
"""Repositories. The database enforces the rules; these make them convenient
and make tenant_id impossible to forget."""

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.enums import (
    Channel,
    ConversationState,
    InboxStatus,
    MessageDirection,
    MessageModality,
    MessageStatus,
)
from app.db.models import Conversation, ContactIdentity
from app.db.repositories import (
    ContactRepository,
    ConversationRepository,
    DeadLetterJobRepository,
    MessageRepository,
    WebhookInboxRepository,
)
from app.db.repositories.errors import DuplicateRecordError, as_duplicate
from tests.db import factories as f

pytestmark = pytest.mark.db


async def test_get_or_create_by_identity_creates_a_contact_and_an_identity(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(
        Channel.WHATSAPP, f.phone(1), display_name="Test Patient"
    )
    assert contact.tenant_id == f.TENANT_A
    identity_count = await db_session.scalar(
        sa.select(sa.func.count()).select_from(ContactIdentity)
    )
    assert identity_count == 1


async def test_get_or_create_by_identity_is_idempotent(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    first = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    second = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    assert first.id == second.id


async def test_two_tenants_with_the_same_phone_get_separate_contacts(db_session):
    a = await ContactRepository(db_session, f.TENANT_A).get_or_create_by_identity(
        Channel.WHATSAPP, f.phone(1)
    )
    b = await ContactRepository(db_session, f.TENANT_B).get_or_create_by_identity(
        Channel.WHATSAPP, f.phone(1)
    )
    assert a.id != b.id


async def test_a_contact_lookup_returns_nothing_for_another_tenants_id(db_session):
    # Review Focus 6. A correct-looking id from the wrong tenant must produce no
    # data, not another clinic's patient.
    a = await ContactRepository(db_session, f.TENANT_A).get_or_create_by_identity(
        Channel.WHATSAPP, f.phone(1)
    )
    assert await ContactRepository(db_session, f.TENANT_B).get(a.id) is None
    assert await ContactRepository(db_session, f.TENANT_A).get(a.id) is not None


async def test_a_duplicate_identity_error_never_carries_the_phone_number(db_session):
    # Review Focus 1. The unique key contains the patient's phone number, and
    # asyncpg puts conflicting values into the exception message. One
    # logger.exception() would ship that to an error tracker (hard rule 8).
    contact = f.make_contact(f.TENANT_A)
    db_session.add(contact)
    await db_session.flush()
    db_session.add(f.make_identity(contact, n=7))
    await db_session.flush()

    other = f.make_contact(f.TENANT_A)
    db_session.add(other)
    await db_session.flush()
    db_session.add(f.make_identity(other, n=7))

    with pytest.raises(IntegrityError) as raised:
        await db_session.flush()

    assert f.phone(7) in str(raised.value)  # the raw error does leak it
    translated = as_duplicate(raised.value)
    assert isinstance(translated, DuplicateRecordError)
    assert f.phone(7) not in str(translated)
    assert f.phone(7) not in repr(translated)
    # Proves the driver exception was actually reached: a failed unwrap would
    # fall back to "unknown constraint" and this assertion would catch it.
    assert translated.constraint == "uq_contact_identities_identity"


async def test_get_or_create_open_conversation_starts_in_ai_active(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    assert conversation.state == ConversationState.AI_ACTIVE


async def test_get_or_create_open_conversation_returns_the_existing_one(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversations = ConversationRepository(db_session, f.TENANT_A)
    first = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)
    second = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)
    assert first.id == second.id


async def test_a_closed_conversation_is_not_reused(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversations = ConversationRepository(db_session, f.TENANT_A)
    first = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)
    await conversations.set_state(first.id, ConversationState.CLOSED)
    second = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)
    assert second.id != first.id
    assert await conversations.get_open(contact.id, Channel.WHATSAPP) is not None


async def test_set_state_records_when_the_state_changed(db_session):
    # Hard rule 7 re-reads this state right before every send; VS-010 races
    # against it. Knowing when it flipped is what makes that debuggable by id.
    #
    # state_changed_at is backdated first on purpose: now() is the TRANSACTION
    # start time, so without this the "before" and "after" values are the same
    # constant and the assertion could never fail.
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversations = ConversationRepository(db_session, f.TENANT_A)
    conversation = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)

    await db_session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(state_changed_at=f.LONG_AGO)
    )
    await db_session.refresh(conversation)
    assert conversation.state_changed_at == f.LONG_AGO

    updated = await conversations.set_state(conversation.id, ConversationState.HUMAN_ACTIVE)
    assert updated is not None
    assert updated.state == ConversationState.HUMAN_ACTIVE
    assert updated.state_changed_at > f.LONG_AGO


async def test_a_conversation_lookup_is_tenant_scoped(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )

    assert await ConversationRepository(db_session, f.TENANT_B).get(conversation.id) is None
    assert (
        await ConversationRepository(db_session, f.TENANT_B).set_state(
            conversation.id, ConversationState.CLOSED
        )
        is None
    )


async def test_an_inbound_message_stamps_last_inbound_at(db_session):
    # The 24h WhatsApp free-form window is measured from this value
    # (docs/architecture.md).
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    assert conversation.last_inbound_at is None

    await MessageRepository(db_session, f.TENANT_A).add(
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        modality=MessageModality.TEXT,
        status=MessageStatus.RECEIVED,
        text="synthetic message body",
        provider_message_id=f.wamid(1),
    )
    await db_session.refresh(conversation)
    assert conversation.last_inbound_at is not None
    assert conversation.last_inbound_at.tzinfo is not None


async def test_an_outbound_message_does_not_move_last_inbound_at(db_session):
    # Backdated first: now() is constant within the transaction, so comparing
    # the column against itself after an outbound send would pass even if the
    # repository wrongly stamped it.
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversations = ConversationRepository(db_session, f.TENANT_A)
    conversation = await conversations.get_or_create_open(contact.id, Channel.WHATSAPP)

    await db_session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(last_inbound_at=f.LONG_AGO)
    )
    await db_session.refresh(conversation)

    await MessageRepository(db_session, f.TENANT_A).add(
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND,
        modality=MessageModality.TEXT,
        status=MessageStatus.QUEUED,
        text="synthetic reply",
    )
    await db_session.refresh(conversation)
    assert conversation.last_inbound_at == f.LONG_AGO


async def test_recent_returns_the_newest_messages_oldest_first(db_session):
    # VS-005 feeds this straight into the chat history, which must read in the
    # order it happened. created_at is set explicitly because every row in this
    # transaction would otherwise share one now().
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    messages = MessageRepository(db_session, f.TENANT_A)

    for index in range(5):
        message = await messages.add(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            modality=MessageModality.TEXT,
            status=MessageStatus.RECEIVED,
            text=f"synthetic {index}",
            provider_message_id=f.wamid(index),
        )
        message.created_at = f.LONG_AGO + dt.timedelta(minutes=index)
    await db_session.flush()

    recent = await messages.recent(conversation.id, limit=3)
    assert [m.text for m in recent] == ["synthetic 2", "synthetic 3", "synthetic 4"]


async def test_a_message_lookup_by_provider_id_is_tenant_scoped(db_session):
    # VS-004 uses this to apply Meta status callbacks. A callback for another
    # tenant's message must find nothing.
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    await MessageRepository(db_session, f.TENANT_A).add(
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND,
        modality=MessageModality.TEXT,
        status=MessageStatus.SENT,
        provider_message_id=f.wamid(1),
    )

    assert (
        await MessageRepository(db_session, f.TENANT_A).get_by_provider_id(f.wamid(1))
        is not None
    )
    assert (
        await MessageRepository(db_session, f.TENANT_B).get_by_provider_id(f.wamid(1))
        is None
    )


async def test_a_duplicate_provider_message_id_becomes_a_safe_error(db_session):
    contacts = ContactRepository(db_session, f.TENANT_A)
    contact = await contacts.get_or_create_by_identity(Channel.WHATSAPP, f.phone(1))
    conversation = await ConversationRepository(db_session, f.TENANT_A).get_or_create_open(
        contact.id, Channel.WHATSAPP
    )
    messages = MessageRepository(db_session, f.TENANT_A)
    kwargs = {
        "conversation_id": conversation.id,
        "direction": MessageDirection.INBOUND,
        "modality": MessageModality.TEXT,
        "status": MessageStatus.RECEIVED,
        "provider_message_id": f.wamid(1),
    }
    await messages.add(text="first", **kwargs)

    with pytest.raises(DuplicateRecordError) as raised:
        await messages.add(text="my knee hurts", **kwargs)
    assert "my knee hurts" not in str(raised.value)
    assert raised.value.constraint == "uq_messages_provider_message_id"


async def test_store_if_new_returns_none_for_a_replayed_event(db_session):
    # Hard rule 2, as the webhook will use it in VS-003: a None result means
    # "already handled, return 200 and do nothing".
    inbox = WebhookInboxRepository(db_session)
    first = await inbox.store_if_new(f.event_id(1), {"n": 1})
    assert first is not None
    assert first.status == InboxStatus.RECEIVED
    assert await inbox.store_if_new(f.event_id(1), {"n": 1}) is None

    count = await db_session.scalar(sa.select(sa.func.count()).select_from(type(first)))
    assert count == 1


async def test_a_dead_letter_job_may_have_no_tenant(db_session):
    # Hard rule 11. A job that died before tenant resolution is exactly the
    # failure most worth recording, so tenant_id cannot be required here.
    job = await DeadLetterJobRepository(db_session).add(
        job_name="process_whatsapp_event",
        payload={"n": 1},
        error="BookingTimeout",
        attempts=5,
        source_event_id=f.event_id(1),
    )
    assert job.tenant_id is None

    with_tenant = await DeadLetterJobRepository(db_session).add(
        job_name="process_whatsapp_event",
        payload={"n": 2},
        error="BookingTimeout",
        attempts=5,
        tenant_id=uuid.uuid4(),
    )
    assert with_tenant.tenant_id is not None
```

The unique constraint on `messages.provider_message_id` is declared with `unique=True` on the column, so the naming convention renders it `uq_messages_provider_message_id`. Confirm the name in the generated migration and adjust the assertion if it differs.

- [ ] **Step 3: Run the tests to verify they fail**

```bash
uv run pytest tests/db/test_repository_contract.py tests/db/test_repositories.py -v
```

Expected: collection error, `ModuleNotFoundError: No module named 'app.db.repositories'`.

- [ ] **Step 4: Write `app/db/repositories/errors.py`**

```python
"""Repository errors that are safe to log.

Review Focus 1: the unique key on contact_identities contains a patient's phone
number, and asyncpg puts conflicting values into the exception message. One
logger.exception() or one Sentry capture would ship that to a third party
(hard rule 8). Nothing here ever carries the original message.
"""

from typing import Any

from sqlalchemy.exc import IntegrityError


class RepositoryError(Exception):
    """Base for errors this layer raises."""


class DuplicateRecordError(RepositoryError):
    """A unique constraint rejected the write.

    Carries the constraint name and nothing else. The name says which rule was
    broken, which is all a caller or a log line needs; the conflicting value is
    patient content.
    """

    def __init__(self, constraint: str) -> None:
        self.constraint = constraint
        super().__init__(f"duplicate violates {constraint}")


def _driver_error(error: IntegrityError) -> Any:
    """Find the exception that actually carries `constraint_name`.

    With the asyncpg dialect this is NOT `error.orig`. SQLAlchemy 2.1's asyncpg
    adapter wraps the driver exception in an emulated DBAPI error
    (`AsyncAdapt_asyncpg_dbapi.UniqueViolationError`, an `EmulatedDBAPIException`
    from sqlalchemy/exc.py) and raises it `from` the asyncpg one. So:

        IntegrityError.orig      -> the adapter exception (no constraint_name)
        .driver_exception / .orig-> the asyncpg exception (has constraint_name)

    Verified on the pinned SQLAlchemy 2.1.1 / asyncpg 0.31: reading
    `error.orig.constraint_name` returns None for every violation, which would
    silently degrade every duplicate to "unknown constraint".

    Each candidate is tried in turn so this also stays correct for sync drivers
    (psycopg), where `error.orig` IS the driver exception, and for SQLAlchemy
    2.0.x, which predates `driver_exception`.
    """
    seen: list[Any] = []
    candidate: Any = error.orig
    while candidate is not None and candidate not in seen:
        if getattr(candidate, "constraint_name", None):
            return candidate
        seen.append(candidate)
        candidate = (
            getattr(candidate, "driver_exception", None)
            or getattr(candidate, "orig", None)
            or candidate.__cause__
        )
    return None


def as_duplicate(error: IntegrityError) -> DuplicateRecordError:
    """Translate an IntegrityError, discarding its message.

    The original exception is deliberately NOT chained with `raise ... from
    error`: a chained traceback would print the message we are trying not to
    keep.
    """
    driver_error = _driver_error(error)
    constraint = getattr(driver_error, "constraint_name", None) or "unknown constraint"
    return DuplicateRecordError(constraint)
```

- [ ] **Step 5: Write `app/db/repositories/base.py`**

```python
"""The tenant boundary, made structural."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession


class Repository:
    """A repository with no tenant dimension.

    Only webhook_inbox and dead_letter_jobs qualify: both are written before
    (or instead of) tenant resolution.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session


class TenantScopedRepository(Repository):
    """Every statement this emits filters on tenant_id.

    Hard rule 4: tenant_id is resolved by our backend from the receiving
    phone_number_id, never taken from LLM output. Taking it in __init__ rather
    than per method means there is no call site that can forget it and no method
    signature the agent layer could be tempted to expose as a tool argument.

    The check runs before the session is touched, which is what lets the
    acceptance test for it run with no database.
    """

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        super().__init__(session)
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> uuid.UUID:
        return self._tenant_id
```

- [ ] **Step 6: Write `app/db/repositories/webhook_inbox.py`**

```python
"""The dedupe gate (hard rule 2)."""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.enums import Channel, InboxStatus
from app.db.models import WebhookInbox
from app.db.repositories.base import Repository


class WebhookInboxRepository(Repository):
    async def store_if_new(
        self,
        provider_event_id: str,
        payload: dict[str, Any],
        provider: str = Channel.WHATSAPP.value,
    ) -> WebhookInbox | None:
        """Store the event, or return None if it was already stored.

        ON CONFLICT DO NOTHING rather than "SELECT then INSERT": two Meta
        deliveries can land in two workers at the same moment, and both would
        see nothing. It is also why no IntegrityError is caught here — the
        database resolves the race, so no exception carrying payload content is
        ever raised.
        """
        statement = (
            pg_insert(WebhookInbox)
            .values(
                id=uuid.uuid4(),
                provider=provider,
                provider_event_id=provider_event_id,
                payload=payload,
                status=InboxStatus.RECEIVED.value,
                attempts=0,
            )
            .on_conflict_do_nothing(index_elements=["provider_event_id"])
            .returning(WebhookInbox)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def mark(
        self, row_id: uuid.UUID, status: InboxStatus, error: str | None = None
    ) -> None:
        """Move an inbox row to a new status.

        `error` must be a short reason code or exception class name. Never a
        payload excerpt: that would copy patient text into a column people read
        casually (hard rule 8).
        """
        await self._session.execute(
            sa.update(WebhookInbox)
            .where(WebhookInbox.id == row_id)
            .values(status=status.value, last_error=error)
        )

    async def attach_tenant(self, row_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        """Record the tenant once the worker has resolved it from phone_number_id."""
        await self._session.execute(
            sa.update(WebhookInbox)
            .where(WebhookInbox.id == row_id)
            .values(tenant_id=tenant_id)
        )
```

- [ ] **Step 7: Write `app/db/repositories/contacts.py`**

```python
"""Patients, found or created by the identity that messaged us."""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import Contact, ContactIdentity
from app.db.repositories.base import TenantScopedRepository


class ContactRepository(TenantScopedRepository):
    async def get(self, contact_id: uuid.UUID) -> Contact | None:
        """Tenant-scoped by construction: a correct id from the wrong tenant
        returns None rather than another clinic's patient."""
        return await self._session.scalar(
            sa.select(Contact).where(
                Contact.id == contact_id, Contact.tenant_id == self.tenant_id
            )
        )

    async def get_by_identity(self, channel: str, external_id: str) -> Contact | None:
        return await self._session.scalar(
            sa.select(Contact)
            .join(ContactIdentity, ContactIdentity.contact_id == Contact.id)
            .where(
                ContactIdentity.tenant_id == self.tenant_id,
                ContactIdentity.channel == str(channel),
                ContactIdentity.external_id == external_id,
            )
        )

    async def get_or_create_by_identity(
        self, channel: str, external_id: str, display_name: str | None = None
    ) -> Contact:
        """Find the patient behind this channel identity, creating both if new.

        The identity insert uses ON CONFLICT DO NOTHING and then re-reads. That
        is not only about concurrency: catching the IntegrityError instead would
        put an exception quoting the patient's phone number on the stack, one
        logger.exception() away from an error tracker (hard rule 8).
        """
        existing = await self.get_by_identity(channel, external_id)
        if existing is not None:
            return existing

        contact = Contact(tenant_id=self.tenant_id, display_name=display_name)
        self._session.add(contact)
        await self._session.flush()

        statement = (
            pg_insert(ContactIdentity)
            .values(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                contact_id=contact.id,
                channel=str(channel),
                external_id=external_id,
            )
            .on_conflict_do_nothing(constraint="uq_contact_identities_identity")
            .returning(ContactIdentity.id)
        )
        inserted = await self._session.execute(statement)
        if inserted.scalar_one_or_none() is not None:
            return contact

        # Another writer won the race. Drop the contact we just made and take
        # theirs, so one patient never ends up as two rows.
        await self._session.delete(contact)
        await self._session.flush()
        winner = await self.get_by_identity(channel, external_id)
        assert winner is not None  # the conflict proves the row exists
        return winner
```

- [ ] **Step 8: Write `app/db/repositories/conversations.py`**

```python
"""The thread, and the state machine hard rule 7 re-reads before every send."""

import uuid

import sqlalchemy as sa

from app.db.enums import ConversationState
from app.db.models import Conversation
from app.db.repositories.base import TenantScopedRepository

CLOSED = ConversationState.CLOSED.value


class ConversationRepository(TenantScopedRepository):
    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        return await self._session.scalar(
            sa.select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self.tenant_id,
            )
        )

    async def get_open(self, contact_id: uuid.UUID, channel: str) -> Conversation | None:
        """The one non-CLOSED conversation, if there is one.

        "The one" is guaranteed by the partial unique index, not by this query.
        """
        return await self._session.scalar(
            sa.select(Conversation).where(
                Conversation.tenant_id == self.tenant_id,
                Conversation.contact_id == contact_id,
                Conversation.channel == str(channel),
                Conversation.state != CLOSED,
            )
        )

    async def get_or_create_open(
        self, contact_id: uuid.UUID, channel: str
    ) -> Conversation:
        """Check-then-insert.

        A concurrent writer can win between the check and the insert, in which
        case the partial unique index raises IntegrityError and it propagates
        unchanged. That is deliberate: the caller (VS-004's job) should treat it
        as retryable and re-read, not dead-letter. The conflicting values are
        ids only, so the raw error is not a hard-rule-8 exposure.
        """
        existing = await self.get_open(contact_id, channel)
        if existing is not None:
            return existing
        conversation = Conversation(
            tenant_id=self.tenant_id,
            contact_id=contact_id,
            channel=str(channel),
            state=ConversationState.AI_ACTIVE.value,
        )
        self._session.add(conversation)
        await self._session.flush()
        return conversation

    async def set_state(
        self, conversation_id: uuid.UUID, state: ConversationState
    ) -> Conversation | None:
        """Move the conversation and record when. Returns None for another
        tenant's id, so a tenant-resolution bug changes nothing."""
        result = await self._session.execute(
            sa.update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == self.tenant_id,
            )
            .values(state=state.value, state_changed_at=sa.func.now())
            .returning(Conversation)
        )
        updated = result.scalar_one_or_none()
        if updated is not None:
            await self._session.refresh(updated)
        return updated
```

- [ ] **Step 9: Write `app/db/repositories/messages.py`**

```python
"""Messages in both directions, and the history VS-005 reads."""

import uuid

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.enums import MessageDirection, MessageModality, MessageStatus
from app.db.models import Conversation, Message
from app.db.repositories.base import TenantScopedRepository
from app.db.repositories.errors import as_duplicate


class MessageRepository(TenantScopedRepository):
    async def add(
        self,
        conversation_id: uuid.UUID,
        direction: MessageDirection,
        modality: MessageModality,
        status: MessageStatus,
        text: str | None = None,
        provider_message_id: str | None = None,
    ) -> Message:
        """Store one message.

        A duplicate provider_message_id becomes DuplicateRecordError, which
        carries the constraint name only — the raw IntegrityError quotes row
        values, and this row's values are patient content (hard rule 8).
        """
        message = Message(
            tenant_id=self.tenant_id,
            conversation_id=conversation_id,
            direction=direction.value,
            modality=modality.value,
            status=status.value,
            text=text,
            provider_message_id=provider_message_id,
        )
        self._session.add(message)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise as_duplicate(error) from None

        if direction is MessageDirection.INBOUND:
            # The 24h WhatsApp free-form window is measured from the patient's
            # last message (docs/architecture.md). Only inbound moves it.
            await self._session.execute(
                sa.update(Conversation)
                .where(
                    Conversation.id == conversation_id,
                    Conversation.tenant_id == self.tenant_id,
                )
                .values(last_inbound_at=sa.func.now())
            )
        return message

    async def get_by_provider_id(self, provider_message_id: str) -> Message | None:
        return await self._session.scalar(
            sa.select(Message).where(
                Message.provider_message_id == provider_message_id,
                Message.tenant_id == self.tenant_id,
            )
        )

    async def recent(self, conversation_id: uuid.UUID, limit: int) -> list[Message]:
        """The newest `limit` messages, returned oldest first.

        Newest-first in SQL so the index does the work and the LIMIT is cheap;
        reversed in Python so the caller gets chat order.
        """
        result = await self._session.scalars(
            sa.select(Message)
            .where(
                Message.tenant_id == self.tenant_id,
                Message.conversation_id == conversation_id,
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        return list(reversed(result.all()))
```

- [ ] **Step 10: Write `app/db/repositories/dead_letter.py`**

```python
"""Where jobs go when retrying stops being useful (hard rule 11)."""

import uuid
from typing import Any

from app.db.models import DeadLetterJob
from app.db.repositories.base import Repository


class DeadLetterJobRepository(Repository):
    """Not tenant-scoped: a job can die before tenant resolution succeeds, and
    that is exactly the failure most worth recording."""

    async def add(
        self,
        job_name: str,
        payload: dict[str, Any],
        error: str,
        attempts: int,
        tenant_id: uuid.UUID | None = None,
        source_event_id: str | None = None,
    ) -> DeadLetterJob:
        job = DeadLetterJob(
            tenant_id=tenant_id,
            job_name=job_name,
            source_event_id=source_event_id,
            payload=payload,
            # A reason code or exception class name. Never a formatted
            # exception carrying request or payload content (hard rule 8).
            error=error[:1000],
            attempts=attempts,
        )
        self._session.add(job)
        await self._session.flush()
        return job
```

- [ ] **Step 11: Write `app/db/repositories/__init__.py`**

```python
"""Tenant-scoped data access.

Nothing above this layer writes SQL, and nothing in this layer is reachable from
the LLM: hard rule 3 gives the model tools in app/agent/tools/ only, and those
tools call services that call these repositories.
"""

from app.db.repositories.contacts import ContactRepository
from app.db.repositories.conversations import ConversationRepository
from app.db.repositories.dead_letter import DeadLetterJobRepository
from app.db.repositories.errors import (
    DuplicateRecordError,
    RepositoryError,
    as_duplicate,
)
from app.db.repositories.messages import MessageRepository
from app.db.repositories.webhook_inbox import WebhookInboxRepository

__all__ = [
    "ContactRepository",
    "ConversationRepository",
    "DeadLetterJobRepository",
    "DuplicateRecordError",
    "MessageRepository",
    "RepositoryError",
    "WebhookInboxRepository",
    "as_duplicate",
]
```

- [ ] **Step 12: Run the tests to verify they pass**

```bash
docker compose up -d postgres
uv run pytest tests/db/test_repository_contract.py tests/db/test_repositories.py -v
```

Expected: 16 passed (2 contract + 14 behaviour).

- [ ] **Step 13: Run the whole suite both ways, lint**

```bash
uv run pytest -q                                        # 68 passed
docker compose stop postgres && uv run pytest -q && docker compose start postgres
uv run ruff check . && uv run ruff format .
```

Expected: `68 passed`, then `37 passed, 31 skipped`.

- [ ] **Step 14: Checkpoint with the developer**

---

### Task 6: Documentation and slice close-out

**Files:**
- Modify: `README.md` (migrations and the two ways to run tests)
- Modify: `docs/slices/VS-002.md` (Status, Notes, Follow-ups)
- Modify: `docs/slices/README.md` (status table)

- [ ] **Step 1: Run the full verification suite in the container**

This is the run the acceptance criteria are judged on. A skipped test is not a passed one.

```bash
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api alembic downgrade base
docker compose exec api alembic upgrade head
docker compose exec api pytest -q
docker compose exec api ruff check .
curl -s localhost:8000/health/ready
```

Expected: every alembic command exits 0; `68 passed`; ruff clean; readiness 200 with both dependencies OK.

- [ ] **Step 2: Confirm the no-dependency run still holds**

```bash
docker compose down
uv run pytest -q
```

Expected: `37 passed, 31 skipped`. If anything *fails* rather than skips, a database test escaped the `db` marker.

- [ ] **Step 3: Add a migrations and testing section to `README.md`**

After the existing "Run it locally" block:

````markdown
## Database and migrations

```bash
docker compose exec api alembic upgrade head          # apply
docker compose exec api alembic downgrade -1          # undo the last revision
docker compose exec api alembic revision --autogenerate -m "what changed"
```

Alembic reads the DSN from `DATABASE_URL` via `app.config`, not from `alembic.ini`
— `sqlalchemy.url` there is deliberately empty so no credential is committed.
`./migrations` and `./alembic.ini` are bind-mounted into the api container, which
is what makes `--autogenerate` write the new revision onto the host.

Autogenerate does **not** detect CHECK constraint changes. Widening one of the
`StrEnum`s in `app/db/enums.py` needs a hand-written migration plus a new case in
`tests/db/test_constraints.py`.

## Running the tests

Tests that need a real PostgreSQL are marked `db`. They use a separate database,
`doctoleb_test`, created on first run, and each test runs in a transaction that is
rolled back.

```bash
docker compose exec api pytest        # everything (the full run)
docker compose up -d postgres && uv run pytest   # everything, from the host
uv run pytest                         # nothing running: db tests skip, the rest pass
uv run pytest -m "not db"             # skip them explicitly
```

Set `TEST_DATABASE_URL` to point somewhere else; otherwise it is derived from
`DATABASE_URL`.
````

- [ ] **Step 4: Update `docs/slices/VS-002.md`**

Set `Status: DONE` and append:

```markdown
## Notes
- State, direction, modality and status are VARCHAR + named CHECK, not native
  PostgreSQL ENUM types. `modality` gains VOICE_NOTE in VS-008 and message
  `status` grows in VS-004; `ALTER TYPE ... ADD VALUE` has no reverse operation,
  which would make the "downgrade works" acceptance criterion unmeetable.
- Alembic's autogenerate NEVER compares CHECK constraints. They are emitted into
  the initial create_table and never looked at again, so the drift test cannot
  protect them. Widening an enum needs a hand-written migration, and
  tests/db/test_constraints.py proves at runtime that an invalid value is
  actually rejected.
- `Base.metadata` carries a `naming_convention`. Without it PostgreSQL names
  CHECK constraints at random, autogenerate gets noisy, and a violated
  constraint cannot be reported by name.
- `UUIDPrimaryKeyMixin` assigns `id` in `__init__`, not only via
  `mapped_column(default=...)`. That default is an INSERT-time
  `CallableColumnDefault`, so `row.id` would be None until flush — and half this
  slice builds a parent and child in one go and reads the parent's id first.
- `webhook_inbox.tenant_id` and `dead_letter_jobs.tenant_id` are nullable.
  Everything else's is NOT NULL. The webhook stores the raw event before anything
  resolves a tenant (hard rule 1), and a job can die before resolution succeeds.
- `messages.provider_message_id` is unique AND nullable. An outbound message that
  failed before Meta accepted it has no id; PostgreSQL permits many NULLs under a
  unique constraint, so the second failed send does not crash the worker.
- One open conversation per (tenant, contact, channel) is enforced by a PARTIAL
  unique index (`WHERE state <> 'CLOSED'`), not by the repository. Two workers
  handling two messages from the same patient both see no open conversation.
- `ConversationRepository.get_or_create_open` is check-then-insert and lets that
  index raise a raw `IntegrityError` when it loses the race. **VS-004's caller
  must treat that as retryable** — re-read and reuse the winner's conversation,
  not dead-letter the job. The conflicting values there are ids only, so unlike
  the contact_identities case the raw error is not a hard-rule-8 exposure.
- Repositories never re-raise a raw `IntegrityError` where the conflicting values
  are patient content. The unique key on `contact_identities` contains the
  patient's phone number, so one `logger.exception()` would ship it to an error
  tracker (hard rule 8). `as_duplicate()` keeps the constraint name and discards
  the rest, deliberately without `raise ... from`.
- `as_duplicate()` cannot read `error.orig.constraint_name`. With the asyncpg
  dialect `error.orig` is SQLAlchemy's emulated DBAPI exception
  (`EmulatedDBAPIException`, sqlalchemy/exc.py); the asyncpg exception that
  carries `constraint_name` is one level further down, at
  `error.orig.driver_exception` (also reachable as `__cause__`, since the adapter
  raises `from` it). Reading `.orig` directly returns None and silently degrades
  every duplicate to "unknown constraint". The helper walks the chain so it also
  works for sync drivers, where `.orig` IS the driver exception.
- `migrations/env.py` calls `fileConfig(..., disable_existing_loggers=False)`.
  The default is True, and the test suite runs migrations in-process: `tests/db`
  sorts before `tests/test_readiness.py`, so the default disables
  `app.api.health`'s logger and VS-001's caplog tests fail in the full run only.
- `Dockerfile` copies `alembic.ini` and `migrations/`, and compose bind-mounts
  both onto the api service. Without the COPY, alembic cannot run in the
  container at all; without the mount, `--autogenerate` writes the revision into
  the container and it disappears.
- PostgreSQL's `now()` is the TRANSACTION start time, constant across a whole
  test. Any test asserting "this timestamp moved" backdates the column to
  `factories.LONG_AGO` first; without that it compares now() to now() and passes
  no matter what the code does.
- Alembic's async template calls `asyncio.run()` inside `env.py`. Calling
  `command.upgrade` from a pytest-asyncio coroutine raises "asyncio.run() cannot
  be called from a running event loop"; the migration fixtures are therefore
  synchronous.
- `db_session` binds to a connection inside a transaction that is always rolled
  back, with `join_transaction_mode="create_savepoint"` so a test may commit.
  Concurrency tests need `second_session_factory` instead: one rolled-back
  transaction cannot show another session uncommitted rows.
- `db_engine` is function-scoped. `asyncio_default_fixture_loop_scope` is
  "function" in this project, so a session-scoped async fixture would bind to a
  loop that closes after the first test.
- `expire_on_commit=False` on the session factory: with the default, reading an
  attribute after commit re-queries, and in async code that raises
  `MissingGreenlet` rather than merely being slow.
- The "repository queries require tenant_id" acceptance test lives in
  `tests/db/test_repository_contract.py` with no `db` marker and a `None`
  session, so it runs even with nothing up. The cross-tenant *behaviour* tests
  still need a database.

## Follow-ups
- The test database is created and never dropped. A stale `doctoleb_test` survives
  a schema rewrite and can hide a broken migration; add a recreate flag or teardown.
- `webhook_inbox.payload` and `dead_letter_jobs.payload` keep raw Meta payloads,
  which contain patient text and phone numbers, indefinitely. A dead letter stores
  the same event body the inbox does, so a retention policy has to cover both
  tables. Pair it with VS-008's audio retention setting.
- No index on `messages.status` or `webhook_inbox.status`. Add them when VS-004
  has a real query pattern instead of guessing now.
- `ConversationRepository.get_or_create_open` has no retry of its own. If VS-004
  finds itself writing the same retry twice, lift it into a helper there.
- `contacts` has no `booking_patient_ref`. Open question 2 in
  `docs/booking-contract.md` decides whether VS-007 needs one.
- No `tenants` table and no FK on any `tenant_id`. Open question 1 decides who
  owns the tenant ↔ WhatsApp number mapping; VS-004 uses a config mapping.
```

- [ ] **Step 5: Update `docs/slices/README.md`**

Set VS-002 to `DONE` in the status table.

- [ ] **Step 6: Explain the slice function by function**

`CLAUDE.md` requires it: after finishing, walk through what each function does, why it exists, and which hard rule it protects. Cover at minimum `check_constraint`, `Base.__repr__`, `UUIDPrimaryKeyMixin.__init__`, `get_sessionmaker`, `store_if_new`, `get_or_create_by_identity`, `get_or_create_open`, `set_state`, `MessageRepository.add`, `recent`, `as_duplicate` / `_driver_error`, and the four database fixtures.

- [ ] **Step 7: Checkpoint with the developer**

---

## Acceptance criteria mapped to tasks

| VS-002 requirement | Where it is built | Where it is proven |
|---|---|---|
| `webhook_inbox` (unique `provider_event_id`, raw payload JSON, status, attempts) | Task 2, Step 3 | Task 2, Step 1 (metadata); Task 4, Step 2 (`test_duplicate_provider_event_id_is_rejected`) |
| `contacts` | Task 2, Step 4 | Task 2, Step 1; Task 5, Step 2 (`test_get_or_create_by_identity_creates_a_contact_and_an_identity`) |
| `contact_identities` (channel + external id, unique per tenant) | Task 2, Step 4 | Task 4, Step 2 (`test_the_same_phone_number_may_exist_in_two_tenants`, `..._twice_in_one_tenant_is_rejected`) |
| `conversations` with the four states | Task 2, Step 5 | Task 1, Step 2 (enum values); Task 2, Step 1 (CHECK declared); Task 4, Step 2 (`test_an_invalid_conversation_state_is_rejected`); Task 5, Step 2 (`test_set_state_records_when_the_state_changed`) |
| `messages` (direction, modality, unique `provider_message_id`, text, status) | Task 2, Step 6 | Task 2, Step 1; Task 4, Step 2 (`test_duplicate_provider_message_id_is_rejected`, `test_many_messages_may_have_no_provider_message_id`) |
| `dead_letter_jobs` | Task 2, Step 7 | Task 2, Step 1; Task 5, Step 2 (`test_a_dead_letter_job_may_have_no_tenant`) |
| `tenant_id` everywhere applicable | Task 2, Steps 3–7 | Task 2, Step 1 (`test_tenant_id_is_not_null_everywhere_a_tenant_is_knowable`) |
| `created_at` / `updated_at` on every table | Task 1, Step 5 (`TimestampMixin`) | Task 2, Step 1; Task 4, Step 2 (`test_stored_timestamps_come_back_timezone_aware`) |
| Alembic migrations exist and run in the container | Task 3, Steps 1–7 (incl. Dockerfile + compose) | Task 3, Step 7 (`alembic --version`, `ls migrations/versions/`); Task 6, Step 1 |
| `alembic upgrade head` on an empty DB works | Task 3, Step 7 | Task 3, Step 10 (`test_upgrade_head_on_an_empty_database_creates_every_table`); Task 6, Step 1 (live) |
| downgrade works | Task 3, Step 7 | Task 3, Step 10 (`test_downgrade_base_leaves_nothing_behind`, `test_upgrade_downgrade_upgrade_is_repeatable`); Task 6, Step 1 (live) |
| Test: duplicate `provider_event_id` fails | — | Task 4, Step 2 |
| Repositories with tenant-scoped query helpers | Task 5, Steps 4–11 | Task 5, Step 12 |
| Test: repository queries require `tenant_id` | Task 5, Step 5 (`TenantScopedRepository.__init__`) | Task 5, Step 1 (`test_a_tenant_scoped_repository_cannot_be_built_without_a_tenant`, `test_only_the_pre_tenant_tables_use_the_unscoped_repository` — **no database needed**); Task 5, Step 2 (`test_a_contact_lookup_returns_nothing_for_another_tenants_id`, `test_a_conversation_lookup_is_tenant_scoped`, `test_a_message_lookup_by_provider_id_is_tenant_scoped`) |
| No clinic/appointment tables (out of scope) | — | Task 2, Step 1 (`test_the_slice_creates_exactly_these_tables`) |
| `pytest` passes | every task | Task 6, Steps 1–2 |
| `ruff check` clean | every task | Task 6, Step 1 |
| Slice file Status and Notes updated | Task 6, Steps 4–5 | Task 6, Step 7 |
| Hard rule 8: no patient content in logs, trackers or fixtures | Task 1, Step 5; Task 4, Step 1; Task 5, Step 4 | Task 1, Step 2; Task 2, Step 1 (`test_no_model_repr_leaks_content`); Task 5, Step 2 (`test_a_duplicate_identity_error_never_carries_the_phone_number`, `test_a_duplicate_provider_message_id_becomes_a_safe_error`) |
| Hard rule 9: no credential in committed source | Task 3, Steps 2–3 | Task 3, Step 11 (skip reason carries host and port only) |
| Models and migrations do not drift (except CHECKs) | Task 3, Step 3 (`compare_type`, `compare_server_default`) | Task 3, Step 10 (`test_models_and_migrations_do_not_drift`) + Task 4, Step 2 (CHECK coverage) |


