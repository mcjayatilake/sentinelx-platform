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

11. **`Base` uses `__mapper_args__ = {"eager_defaults": True}`
    (`backend/app/db/base.py`) — do not remove it.** Without it, a
    server-generated/`onupdate` column (`created_at`, `updated_at`)
    accessed after a flush inside an async request — e.g. serializing to
    a Pydantic schema right after a service-layer create-then-update —
    raises `MissingGreenlet`, since `AsyncSession` only permits DB IO from
    an awaited call and a later plain attribute access isn't one.

## Authentication / authorization rules

These rules exist because getting auth wrong is a direct security
incident, not a style nit. See
[`docs/authentication.md`](docs/authentication.md),
[`docs/rbac.md`](docs/rbac.md), and [`docs/security.md`](docs/security.md)
for the full design; this is the enforceable summary.

1. **Passwords: Argon2id only, via `app.core.security.hash_password`/
   `verify_password`.** Never hash a password with anything else, never
   log or return one (raw or hashed) in a request/response, never add a
   `deprecated="auto"` fallback scheme without a real pre-existing corpus
   of hashes in that scheme to support (there wasn't one when bcrypt was
   removed from the `CryptContext` — see ADR 0002).

2. **High-entropy random secrets (refresh tokens, API key secrets,
   verification/reset tokens) are SHA-256-hashed via
   `app.core.security.hash_secret`, never Argon2id-hashed.** These are
   two different threat models (see ADR 0004) — do not "unify" them.

3. **No endpoint may resolve `tenant_id` from a client-supplied header,
   query parameter, or path parameter.** Use `CurrentPrincipalDep`
   (`principal.tenant_id`), which comes from a verified access-token
   claim plus a live `TenantMembership` re-check
   (`app.api.deps.get_current_principal`). This is the same discipline as
   rule 1 above (repository `tenant_id` argument), applied one layer up.

4. **No endpoint hand-rolls a role/permission check.** Use
   `Depends(require_permission(Permission.X))` as a router-level
   `dependencies=[...]` entry (not bound to the `principal` parameter's
   default — see `docs/rbac.md` for why that specific combination is
   wrong). Adding a new permission means adding it to the
   `Permission` enum and `ROLE_PERMISSIONS` matrix in
   `app/core/permissions.py`, not a new `if role == ...` somewhere.

5. **Never store a plaintext API key, refresh token, or verification
   token** — same rule as database rule 8, extended: `hashed_token`/
   `hashed_secret` fields, never on a `*Read` schema, and the one-time
   plaintext response (`APIKeyIssueResponse.api_key`,
   `TokenPair.refresh_token`) is never persisted anywhere after that
   response is sent.

6. **Every auth-relevant mutation writes an `AuditEvent` from
   `app.services.auth_service` (or the relevant endpoint for
   membership/API-key actions) — never construct the same audit event at
   more than one call site.** See `docs/security.md` for the full list of
   audited actions. Adding a new security-sensitive action means adding
   one `AuditEventCreate` call at its single natural call site, not
   scattering it across every place that could trigger it.

7. **`POST /auth/password/reset/request` must return the same generic
   response and write no audit event for an unregistered email.** This is
   deliberate enumeration resistance (see `docs/security.md`) — do not
   "fix" it to be more informative.

## Where things live

```
backend/app/
├── models/         SQLAlchemy ORM — one file per entity, enums.py, mixins.py
├── schemas/         Pydantic Create/Update/Read — never expose ORM models directly
├── repositories/     One explicit class per entity, tenant-scoped where applicable
├── services/        Business orchestration (auth, API keys, email) — the only
│                     place that constructs multi-step flows across repositories
├── api/
│   ├── deps.py        Shared FastAPI dependencies (current user/principal, db session)
│   └── v1/endpoints/  One router module per resource
└── db/
    ├── base.py        Declarative Base + naming convention
    ├── session.py      Async engine/session
    └── seed.py          Dev-only seed data (never runs automatically)
```
