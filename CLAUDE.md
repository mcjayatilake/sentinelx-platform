# CLAUDE.md

Guidance for AI agents (and humans) working in this repository.

## General

- Follow [`docs/CODING_STANDARDS.md`](docs/CODING_STANDARDS.md) for
  language/formatting/testing conventions — this file does not repeat
  those, only what's specific to working with the database layer.
- Follow [`docs/architecture/SECURITY.md`](docs/architecture/SECURITY.md)
  for tenancy, authorization, and secrets-handling constraints — they are
  binding, not suggestions.

## Database development rules

These rules exist because this platform stores multi-tenant customer
security data. Violating them is a security issue, not a style nit.

1. **Never add a tenant-owned repository method without a required
   `tenant_id` argument.** No `list_all()`/`get_by_id()` for tenant-owned
   data that doesn't take `tenant_id` as a required, positional parameter.
   See [`docs/tenant-isolation.md`](docs/tenant-isolation.md).

2. **Never add a generic `Repository[T]` base class.** Each repository is
   deliberately small and explicit — see
   [`backend/app/repositories/base.py`](backend/app/repositories/base.py)
   for why. A shared generic base is exactly the kind of abstraction that
   can hide a missing tenant filter.

3. **When a new tenant-owned entity references another tenant-owned
   entity, propagate `tenant_id` via a composite foreign key**, not a
   plain single-column FK — see any of `Asset`/`Scan`/`Finding` in
   `backend/app/models/` for the pattern, and add the matching
   `UNIQUE (tenant_id, id)` constraint on the parent if it doesn't already
   have one.

4. **Every enum column must use
   `sa.Enum(PyEnum, native_enum=False, create_constraint=True, validate_strings=True, length=32)`
   — both `native_enum=False` and `create_constraint=True`.** Omitting
   `create_constraint=True` silently produces a column with no `CHECK`
   constraint at all (SQLAlchemy 2.x does not default it to `True`). This
   was a real bug caught by tests during the database-foundation phase.

5. **Never use a native PostgreSQL `ENUM` type** (`native_enum=True`) — it
   makes adding a new enum value a non-transactional migration
   (`ALTER TYPE ... ADD VALUE`). See
   [`docs/database-architecture.md`](docs/database-architecture.md).

6. **Never trust `alembic revision --autogenerate` output without reading
   it.** Generate, then open the file and check it against the models that
   changed, before committing. Autogenerate has missed real bugs during
   this project's development (a missing composite-FK-supporting unique
   constraint, and a `CheckConstraint` referencing the wrong column name)
   that were only caught by actually running the migration against
   PostgreSQL — see `database-architecture.md`.

7. **`AuditEvent` is append-only.** Never add an `update()`/`delete()`
   method to `AuditEventRepository`, and never add an `updated_at` column
   to the `AuditEvent` model.

8. **Never store a plaintext secret.** `APIKeyMetadata.hashed_secret` (and
   any future credential field) must be named to make clear it's hashed,
   and must never appear on a `*Read` schema.

9. **Repositories never call `session.commit()`.** They may call
   `session.flush()` (so constraint violations surface immediately), but
   committing is the caller's responsibility — this keeps transaction
   boundaries explicit and testable.

10. **Tests that touch the database run against real PostgreSQL**, not
    SQLite — most of what's worth testing here (composite FKs,
    `CHECK`-constraint enums, `JSONB` size limits) is PostgreSQL-specific.
    See [`docs/local-development.md`](docs/local-development.md) for the
    fixture pattern (`db_session`, `db_engine`) and a known
    pytest-asyncio pitfall it works around.

## Where things live

```
backend/app/
├── models/         SQLAlchemy ORM — one file per entity, enums.py, mixins.py
├── schemas/         Pydantic Create/Update/Read — never expose ORM models directly
├── repositories/     One explicit class per entity, tenant-scoped where applicable
└── db/
    ├── base.py        Declarative Base + naming convention
    ├── session.py      Async engine/session
    └── seed.py          Dev-only seed data (never runs automatically)
```
