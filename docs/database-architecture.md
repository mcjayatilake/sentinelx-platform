# Database Architecture

This document covers the persistence layer added in the database-foundation
phase: engine/session lifecycle, migrations, and the conventions every new
model must follow. For entity descriptions and relationships, see
[`data-model.md`](data-model.md). For the tenant-isolation strategy
specifically, see [`tenant-isolation.md`](tenant-isolation.md).

## Stack

- **PostgreSQL** — the only supported production database. SQLite is
  permitted in tests only where PostgreSQL-specific behavior (composite
  foreign keys, `JSONB`, `pg_column_size`, CHECK-constraint enums) isn't
  being exercised — in practice, this project's test suite runs entirely
  against real PostgreSQL (see [`local-development.md`](local-development.md)),
  because most of what's worth testing here *is* PostgreSQL-specific.
- **SQLAlchemy 2.x**, fully typed declarative models (`Mapped`/`mapped_column`),
  async engine/session for the application, sync engine for Alembic.
- **Alembic** for schema migrations.
- **Pydantic v2** for the API/persistence boundary (`app/schemas`) — never
  the ORM models themselves.

## Engine & session lifecycle

- `app/db/session.py` creates one module-level async `engine` from
  `Settings.database_url`, pool size/overflow from
  `DATABASE_POOL_SIZE`/`DATABASE_MAX_OVERFLOW`, and `pool_pre_ping=True` so
  stale connections are detected and replaced rather than surfacing as
  request failures.
- `AsyncSessionLocal` is an `async_sessionmaker` bound to that engine;
  `get_db()` is a FastAPI dependency that yields one request-scoped session.
- On shutdown, `app/main.py`'s lifespan handler calls `await engine.dispose()`
  so connections are closed cleanly rather than left for the process to tear
  down.
- Readiness (`GET /api/v1/health/ready`) executes `SELECT 1` against this
  engine and reports `database: "ok" | "unavailable"` without ever logging
  connection strings or credentials.

## Primary keys, timestamps, enums

These conventions are enforced by shared mixins/helpers so every model gets
them automatically rather than by convention alone:

- **UUID primary keys** (`app/models/mixins.py::UUIDPrimaryKeyMixin`) —
  generated client-side via `uuid.uuid4()`, not `gen_random_uuid()`. This
  avoids any dependency on a PostgreSQL extension or version; there is
  nothing stopping a future move to server-side generation if a specific
  workload benefits from it.
- **Timestamps** (`TimestampMixin` / `CreatedAtMixin`) — always
  `TIMESTAMP WITH TIME ZONE`, `server_default=func.now()`, `updated_at`
  additionally `onupdate=func.now()`. `CreatedAtMixin` (no `updated_at`) is
  used only by `AuditEvent`, enforcing append-only at the schema level.
- **Enums** — every enum column uses
  `sa.Enum(PyEnum, native_enum=False, create_constraint=True, validate_strings=True, length=32)`.
  This renders as `VARCHAR(32)` plus a `CHECK (col IN (...))` constraint,
  instead of a native PostgreSQL `ENUM` type. The reason is entirely about
  migration safety: adding a new value to a native `ENUM` type requires
  `ALTER TYPE ... ADD VALUE`, which cannot run inside a transaction block in
  older PostgreSQL and interacts badly with Alembic's transactional
  migrations. A `VARCHAR` + `CHECK` constraint is a plain
  `ALTER TABLE ... DROP CONSTRAINT ... ADD CONSTRAINT ...`, fully
  transactional. **Both `native_enum=False` and `create_constraint=True`
  are required** — without `create_constraint=True`, SQLAlchemy 2.x does
  not emit a CHECK constraint at all (this was caught by
  `test_severity_check_constraint_enforced_at_db_level` during development:
  the constraint was silently absent until that flag was added).
  All enum classes live in `app/models/enums.py`.

## Constraints

- Naming is centralized in `app/db/base.py`'s `NAMING_CONVENTION` for
  indexes, unique constraints, foreign keys, and primary keys — every
  constraint gets a deterministic name without hand-naming each one.
- `CheckConstraint` is the one exception: SQLAlchemy's naming convention
  always renders a `CheckConstraint`'s `name=` through the
  `ck_%(table_name)s_%(constraint_name)s` template, so hand-written check
  constraints pass a *bare suffix* (e.g. `name="completed_after_started"`),
  not a pre-prefixed name — passing the full `ck_scans_...` name yourself
  produces a doubled prefix (caught the same way, by generating and
  reviewing the migration before committing it).

## Migrations

- `alembic/env.py` imports `app.models` (which imports every model module)
  before reading `Base.metadata`, so `--autogenerate` sees every table.
- The sync (`psycopg`) URL is used for migrations
  (`Settings.database_url_sync`), independent of the async URL the
  application uses — Alembic's migration runner is synchronous.
- **Autogenerate is never trusted blindly.** Every generated migration in
  this repository has been hand-reviewed against the models before commit.
  Two real bugs were caught this way during development of the initial
  migration: a composite foreign key referencing a unique constraint that
  didn't exist yet (`Project` was missing `UNIQUE (tenant_id, id)`, needed
  by `Asset`/`Scan`/`Finding`'s composite FKs), and a `CheckConstraint` SQL
  string referencing the Python attribute name (`event_metadata`) instead
  of the actual database column name (`metadata`, chosen because `metadata`
  collides with SQLAlchemy's own `Base.metadata` as a Python attribute).
  Both were only visible by actually running `alembic upgrade head` against
  real PostgreSQL, not by reading the models alone.
- See [`local-development.md`](local-development.md) for the exact
  commands to generate, review, and apply migrations.

## Seed data

`app/db/seed.py` creates exactly one clearly-labeled tenant, user,
membership, and project for local development. It:

- refuses to run when `ENVIRONMENT=production` (`SystemExit`),
- is idempotent — matches existing rows by slug/email and reuses them,
- is never invoked automatically (no call from `app/main.py`'s lifespan or
  anywhere else at startup) — only via `python -m app.db.seed` / `make seed`.

## What this phase does not include

No API endpoints expose these models, no authentication/authorization is
implemented, and Row-Level Security is deliberately deferred — see
[`docs/decisions/0001-tenant-isolation-and-rls.md`](decisions/0001-tenant-isolation-and-rls.md).
